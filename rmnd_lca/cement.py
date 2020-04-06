import copy
import csv
import uuid
import pandas as pd
import numpy as np
import wurst
from wurst import searching as ws
from . import DATA_DIR
from .activity_maps import InventorySet
from .geomap import Geomap

LHV_FUELS = DATA_DIR / "fuels_lower_heating_value.txt"
CO2_FUELS = DATA_DIR / "fuel_co2_emission_factor.txt"
CLINKER_RATIO_ECOINVENT_36 = DATA_DIR / "cement" / "clinker_ratio_ecoinvent_36.csv"
CLINKER_RATIO_ECOINVENT_35 = DATA_DIR / "cement" / "clinker_ratio_ecoinvent_35.csv"
CLINKER_RATIO_REMIND = DATA_DIR / "cement" / "clinker_ratios.csv"


class Cement:
    """
    Class that modifies clinker and cement production datasets in ecoinvent based on REMIND and WBCSD's GNR data.
    :ivar scenario: name of a Remind scenario
    :vartype scenario: str

    """

    def __init__(self, db, rmd, scenario, year, version):
        self.db = db
        self.rmd = rmd
        self.geo = Geomap()
        self.scenario = scenario
        self.year = year
        self.version = version

        self.clinker_ratio_eco = self.get_clinker_ratio_ecoinvent(version)
        self.clinker_ratio_remind = self.get_clinker_ratio_remind()

        self.fuels_lhv = self.get_lower_heating_values()
        self.fuels_co2 = self.get_fuel_co2_emission_factors()

        mapping = InventorySet(self.db)
        self.emissions_map = mapping.get_remind_to_ecoinvent_emissions()
        self.fuel_map = mapping.generate_fuel_map()

    def get_clinker_ratio_remind(self):
        """
        Return an array with the average clinker-to-cement ratio per year and per region, as given by REMIND.
        :return: xarray
        :return:
        """
        df = pd.read_csv(
            CLINKER_RATIO_REMIND)

        return df.groupby(["region", "year"]) \
            .mean()["value"] \
            .to_xarray() \
            .interp(year=self.year)

    @staticmethod
    def get_clinker_ratio_ecoinvent(version):
        """
        Return a dictionary with (cement names, location) as keys and clinker-to-cement ratios as values,
        as found in ecoinvent.
        :return: dict
        """
        if version == 3.5:
            fp = CLINKER_RATIO_ECOINVENT_35
        else:
            fp = CLINKER_RATIO_ECOINVENT_36

        with open(fp) as f:
            d = {}
            for val in csv.reader(f):
                d[(val[0], val[1])] = float(val[2])
        return d

    @staticmethod
    def get_fuel_co2_emission_factors():
        """
        Return a dictionary with fuel names as keys and, as values:
        * CO_2 emission factor, in kg CO2 per MJ of lower heating value
        * share of biogenic CO2

        Source: https://www.plateformeco2.ch/portal/documents/10279/16917/IPCC+(2006),%20Guidelines+for+National+Greenhouse+Gas+Inventories.pdf/a3838a98-5ad6-4da5-82f3-c9430007a158

        :return: dict
        """
        d = {}
        with open(CO2_FUELS) as f:
            r = csv.reader(f, delimiter=";")
            for row in r:
                d[row[0]] = {"co2": float(row[1]), "bio_share": float(row[2])}

        return d

    @staticmethod
    def get_lower_heating_values():
        """
        Loads a csv file into a dictionary. This dictionary contains lower heating values for a number of fuel types.
        Mostly taken from: https://www.engineeringtoolbox.com/fuels-higher-calorific-values-d_169.html

        :return: dictionary that contains lower heating values
        :rtype: dict
        """
        with open(LHV_FUELS) as f:
            return dict(filter(None, csv.reader(f, delimiter=";")))

    def fetch_proxies(self, name, ref_prod):
        """
        Fetch dataset proxies, given a dataset `name` and `reference product`.
        Store a copy for each REMIND region.
        If a REMIND region does not find a fitting ecoinvent location,
        fetch a dataset with a "RoW" location.
        Delete original datasets from the database.

        :return:
        """
        d_map = {
            self.geo.ecoinvent_to_remind_location(d['location']): d['location']
            for d in ws.get_many(
                self.db,
                ws.equals("name", name),
                ws.equals("reference product", ref_prod)
            )
        }

        list_remind_regions = [
            c[1] for c in self.geo.geo.keys()
            if type(c) == tuple and c[0] == "REMIND"
        ]

        d_remind_to_eco = {r: d_map.get(r, "RoW") for r in list_remind_regions}

        d_act = {}

        for d in d_remind_to_eco:
            try:
                ds = ws.get_one(
                    self.db,
                    ws.equals("name", name),
                    ws.equals("reference product", ref_prod),
                    ws.equals("location", d_remind_to_eco[d]),
                )

                d_act[d] = copy.deepcopy(ds)
                d_act[d]["location"] = d
                d_act[d]["code"] = str(uuid.uuid4().hex)
            except ws.NoResults:
                continue

            for prod in ws.production(d_act[d]):
                prod['location'] = d

        deleted_markets = [
            (act['name'], act['reference product'], act['location']) for act in self.db
                   if (act["name"], act['reference product']) == (name, ref_prod)
        ]

        with open(DATA_DIR / "logs/log deleted cement datasets.csv", "a") as csv_file:
                writer = csv.writer(csv_file,
                                    delimiter=';',
                                    lineterminator='\n')
                for line in deleted_markets:
                    writer.writerow(line)

        # Remove old datasets
        self.db = [act for act in self.db
                   if (act["name"], act['reference product']) != (name, ref_prod)]


        return d_act

    @staticmethod
    def remove_exchanges(dict, list_exc):

        keep = lambda x: {
            k: v
            for k, v in x.items()
            if not any(ele in x["name"] for ele in list_exc)
        }

        for r in dict:
            dict[r]["exchanges"] = [keep(exc) for exc in dict[r]["exchanges"]]

        return dict

    def get_suppliers_of_a_region(
            self, remind_region, ecoinvent_technologies, reference_product
    ):
        """
        Return a list of datasets which location and name correspond to the region, name and reference product given,
        respectively.

        :param remind_region: a REMIND region
        :type remind_region: str
        :param ecoinvent_technologies: list of names of ecoinvent dataset
        :type ecoinvent_technologies: list
        :param reference_product: reference product
        :type reference_product: str
        :return: list of wurst datasets
        :rtype: list
        """
        return ws.get_many(
            self.db,
            *[
                ws.either(
                    *[
                        ws.equals("name", supplier)
                        for supplier in ecoinvent_technologies
                    ]
                ),
                ws.either(
                    *[
                        ws.equals("location", loc)
                        for loc in self.geo.remind_to_ecoinvent_location(remind_region)
                    ]
                ),
                ws.equals("unit", "kilogram"),
                ws.equals("reference product", reference_product),
            ]
        )

    @staticmethod
    def get_shares_from_production_volume(ds):
        """
        Return shares of supply based on production volumes
        :param ds: list of datasets
        :return: dictionary with (dataset name, dataset location) as keys, shares as values. Shares total 1.
        :rtype: dict
        """
        dict_act = {}
        total_production_volume = 0
        for act in ds:
            for exc in ws.production(act):
                dict_act[(act["name"], act["location"], act["reference product"], act["unit"])] = float(
                    exc["production volume"]
                )
                total_production_volume += float(exc["production volume"])

        for d in dict_act:
            dict_act[d] /= total_production_volume

        return dict_act

    def update_pollutant_emissions(self, ds):
        """
        Update pollutant emissions based on GAINS data.
        :return:
        """

        # Update biosphere exchanges according to GAINS emission values
        for exc in ws.biosphere(
                ds, ws.either(*[ws.contains("name", x) for x in self.emissions_map])
            ):
            remind_emission_label = self.emissions_map[exc["name"]]

            try:
                remind_emission = self.rmd.cement_emissions.loc[
                    dict(
                        region=ds["location"],
                        pollutant=remind_emission_label
                    )
                ].values.item(0)
            except KeyError:
                # TODO: fix this.
                # GAINS does not have a 'World' region, hence w euse China as a temporary fix
                remind_emission = self.rmd.cement_emissions.loc[
                    dict(
                        region='CHA',
                        pollutant=remind_emission_label
                    )
                ].values.item(0)


            if exc["amount"] == 0:
                wurst.rescale_exchange(
                    exc, remind_emission / 1, remove_uncertainty=True
                )
            else:
                wurst.rescale_exchange(exc, remind_emission / exc["amount"])
        return ds

    def build_clinker_market_datasets(self):
        # Fetch clinker market activities and store them in a dictionary
        return self.fetch_proxies('market for clinker', 'clinker')

    def build_clinker_production_datasets(self):
        """
        Builds clinker production datasets for each REMIND region.
        Add CO2 capture and Storage if needed.
        Source for Co2 capture and compression: https://www.sciencedirect.com/science/article/pii/S1750583613001230?via%3Dihub#fn0040
        :return: a dictionary with REMIND regions as keys and clinker production datasets as values.
        :rtype: dict
        """

        # Fetch clinker production activities and store them in a dictionary
        d_act_clinker = self.fetch_proxies('clinker production', 'clinker')

        # Fuel exchanges to remove
        list_fuels = ["diesel", "coal", "lignite", "coke", "fuel", "meat", "gas", "oil", "electricity"]

        # Remove fuel and electricity exchanges in each activity
        d_act_clinker = self.remove_exchanges(d_act_clinker, list_fuels)

        for k, v in d_act_clinker.items():
            # Production volume by kiln type
            energy_input_per_kiln_type = self.rmd.gnr_data.sel(
                region=k,
                variables=[
                    v
                    for v in self.rmd.gnr_data.variables.values
                    if "Production volume share" in v
                ]
            ).clip(0, 1)
            # Energy input per ton of clinker, in MJ, per kiln type
            energy_input_per_kiln_type /= energy_input_per_kiln_type.sum(axis=0)

            energy_eff_per_kiln_type = self.rmd.gnr_data.sel(
                region=k,
                variables=[
                    v
                    for v in self.rmd.gnr_data.variables.values
                    if "Thermal energy consumption" in v
                ]
            )

            # Weighted average energy input per ton clinker, in MJ
            energy_input_per_ton_clinker = (
                    energy_input_per_kiln_type.values * energy_eff_per_kiln_type.values
            )

            # Fuel mix (waste, biomass, fossil)
            fuel_mix = self.rmd.gnr_data.sel(
                variables=[
                    "Share waste fuel",
                    "Share biomass fuel",
                    "Share fossil fuel",
                ],
                region=k
            ).clip(0, 1)

            fuel_mix /= fuel_mix.sum(axis=0)

            # Calculate quantities (in kg) of fuel, per type of fuel, per ton of clinker
            # MJ per ton of clinker * fuel mix * (1 / lower heating value)
            fuel_qty_per_type = (
                    energy_input_per_ton_clinker.sum()
                    * fuel_mix
                    * 1
                    / np.array(
                [
                    float(self.fuels_lhv["waste"]),
                    float(self.fuels_lhv["wood pellet"]),
                    float(self.fuels_lhv["hard coal"]),
                ]
            )
            )

            fuel_fossil_co2_per_type = (
                    energy_input_per_ton_clinker.sum()
                    * fuel_mix
                    * np.array(
                [
                    (
                            self.fuels_co2["waste"]["co2"]
                            * (1 - self.fuels_co2["waste"]["bio_share"])
                    ),
                    (
                            self.fuels_co2["wood pellet"]["co2"]
                            * (1 - self.fuels_co2["wood pellet"]["bio_share"])
                    ),
                    (
                            self.fuels_co2["hard coal"]["co2"]
                            * (1 - self.fuels_co2["hard coal"]["bio_share"])
                    ),
                ]
            )
            )

            fuel_biogenic_co2_per_type = (
                    energy_input_per_ton_clinker.sum()
                    * fuel_mix
                    * np.array(
                [
                    (
                            self.fuels_co2["waste"]["co2"]
                            * (self.fuels_co2["waste"]["bio_share"])
                    ),
                    (
                            self.fuels_co2["wood pellet"]["co2"]
                            * (self.fuels_co2["wood pellet"]["bio_share"])
                    ),
                    (
                            self.fuels_co2["hard coal"]["co2"]
                            * (self.fuels_co2["hard coal"]["bio_share"])
                    ),
                ]
            )
            )

            for fuel in [('waste', 'waste plastic, mixture', 'EUR'),
                         ('wood pellet', 'wood pellet, measured as dry mass', 'EUR'),
                         ('hard coal', 'hard coal', 'REF')]:
                # Select waste fuel providers, fitting the REMIND region
                # Fetch respective shares based on production volumes
                fuel_suppliers = self.get_shares_from_production_volume(
                    self.get_suppliers_of_a_region(k,
                                                   self.fuel_map[fuel[0]],
                                                   fuel[1]))
                if len(fuel_suppliers) == 0:
                    fuel_suppliers = self.get_shares_from_production_volume(
                        self.get_suppliers_of_a_region(fuel[2],
                                                       self.fuel_map[fuel[0]],
                                                       fuel[1]))

                # Append it to the dataset exchanges
                # Append it to the dataset exchanges
                new_exchanges = []
                for supplier in fuel_suppliers:
                    new_exchanges.append(
                        {
                            "uncertainty type": 0,
                            "loc": 1,
                            "amount": (fuel_suppliers[supplier] * fuel_qty_per_type[2].values) / 1000,
                            "type": "technosphere",
                            "production volume": 0,
                            "product": supplier[2],
                            "name": supplier[0],
                            "unit": supplier[3],
                            "location": supplier[1],
                        }
                    )
                v["exchanges"].extend(new_exchanges)

            v['exchanges'] = [v for v in v["exchanges"] if v]

            # Add carbon capture-related energy exchanges
            # Carbon capture rate: share of total CO2 captured
            carbon_capture_rate = (self.rmd.data.sel(
                variables='Emi|CCO2|FFaI|Industry|Cement',
                region=k
            ).interp(year=self.year) / self.rmd.data.sel(
                variables='Emi|CO2|FFaI|Industry|Cement',
                region=k
            ).interp(year=self.year)).values

            if carbon_capture_rate > 0:

                # CO2 effectively captured per kg of clinker
                carbon_capture_abs = carbon_capture_rate * ((fuel_biogenic_co2_per_type.sum().values
                                                             + fuel_fossil_co2_per_type.sum().values + 525)
                                                            / 1000)

                # Electricity: 0.024 kWh/kg CO2 for capture, 0.146 kWh/kg CO2 for compression
                carbon_capture_electricity = carbon_capture_abs * (0.146 + 0.024)
                new_exchanges = []
                new_exchanges.append(
                            {
                                "uncertainty type": 0,
                                "loc": 1,
                                "amount": carbon_capture_electricity,
                                "type": "technosphere",
                                "production volume": 0,
                                "product": 'electricity, medium voltage',
                                "name": 'market group for electricity, medium voltage',
                                "unit": 'kilowatt hour',
                                "location": k,
                            }
                        )

                # Heat, as steam: 3.48 MJ/kg CO2 captured, minus excess heat generated on site
                excess_heat_generation = self.rmd.gnr_data.sel(
                    variables='Share of recovered energy, per ton clinker',
                    region=k
                ).values * energy_input_per_ton_clinker.sum()

                carbon_capture_heat = (carbon_capture_abs * 3.48) - (excess_heat_generation / 1000)

                new_exchanges.append(
                            {
                                "uncertainty type": 0,
                                "loc": 1,
                                "amount": carbon_capture_heat,
                                "type": "technosphere",
                                "production volume": 0,
                                "product": 'heat, from steam, in chemical industry',
                                "name": 'steam production, as energy carrier, in chemical industry',
                                "unit": 'megajoule',
                                "location": 'RoW',
                            }
                        )

                v["exchanges"].extend(new_exchanges)

            else:
                carbon_capture_rate = 0


            # Update fossil CO2 exchange, add 525 kg of fossil CO_2 from calcination, minus CO2 captured
            fossil_co2_exc = [e for e in v["exchanges"] if e['name'] == 'Carbon dioxide, fossil'][0]
            fossil_co2_exc['amount'] = ((fuel_fossil_co2_per_type.sum().values + 525) / 1000) * (1 - carbon_capture_rate)
            fossil_co2_exc['uncertainty type'] = 0

            try:
                # Update biogenic CO2 exchange, minus CO2 captured
                biogenic_co2_exc = [e for e in v["exchanges"] if e['name'] == 'Carbon dioxide, non-fossil'][0]
                biogenic_co2_exc['amount'] = (fuel_biogenic_co2_per_type.sum().values / 1000) * (1 - carbon_capture_rate)
                biogenic_co2_exc['uncertainty type'] = 0
            except:
                # There isn't a biogenic CO2 emissions exchange
                biogenic_co2_exc = {
                    "uncertainty type": 0,
                    "loc": 1,
                    "amount": (fuel_biogenic_co2_per_type.sum().values / 1000) * (1 - carbon_capture_rate),
                    "type": "biosphere",
                    "production volume": 0,
                    "name": "Carbon dioxide, non-fossil",
                    "unit": "kilogram",
                    "input": ('biosphere3', 'eba59fd6-f37e-41dc-9ca3-c7ea22d602c7'),
                    "categories": ('air',),
                }
                v["exchanges"].append(biogenic_co2_exc)


        d_act_clinker = {k:self.update_pollutant_emissions(v) for k,v in d_act_clinker.items()}

        return d_act_clinker

    def relink_datasets(self, name, ref_product):
        """
        For a given dataset name, change its location to a REMIND location,
        to effectively link the newly built dataset(s).

        :param ref_product:
        :param name: dataset name
        :type name: str
        """
        list_remind_regions = [
            c[1] for c in self.geo.geo.keys() if type(c) == tuple and c[0] == "REMIND"
        ]

        for act in self.db:
            for exc in act['exchanges']:
                if (exc['name'], exc.get('product')) == (name, ref_product) and exc['type'] == 'technosphere':
                    if not exc['location'] in list_remind_regions:
                        old_location = exc['location']
                        exc['location'] = self.geo.ecoinvent_to_remind_location(old_location)

    def adjust_clinker_ratio(self, d_act):
        """ Adjust the cement suppliers composition for "cement, unspecified", in order to reach
        the average clinker-to-cement ratio given by REMIND.

        The supply of the cement with the highest clinker-to-cement ratio is decreased by 1% to the favor of
        the supply of the cement with the lowest clinker-to-cement ratio, and the average clinker-to-cement ratio
        is calculated.

        This operation is repeated until the average clinker-to-cement ratio aligns with that given by REMIND.
        When the supply of the cement with the highest clinker-to-cement ratio goes below 1%,
        the cement with the second highest clinker-to-cement ratio becomes affected and so forth.

        """

        for d in d_act:

            ratio_to_reach = self.clinker_ratio_remind.sel(dict(
                region=d
            )).values

            share = []
            ratio = []

            for exc in d_act[d]['exchanges']:
                if 'cement' in exc['product'] and exc['type'] == "technosphere":
                    share.append(exc['amount'])
                    ratio.append(self.clinker_ratio_eco[(exc['name'], exc['location'])])

            share = np.array(share)
            ratio = np.array(ratio)

            average_ratio = (share * ratio).sum()

            while average_ratio > ratio_to_reach:
                share[share == 0] = np.nan

                ratio = np.where(share >= 0.001, ratio, np.nan)

                highest_ratio = np.nanargmax(ratio)
                lowest_ratio = np.nanargmin(ratio)

                share[highest_ratio] -= .01
                share[lowest_ratio] += .01

                average_ratio = (np.nan_to_num(ratio) * np.nan_to_num(share)).sum()

            share = np.nan_to_num(share)

            count = 0
            for exc in d_act[d]['exchanges']:
                if 'cement' in exc['product'] and exc['type'] == "technosphere":
                    exc['amount'] = share[count]
                    count += 1

        return d_act

    def update_cement_production_datasets(self, name, ref_prod):
        """
        Update electricity use (mainly for grinding).
        Update clinker-to-cement ratio.
        Update use of cementitious supplementary materials.

        :return:
        """
        # Fetch proxies
        # Delete old datasets
        d_act_cement = self.fetch_proxies(name, ref_prod)
        # Update electricity use
        d_act_cement = self.update_electricity_exchanges(d_act_cement)

        return d_act_cement

    def update_electricity_exchanges(self, d_act):
        """
        Update electricity exchanges in cement production datasets.
        Electricity consumption equals electricity use minus on-site electricity generation from excess heat recovery.

        :return:
        """

        for act in d_act:
            for exc in ws.technosphere(d_act[act]):
                if exc['unit'] == 'kilowatt hour':
                    exc['amount'] = (self.rmd.gnr_data.loc[dict(
                        variables='Power consumption',
                        region=act
                    )].values - self.rmd.gnr_data.loc[dict(
                        variables='Power generation',
                        region=act
                    )].values) / 1000
        return d_act



    def add_datasets_to_database(self):

        print('Log of deleted cement datasets saved in {}'.format(DATA_DIR / 'logs'))
        print('Log of created cement datasets saved in {}'.format(DATA_DIR / 'logs'))

        with open(DATA_DIR / "logs/log deleted cement datasets.csv", "w") as csv_file:
            writer = csv.writer(csv_file,
                                delimiter=';',
                                lineterminator='\n')
            writer.writerow(['dataset name', 'reference product', 'location'])

        with open(DATA_DIR / "logs/log created cement datasets.csv", "w") as csv_file:
            writer = csv.writer(csv_file,
                                delimiter=';',
                                lineterminator='\n')
            writer.writerow(['dataset name', 'reference product', 'location'])

        print('Create new clinker production datasets and delete old datasets')
        clinker_prod_datasets = [d for d in self.build_clinker_production_datasets().values()]
        self.db.extend(clinker_prod_datasets)

        created_datasets = [(act['name'], act['reference product'], act['location'])
                            for act in clinker_prod_datasets]

        print('Create new clinker market datasets and delete old datasets')
        clinker_market_datasets = [d for d in self.build_clinker_market_datasets().values()]
        self.db.extend(clinker_market_datasets)

        created_datasets.extend([(act['name'], act['reference product'], act['location'])
                            for act in clinker_market_datasets])

        print('Adjust clinker-to-cement ratio in "unspecified cement" datasets')

        if self.version == 3.5:
            name = 'market for cement, unspecified'
            ref_prod = 'cement, unspecified'

        else:
            name = 'cement, all types to generic market for cement, unspecified'
            ref_prod = 'cement, unspecified'

        act_cement_unspecified = self.fetch_proxies(name, ref_prod)
        act_cement_unspecified = self.adjust_clinker_ratio(act_cement_unspecified)
        self.db.extend([v for v in act_cement_unspecified.values()])
        self.relink_datasets(name, ref_prod)

        created_datasets.extend([(act['name'], act['reference product'], act['location'])
                            for act in act_cement_unspecified.values()])

        print('Create new cement production datasets and adjust electricity consumption')

        if self.version == 3.5:
            for i in (
                ("cement production, alternative constituents 21-35%","cement, alternative constituents 21-35%"),
                ("cement production, alternative constituents 6-20%","cement, alternative constituents 6-20%"),
                ("cement production, blast furnace slag 18-30% and 18-30% other alternative constituents",
                 "cement, blast furnace slag 18-30% and 18-30% other alternative constituents"),
                ("cement production, blast furnace slag 25-70%, US only","cement, blast furnace slag 25-70%, US only"),
                ("cement production, blast furnace slag 31-50% and 31-50% other alternative constituents",
                 "cement, blast furnace slag 31-50% and 31-50% other alternative constituents"),
                ("cement production, blast furnace slag 36-65%, non-US","cement, blast furnace slag 36-65%, non-US"),
                ("cement production, blast furnace slag 5-25%, US only","cement, blast furnace slag 5-25%, US only"),
                ("cement production, blast furnace slag 70-100%, non-US","cement, blast furnace slag 70-100%, non-US"),
                ("cement production, blast furnace slag 70-100%, US only","cement, blast furnace slag 70-100%, US only"),
                ("cement production, blast furnace slag 81-95%, non-US","cement, blast furnace slag 81-95%, non-US"),
                ("cement production, blast furnace slag, 66-80%, non-US","cement, blast furnace slag, 66-80%, non-US"),
                ("cement production, Portland","cement, Portland"),
                ("cement production, pozzolana and fly ash 11-35%, non-US","cement, pozzolana and fly ash 11-35%, non-US"),
                ("cement production, pozzolana and fly ash 15-40%, US only","cement, pozzolana and fly ash 15-40%, US only"),
                ("cement production, pozzolana and fly ash 36-55%,non-US","cement, pozzolana and fly ash 36-55%,non-US"),
                ("cement production, pozzolana and fly ash 5-15%, US only","cement, pozzolana and fly ash 5-15%, US only")
            ):
                act_cement = self.update_cement_production_datasets(i[0], i[1])
                self.db.extend([v for v in act_cement.values()])

                created_datasets.extend([(act['name'], act['reference product'], act['location'])
                                for act in act_cement.values()])
                self.relink_datasets(i[0], i[1])
                
            print('Create new cement market datasets')

            for i in (("market for cement, alternative constituents 21-35%","cement, alternative constituents 21-35%"),
                ("market for cement, alternative constituents 6-20%","cement, alternative constituents 6-20%"),
                ("market for cement, blast furnace slag 18-30% and 18-30% other alternative constituents",
                 "cement, blast furnace slag 18-30% and 18-30% other alternative constituents"),
                ("market for cement, blast furnace slag 25-70%, US only","cement, blast furnace slag 25-70%, US only"),
                ("market for cement, blast furnace slag 31-50% and 31-50% other alternative constituents",
                 "cement, blast furnace slag 31-50% and 31-50% other alternative constituents"),
                ("market for cement, blast furnace slag 36-65%, non-US","cement, blast furnace slag 36-65%, non-US"),
                ("market for cement, blast furnace slag 5-25%, US only","cement, blast furnace slag 5-25%, US only"),
                ("market for cement, blast furnace slag 70-100%, non-US","cement, blast furnace slag 70-100%, non-US"),
                ("market for cement, blast furnace slag 70-100%, US only","cement, blast furnace slag 70-100%, US only"),
                ("market for cement, blast furnace slag 81-95%, non-US","cement, blast furnace slag 81-95%, non-US"),
                ("market for cement, blast furnace slag, 66-80%, non-US","cement, blast furnace slag, 66-80%, non-US"),
                ("market for cement, Portland","cement, Portland"),
                ("market for cement, pozzolana and fly ash 11-35%, non-US","cement, pozzolana and fly ash 11-35%, non-US"),
                ("market for cement, pozzolana and fly ash 15-40%, US only","cement, pozzolana and fly ash 15-40%, US only"),
                ("market for cement, pozzolana and fly ash 36-55%,non-US","cement, pozzolana and fly ash 36-55%,non-US"),
                ("market for cement, pozzolana and fly ash 5-15%, US only","cement, pozzolana and fly ash 5-15%, US only")
                      ):
                act_cement = self.fetch_proxies(i[0], i[1])
                self.db.extend([v for v in act_cement.values()])
                created_datasets.extend([(act['name'], act['reference product'], act['location'])
                            for act in act_cement.values()])

                self.relink_datasets(i[0], i[1])

        else:
            for i in (
                      ("cement production, Portland", "cement, Portland"),
                      ("cement production, blast furnace slag 35-70%", "cement, blast furnace slag 35-70%"),
                      ("cement production, blast furnace slag 6-34%", "cement, blast furnace slag 6-34%"),
                      ("cement production, limestone 6-10%", "cement, limestone 6-10%"),
                      ("cement production, pozzolana and fly ash 15-50%", "cement, pozzolana and fly ash 15-50%"),
                      ("cement production, pozzolana and fly ash 6-14%", "cement, pozzolana and fly ash 6-14%"),
                      ("cement production, alternative constituents 6-20%", "cement, alternative constituents 6-20%"),
                      ("cement production, alternative constituents 21-35%", "cement, alternative constituents 21-35%"),
                      ("cement production, blast furnace slag 18-30% and 18-30% other alternative constituents",
                       "cement, blast furnace slag 18-30% and 18-30% other alternative constituents"),
                      ("cement production, blast furnace slag 31-50% and 31-50% other alternative constituents",
                       "cement, blast furnace slag 31-50% and 31-50% other alternative constituents"),
                      ("cement production, blast furnace slag 36-65%", "cement, blast furnace slag 36-65%"),
                      ("cement production, blast furnace slag 66-80%", "cement, blast furnace slag, 66-80%"),
                      ("cement production, blast furnace slag 81-95%", "cement, blast furnace slag 81-95%"),
                      ("cement production, pozzolana and fly ash 11-35%", "cement, pozzolana and fly ash 11-35%"),
                      ("cement production, pozzolana and fly ash 36-55%", "cement, pozzolana and fly ash 36-55%"),
                      ("cement production, alternative constituents 45%", "cement, alternative constituents 45%"),
                      ("cement production, blast furnace slag 40-70%", "cement, blast furnace 40-70%"),
                      ("cement production, pozzolana and fly ash 25-35%", "cement, pozzolana and fly ash 25-35%"),
                      ("cement production, limestone 21-35%", "cement, limestone 21-35%"),
                      ("cement production, blast furnace slag 21-35%", "cement, blast furnace slag 21-35%"),
                      ("cement production, blast furnace slag 25-70%", "cement, blast furnace slag 25-70%"),
                      ("cement production, blast furnace slag 5-25%", "cement, blast furnace slag 5-25%"),
                      ("cement production, blast furnace slag 6-20%", "cement, blast furnace slag 6-20%"),
                      ("cement production, blast furnace slag 70-100%", "cement, blast furnace slag 70-100%"),
                      ("cement production, pozzolana and fly ash 15-40%", "cement, pozzolana and fly ash 15-40%"),
                      ("cement production, pozzolana and fly ash 5-15%", "cement, pozzolana and fly ash 5-15%"),
                      ):
                act_cement = self.update_cement_production_datasets(i[0], i[1])
                self.db.extend([v for v in act_cement.values()])

                created_datasets.extend([(act['name'], act['reference product'], act['location'])
                                for act in act_cement.values()])

                self.relink_datasets(i[0], i[1])

            print('Create new cement market datasets')

            for i in (("market for cement, Portland", "cement, Portland"),
                      ("market for cement, blast furnace slag 35-70%", "cement, blast furnace slag 35-70%"),
                      ("market for cement, blast furnace slag 6-34%", "cement, blast furnace slag 6-34%"),
                      ("market for cement, limestone 6-10%", "cement, limestone 6-10%"),
                      ("market for cement, pozzolana and fly ash 15-50%", "cement, pozzolana and fly ash 15-50%"),
                      ("market for cement, pozzolana and fly ash 6-14%", "cement, pozzolana and fly ash 6-14%"),
                      ("market for cement, alternative constituents 6-20%", "cement, alternative constituents 6-20%"),
                      ("market for cement, alternative constituents 21-35%", "cement, alternative constituents 21-35%"),
                      ("market for cement, blast furnace slag 18-30% and 18-30% other alternative constituents",
                       "cement, blast furnace slag 18-30% and 18-30% other alternative constituents"),
                      ("market for cement, blast furnace slag 31-50% and 31-50% other alternative constituents",
                       "cement, blast furnace slag 31-50% and 31-50% other alternative constituents"),
                      ("market for cement, blast furnace slag 36-65%", "cement, blast furnace slag 36-65%"),
                      ("market for cement, blast furnace slag 66-80%", "cement, blast furnace slag, 66-80%"),
                      ("market for cement, blast furnace slag 81-95%", "cement, blast furnace slag 81-95%"),
                      ("market for cement, pozzolana and fly ash 11-35%", "cement, pozzolana and fly ash 11-35%"),
                      ("market for cement, pozzolana and fly ash 36-55%", "cement, pozzolana and fly ash 36-55%"),
                      ("market for cement, alternative constituents 45%", "cement, alternative constituents 45%"),
                      ("market for cement, blast furnace slag 40-70%", "cement, blast furnace 40-70%"),
                      ("market for cement, pozzolana and fly ash 25-35%", "cement, pozzolana and fly ash 25-35%"),
                      ("market for cement, limestone 21-35%", "cement, limestone 21-35%"),
                      ("market for cement, blast furnace slag 21-35%", "cement, blast furnace slag 21-35%"),
                      ("market for cement, blast furnace slag 25-70%", "cement, blast furnace slag 25-70%"),
                      ("market for cement, blast furnace slag 5-25%", "cement, blast furnace slag 5-25%"),
                      ("market for cement, blast furnace slag 6-20%", "cement, blast furnace slag 6-20%"),
                      ("market for cement, blast furnace slag 70-100%", "cement, blast furnace slag 70-100%"),
                      ("market for cement, pozzolana and fly ash 15-40%", "cement, pozzolana and fly ash 15-40%"),
                      ("market for cement, pozzolana and fly ash 5-15%", "cement, pozzolana and fly ash 5-15%"),
                      ):
                act_cement = self.fetch_proxies(i[0], i[1])
                self.db.extend([v for v in act_cement.values()])

                created_datasets.extend([(act['name'], act['reference product'], act['location'])
                            for act in act_cement.values()])

                self.relink_datasets(i[0], i[1])


        with open(DATA_DIR / "logs/log created cement datasets.csv", "a") as csv_file:
                writer = csv.writer(csv_file,
                                    delimiter=';',
                                    lineterminator='\n')
                for line in created_datasets:
                    writer.writerow(line)

        print('Relink cement production datasets to new clinker production datasets')
        self.relink_datasets('clinker production', 'clinker')
        print('Relink cement production datasets to new clinker market datasets')
        self.relink_datasets('market for clinker', 'clinker')

        print('Relink cement market datasets to new cement production datasets')
        self.relink_datasets('market for cement', 'cement')

        print('Relink activities to new cement datasets')
        self.relink_datasets('cement, all types to generic market for cement, unspecified',
                             'cement, unspecified')

        return self.db
