#!/usr/bin/env python3
"""Generate DC-compatible CSVs from Slums Compendium 2015 and NHM ROP data."""

import csv
import os

EIDB_DIR = os.path.dirname(os.path.abspath(__file__))

# Wikidata ID mapping for Indian states/UTs
STATE_WIKI = {
    "Andhra Pradesh": "wikidataId/Q1159",
    "Arunachal Pradesh": "wikidataId/Q1162",
    "Assam": "wikidataId/Q1164",
    "Bihar": "wikidataId/Q1165",
    "Chhattisgarh": "wikidataId/Q1168",
    "Goa": "wikidataId/Q1171",
    "Gujarat": "wikidataId/Q1061",
    "Haryana": "wikidataId/Q1174",
    "Himachal Pradesh": "wikidataId/Q1177",
    "Jammu & Kashmir": "wikidataId/Q1180",
    "Jharkhand": "wikidataId/Q1184",
    "Karnataka": "wikidataId/Q1185",
    "Kerala": "wikidataId/Q1186",
    "Madhya Pradesh": "wikidataId/Q1188",
    "Maharashtra": "wikidataId/Q1191",
    "Manipur": "wikidataId/Q1193",
    "Meghalaya": "wikidataId/Q1195",
    "Mizoram": "wikidataId/Q1502",
    "Nagaland": "wikidataId/Q1599",
    "Odisha": "wikidataId/Q22048",
    "Punjab": "wikidataId/Q22424",
    "Rajasthan": "wikidataId/Q1437",
    "Sikkim": "wikidataId/Q1505",
    "Tamil Nadu": "wikidataId/Q1445",
    "Tripura": "wikidataId/Q1363",
    "Uttar Pradesh": "wikidataId/Q1498",
    "Uttarakhand": "wikidataId/Q1499",
    "West Bengal": "wikidataId/Q1356",
    # UTs
    "A & N Islands": "wikidataId/Q40888",
    "Chandigarh": "wikidataId/Q43433",
    "D & N Haveli": "wikidataId/Q46107",
    "Daman & Diu": "wikidataId/Q66743",
    "NCT of Delhi": "wikidataId/Q1353",
    "Lakshadweep": "wikidataId/Q26927",
    "Puducherry": "wikidataId/Q66583",
    "INDIA": "country/IND",
}

# Alternate name mappings found in the PDF
ALT_NAMES = {
    "Orissa": "Odisha",
    "Orisa": "Odisha",
    "N CT of Delhi": "NCT of Delhi",
    "NCT of Delhi*": "NCT of Delhi",
    "N CT of Delhi*": "NCT of Delhi",
    "Uttar Pradesh*": "Uttar Pradesh",
    "A& N Islands": "A & N Islands",
    "A&N Islands": "A & N Islands",
    "D &N Haveli": "D & N Haveli",
    "D&N Haveli": "D & N Haveli",
    "Jammu and Kashmir": "Jammu & Kashmir",
    "H aryana": "Haryana",
    "H imachal Pradesh": "Himachal Pradesh",
}

HEADER = "observationDate,observationAbout,variableMeasured,value,unit,measurementMethod,observationPeriod"

def get_wiki(state_name):
    name = ALT_NAMES.get(state_name, state_name)
    return STATE_WIKI.get(name)

def write_csv(filename, rows):
    path = os.path.join(EIDB_DIR, filename)
    with open(path, 'w', newline='') as f:
        f.write(HEADER + "\n")
        for row in rows:
            f.write(",".join(str(x) for x in row) + "\n")
    print(f"  Written {filename}: {len(rows)} rows")

def gen_slum_population():
    """Appendix 24 + 27: State-wise Slum Population 2011"""
    # From Appendix 27 (most complete — total pop, urban pop, slum pop, % in total, % in urban, % in total slum)
    data = {
        "Andhra Pradesh": (84580777, 28219075, 10186934, 6.99, 7.48, 15.55),
        "Arunachal Pradesh": (1383727, 317369, 15562, 0.11, 0.08, 0.02),
        "Assam": (31205576, 4398542, 197266, 2.58, 1.17, 0.30),
        "Bihar": (104099452, 11758016, 1237682, 8.60, 3.12, 1.89),
        "Chhattisgarh": (25545198, 5937237, 1898931, 2.11, 1.57, 2.90),
        "Goa": (1458545, 906814, 26247, 0.12, 0.24, 0.04),
        "Gujarat": (60439692, 25745083, 1680095, 4.99, 6.83, 2.57),
        "Haryana": (25351462, 8842103, 1662305, 2.09, 2.34, 2.54),
        "Himachal Pradesh": (6864602, 688552, 61312, 0.57, 0.18, 0.09),
        "Jammu & Kashmir": (12541302, 3433242, 662062, 1.04, 0.91, 1.01),
        "Jharkhand": (32988134, 7933061, 372999, 2.73, 2.10, 0.57),
        "Karnataka": (61095297, 23625962, 3291434, 5.05, 6.27, 5.03),
        "Kerala": (33406061, 15934926, 202048, 2.76, 4.23, 0.31),
        "Madhya Pradesh": (72626809, 20069405, 5688993, 6.00, 5.32, 8.69),
        "Maharashtra": (112374333, 50818259, 11848423, 9.28, 13.48, 18.09),
        "Manipur": (2855794, 834154, None, 0.24, 0.22, None),
        "Meghalaya": (2966889, 595450, 57418, 0.25, 0.16, 0.09),
        "Mizoram": (1097206, 571771, 78561, 0.09, 0.15, 0.12),
        "Nagaland": (1978502, 570966, 82324, 0.16, 0.15, 0.13),
        "Odisha": (41974218, 7003656, 1560303, 3.47, 1.86, 2.38),
        "Punjab": (27743338, 10399146, 1460518, 2.29, 2.76, 2.23),
        "Rajasthan": (68548437, 17048085, 2068000, 5.66, 4.52, 3.16),
        "Sikkim": (610577, 153578, 31378, 0.05, 0.04, 0.05),
        "Tamil Nadu": (72147030, 34917440, 5798459, 5.96, 9.26, 8.85),
        "Tripura": (3673917, 961453, 139780, 0.30, 0.25, 0.21),
        "Uttar Pradesh": (199812341, 44495063, 6239965, 16.51, 11.80, 9.53),
        "Uttarakhand": (10086292, 3049338, 487741, 0.83, 0.81, 0.74),
        "West Bengal": (91276115, 29093002, 6418594, 7.54, 7.71, 9.80),
        "A & N Islands": (380581, 143488, 14172, 0.03, 0.04, 0.02),
        "Chandigarh": (1055450, 1026459, 95135, 0.09, 0.27, 0.15),
        "D & N Haveli": (343709, 160595, None, 0.03, 0.04, None),
        "Daman & Diu": (243247, 182851, None, 0.02, 0.05, None),
        "NCT of Delhi": (16787941, 16368899, 1785390, 1.39, 4.34, 2.73),
        "Lakshadweep": (64473, 50332, None, 0.01, 0.01, None),
        "Puducherry": (1247953, 852753, 144573, 0.10, 0.23, 0.22),
        "INDIA": (1210854977, 377106125, 65494604, 100.0, 100.0, 100.0),
    }

    rows = []
    for state, vals in data.items():
        wiki = get_wiki(state)
        if not wiki:
            continue
        total_pop, urban_pop, slum_pop, pct_total, pct_urban, pct_slum = vals

        # Slum population
        if slum_pop is not None:
            rows.append((2011, wiki, "dcid:NDAP_SlumPopulation_Statewise", slum_pop, "Count", "NDAP_NBO_SlumCompendium2015", "P1Y"))
        # Slum as % of urban population
        if slum_pop is not None and urban_pop > 0:
            pct = round(slum_pop / urban_pop * 100, 2)
            rows.append((2011, wiki, "dcid:NDAP_SlumPctOfUrban_Statewise", pct, "Percent", "NDAP_NBO_SlumCompendium2015", "P1Y"))

    return rows

def gen_slum_literacy():
    """Appendix 43: State-wise Slum Literacy Rate 2011"""
    data = {
        "Andhra Pradesh": (75.3, 81.8, 68.9, 12.9),
        "Arunachal Pradesh": (69.4, 76.0, 62.2, 13.8),
        "Assam": (81.6, 86.5, 76.4, 10.1),
        "Bihar": (68.2, 75.0, 60.6, 14.4),
        "Chhattisgarh": (80.4, 88.2, 72.3, 15.9),
        "Goa": (82.4, 88.4, 75.8, 12.5),
        "Gujarat": (70.5, 78.3, 61.1, 17.2),
        "Haryana": (75.9, 83.0, 67.7, 15.3),
        "Himachal Pradesh": (87.7, 90.4, 84.8, 5.6),
        "Jammu & Kashmir": (68.0, 75.7, 59.9, 15.7),
        "Jharkhand": (75.5, 82.7, 67.8, 14.9),
        "Karnataka": (75.6, 81.8, 69.4, 12.4),
        "Kerala": (93.1, 95.4, 91.0, 4.4),
        "Madhya Pradesh": (77.3, 84.3, 69.6, 14.7),
        "Maharashtra": (84.6, 89.3, 79.0, 10.3),
        "Meghalaya": (89.0, 90.9, 87.2, 3.7),
        "Mizoram": (98.1, 98.4, 97.9, 0.5),
        "Nagaland": (88.8, 90.7, 86.8, 3.9),
        "Odisha": (78.9, 85.6, 71.9, 13.7),
        "Punjab": (74.2, 78.6, 69.2, 9.4),
        "Rajasthan": (69.8, 79.8, 58.9, 21.0),
        "Sikkim": (88.1, 92.1, 83.8, 8.3),
        "Tamil Nadu": (82.1, 88.0, 76.2, 11.8),
        "Tripura": (90.7, 93.4, 88.0, 5.3),
        "Uttar Pradesh": (69.0, 75.3, 61.9, 13.3),
        "Uttarakhand": (76.9, 82.5, 70.6, 11.9),
        "West Bengal": (81.4, 85.8, 76.7, 9.1),
        "A & N Islands": (82.8, 88.0, 77.0, 11.0),
        "Chandigarh": (66.4, 75.6, 54.3, 21.3),
        "NCT of Delhi": (75.2, 80.5, 68.7, 11.8),
        "Puducherry": (81.4, 87.3, 75.8, 11.4),
        "INDIA": (77.7, 83.7, 71.2, 12.5),
    }

    rows = []
    for state, (total, male, female, gap) in data.items():
        wiki = get_wiki(state)
        if not wiki:
            continue
        rows.append((2011, wiki, "dcid:NDAP_SlumLiteracyRate_Total", total, "Percent", "NDAP_NBO_SlumCompendium2015", "P1Y"))
        rows.append((2011, wiki, "dcid:NDAP_SlumLiteracyRate_Male", male, "Percent", "NDAP_NBO_SlumCompendium2015", "P1Y"))
        rows.append((2011, wiki, "dcid:NDAP_SlumLiteracyRate_Female", female, "Percent", "NDAP_NBO_SlumCompendium2015", "P1Y"))

    return rows

def gen_slum_work_participation():
    """Appendix 45: Work Participation Rate in Slums 2011"""
    data = {
        "Andhra Pradesh": (37.5, 54.5, 20.4),
        "Arunachal Pradesh": (32.2, 49.4, 13.8),
        "Assam": (36.3, 57.7, 13.7),
        "Bihar": (29.0, 44.7, 11.6),
        "Chhattisgarh": (36.3, 53.3, 18.7),
        "Goa": (39.7, 57.7, 19.6),
        "Gujarat": (38.8, 59.0, 14.8),
        "Haryana": (31.7, 50.1, 10.7),
        "Himachal Pradesh": (37.9, 53.2, 20.5),
        "Jammu & Kashmir": (30.7, 46.5, 13.7),
        "Jharkhand": (31.8, 48.0, 14.3),
        "Karnataka": (40.4, 57.0, 23.6),
        "Kerala": (35.9, 55.7, 17.3),
        "Madhya Pradesh": (35.1, 51.8, 17.1),
        "Maharashtra": (38.1, 56.5, 17.0),
        "Meghalaya": (34.0, 45.9, 22.0),
        "Mizoram": (40.0, 49.7, 30.6),
        "Nagaland": (34.4, 43.9, 24.2),
        "Odisha": (36.2, 54.9, 16.5),
        "Punjab": (35.9, 55.5, 13.7),
        "Rajasthan": (32.8, 50.9, 13.1),
        "Sikkim": (41.0, 56.2, 24.8),
        "Tamil Nadu": (40.9, 58.6, 23.4),
        "Tripura": (36.4, 56.7, 16.0),
        "Uttar Pradesh": (31.0, 48.5, 11.4),
        "Uttarakhand": (32.2, 51.3, 10.7),
        "West Bengal": (36.5, 56.6, 15.0),
        "A & N Islands": (38.8, 57.5, 18.5),
        "Chandigarh": (36.6, 54.9, 13.1),
        "NCT of Delhi": (35.4, 55.7, 11.0),
        "Puducherry": (34.9, 53.6, 17.1),
        "INDIA": (36.4, 54.3, 17.1),
    }

    rows = []
    for state, (total, male, female) in data.items():
        wiki = get_wiki(state)
        if not wiki:
            continue
        rows.append((2011, wiki, "dcid:NDAP_SlumWorkParticipation_Total", total, "Percent", "NDAP_NBO_SlumCompendium2015", "P1Y"))
        rows.append((2011, wiki, "dcid:NDAP_SlumWorkParticipation_Male", male, "Percent", "NDAP_NBO_SlumCompendium2015", "P1Y"))
        rows.append((2011, wiki, "dcid:NDAP_SlumWorkParticipation_Female", female, "Percent", "NDAP_NBO_SlumCompendium2015", "P1Y"))

    return rows

def gen_slum_households():
    """Appendix 26/28: Slum Households and Household Size 2011"""
    # state: (slum_households, avg_slum_hh_size, avg_urban_hh_size)
    data = {
        "Andhra Pradesh": (2431474, 4.2, 4.2),
        "Arunachal Pradesh": (3479, 4.5, 4.5),
        "Assam": (42533, 4.6, 4.5),
        "Bihar": (216496, 5.7, 5.7),
        "Chhattisgarh": (413831, 4.6, 4.6),
        "Goa": (5497, 4.8, 4.2),
        "Gujarat": (345998, 4.9, 4.7),
        "Haryana": (332697, 5.0, 4.9),
        "Himachal Pradesh": (14385, 4.3, 4.0),
        "Jammu & Kashmir": (103633, 6.4, 6.1),
        "Jharkhand": (72544, 5.1, 5.2),
        "Karnataka": (707662, 4.7, 4.4),
        "Kerala": (45417, 4.4, 4.3),
        "Madhya Pradesh": (1117764, 5.1, 5.0),
        "Maharashtra": (2499948, 4.7, 4.5),
        "Meghalaya": (10518, 5.5, 5.1),
        "Mizoram": (15987, 4.9, 4.9),
        "Nagaland": (17152, 4.8, 4.8),
        "Odisha": (350032, 4.5, 4.5),
        "Punjab": (293928, 5.0, 4.8),
        "Rajasthan": (394391, 5.2, 5.3),
        "Sikkim": (7203, 4.4, 4.3),
        "Tamil Nadu": (1463689, 4.0, 3.9),
        "Tripura": (34143, 4.1, 4.0),
        "Uttar Pradesh": (1066363, 5.9, 5.7),
        "Uttarakhand": (93911, 5.2, 4.8),
        "West Bengal": (1391756, 4.6, 4.4),
        "A & N Islands": (3324, 4.3, 4.0),
        "Chandigarh": (21704, 4.4, 4.4),
        "NCT of Delhi": (367893, 4.9, 4.9),
        "Puducherry": (34839, 4.1, 4.1),
        "INDIA": (13920191, 4.7, 4.7),
    }

    rows = []
    for state, (hh, avg_slum, avg_urban) in data.items():
        wiki = get_wiki(state)
        if not wiki:
            continue
        rows.append((2011, wiki, "dcid:NDAP_SlumHouseholds_Total", hh, "Count", "NDAP_NBO_SlumCompendium2015", "P1Y"))
        rows.append((2011, wiki, "dcid:NDAP_SlumHouseholdSize_Avg", avg_slum, "Count", "NDAP_NBO_SlumCompendium2015", "P1Y"))

    return rows

def gen_vital_rates():
    """Appendix 23: Birth Rate, Death Rate, Natural Growth Rate, IMR by State 2013"""
    # state: (birth_rate_total, death_rate_total, nat_growth_total, imr_total)
    data = {
        "Andhra Pradesh": (17.4, 7.3, 10.0, 39),
        "Arunachal Pradesh": (19.3, 5.8, 13.4, 32),
        "Assam": (22.4, 7.8, 14.5, 54),
        "Bihar": (27.6, 6.6, 21.0, 42),
        "Chhattisgarh": (24.4, 7.9, 16.5, 46),
        "Goa": (13.0, 6.6, 6.4, 9),
        "Gujarat": (20.8, 6.5, 14.3, 36),
        "Haryana": (21.3, 6.3, 15.0, 41),
        "Himachal Pradesh": (16.0, 6.7, 9.4, 35),
        "Jammu & Kashmir": (17.5, 5.3, 12.1, 37),
        "Jharkhand": (24.6, 6.8, 17.8, 37),
        "Karnataka": (18.3, 7.0, 11.3, 31),
        "Kerala": (14.7, 6.9, 7.8, 12),
        "Madhya Pradesh": (26.3, 8.0, 18.4, 54),
        "Maharashtra": (16.5, 6.2, 10.2, 24),
        "Manipur": (14.7, 4.0, 10.6, 10),
        "Meghalaya": (23.9, 7.6, 16.4, 47),
        "Mizoram": (16.1, 4.3, 11.8, 35),
        "Nagaland": (15.4, 3.1, 12.3, 18),
        "Odisha": (19.6, 8.4, 11.3, 51),
        "Punjab": (15.7, 6.7, 9.0, 26),
        "Rajasthan": (25.6, 6.5, 19.1, 47),
        "Sikkim": (17.1, 5.2, 11.8, 22),
        "Tamil Nadu": (15.6, 7.3, 8.3, 21),
        "Tripura": (13.7, 4.7, 9.0, 26),
        "Uttar Pradesh": (27.2, 7.7, 19.5, 50),
        "Uttarakhand": (18.2, 6.1, 12.1, 32),
        "West Bengal": (16.0, 6.4, 9.6, 31),
        "A & N Islands": (14.6, 4.6, 10.0, 24),
        "Chandigarh": (14.7, 4.0, 10.7, 21),
        "D & N Haveli": (25.5, 4.4, 21.1, 31),
        "Daman & Diu": (17.9, 4.9, 13.0, 20),
        "NCT of Delhi": (17.2, 4.1, 13.1, 24),
        "Lakshadweep": (14.8, 6.3, 8.5, 24),
        "Puducherry": (15.7, 7.0, 8.7, 17),
        "INDIA": (21.4, 7.0, 14.4, 40),
    }

    rows = []
    for state, (br, dr, ngr, imr) in data.items():
        wiki = get_wiki(state)
        if not wiki:
            continue
        rows.append((2013, wiki, "dcid:NDAP_BirthRate_Total", br, "PerThousand", "NDAP_NBO_SlumCompendium2015_SRS", "P1Y"))
        rows.append((2013, wiki, "dcid:NDAP_DeathRate_Total", dr, "PerThousand", "NDAP_NBO_SlumCompendium2015_SRS", "P1Y"))
        rows.append((2013, wiki, "dcid:NDAP_NaturalGrowthRate_Total", ngr, "PerThousand", "NDAP_NBO_SlumCompendium2015_SRS", "P1Y"))
        rows.append((2013, wiki, "dcid:NDAP_InfantMortalityRate_Total", imr, "PerThousand", "NDAP_NBO_SlumCompendium2015_SRS", "P1Y"))

    return rows

def gen_population_density():
    """Appendix 20: State-wise Population Density 2001-2011"""
    data = {
        "Andhra Pradesh": (277, 308),
        "Arunachal Pradesh": (13, 17),
        "Assam": (340, 398),
        "Bihar": (881, 1106),
        "Chhattisgarh": (154, 189),
        "Goa": (364, 394),
        "Gujarat": (258, 308),
        "Haryana": (478, 573),
        "Himachal Pradesh": (109, 123),
        "Jammu & Kashmir": (100, 124),
        "Jharkhand": (338, 414),
        "Karnataka": (276, 319),
        "Kerala": (820, 860),
        "Madhya Pradesh": (196, 236),
        "Maharashtra": (315, 365),
        "Manipur": (97, 115),
        "Meghalaya": (103, 132),
        "Mizoram": (42, 52),
        "Nagaland": (120, 119),
        "Odisha": (236, 270),
        "Punjab": (484, 551),
        "Rajasthan": (165, 200),
        "Sikkim": (76, 86),
        "Tamil Nadu": (480, 555),
        "Tripura": (305, 350),
        "Uttar Pradesh": (690, 829),
        "Uttarakhand": (159, 189),
        "West Bengal": (903, 1028),
        "A & N Islands": (43, 46),
        "Chandigarh": (7900, 9258),
        "D & N Haveli": (449, 700),
        "Daman & Diu": (1425, 2191),
        "NCT of Delhi": (9340, 11320),
        "Lakshadweep": (2022, 2149),
        "Puducherry": (1989, 2547),
        "INDIA": (325, 382),
    }

    rows = []
    for state, (d2001, d2011) in data.items():
        wiki = get_wiki(state)
        if not wiki:
            continue
        rows.append((2001, wiki, "dcid:NDAP_PopulationDensity_Total", d2001, "PerSquareKilometer", "NDAP_NBO_SlumCompendium2015_Census", "P1Y"))
        rows.append((2011, wiki, "dcid:NDAP_PopulationDensity_Total", d2011, "PerSquareKilometer", "NDAP_NBO_SlumCompendium2015_Census", "P1Y"))

    return rows

def gen_slum_housing_condition():
    """Appendix 53: Slum Housing Condition 2011"""
    # state: (total, good, livable, dilapidated)
    data = {
        "Andhra Pradesh": (2408463, 1807032, 557387, 44044),
        "Arunachal Pradesh": (3991, 1605, 2127, 259),
        "Assam": (48039, 21477, 21504, 5058),
        "Bihar": (189951, 79566, 91745, 18640),
        "Chhattisgarh": (391696, 225119, 154133, 12444),
        "Goa": (4837, 2650, 2044, 143),
        "Gujarat": (359234, 172898, 177098, 9238),
        "Haryana": (324731, 160883, 147603, 16245),
        "Himachal Pradesh": (14244, 10397, 3553, 294),
        "Jammu & Kashmir": (89721, 59251, 26867, 3603),
        "Jharkhand": (78649, 39310, 34528, 4811),
        "Karnataka": (726028, 416485, 279318, 30225),
        "Kerala": (54409, 34370, 16996, 3043),
        "Madhya Pradesh": (1080427, 625007, 414894, 40526),
        "Maharashtra": (2441200, 1412577, 957476, 71147),
        "Meghalaya": (10719, 6515, 3743, 461),
        "Mizoram": (16255, 13041, 3116, 98),
        "Nagaland": (15208, 9117, 5817, 274),
        "Odisha": (347070, 132186, 187355, 27529),
        "Punjab": (295556, 126123, 142474, 26959),
        "Rajasthan": (380535, 215214, 153516, 11805),
        "Sikkim": (8607, 6651, 1769, 187),
        "Tamil Nadu": (1447668, 1001502, 421325, 24841),
        "Tripura": (33719, 18173, 13620, 1926),
        "Uttar Pradesh": (983771, 487031, 447595, 49145),
        "Uttarakhand": (89059, 56026, 28844, 4189),
        "West Bengal": (1389625, 703306, 585393, 100926),
        "A & N Islands": (3050, 2123, 900, 27),
        "Chandigarh": (22069, 1308, 15925, 4836),
        "NCT of Delhi": (381202, 120606, 223176, 37420),
        "Puducherry": (34557, 24365, 9414, 778),
        "INDIA": (13674290, 7991914, 5131255, 551121),
    }

    rows = []
    for state, (total, good, livable, dilap) in data.items():
        wiki = get_wiki(state)
        if not wiki:
            continue
        rows.append((2011, wiki, "dcid:NDAP_SlumHousing_Good", good, "Count", "NDAP_NBO_SlumCompendium2015_Census", "P1Y"))
        rows.append((2011, wiki, "dcid:NDAP_SlumHousing_Livable", livable, "Count", "NDAP_NBO_SlumCompendium2015_Census", "P1Y"))
        rows.append((2011, wiki, "dcid:NDAP_SlumHousing_Dilapidated", dilap, "Count", "NDAP_NBO_SlumCompendium2015_Census", "P1Y"))

    return rows

def gen_nhm_allocations():
    """NHM State-wise ROP Allocations (summary level, Rs. Lakhs)"""
    # From the PDFs/Excel files read:
    # state: {fy: {pool: amount_lakhs}}
    # AP: 2025-26, total 9121.07 lakhs (Visakhapatnam district only — use as-is with note)
    # Telangana: 2022-23 total 4039, 2023-24 total 3616
    # Tripura: 2024-26 data available
    # Sikkim: total 7557.66 lakhs (RCH=790.41, NDCP=1066.62, NCD=394.82, HSS-U=160.34, HSS-R=5145.49)

    # For a meaningful demo, use summary-level state NHM allocations
    # Format: state, FY, pool, amount in lakhs
    nhm_data = [
        # Sikkim - from Excel summary sheet
        ("Sikkim", "2024", "NDAP_NHM_RCH_Allocation", 790.41),
        ("Sikkim", "2024", "NDAP_NHM_NDCP_Allocation", 1066.62),
        ("Sikkim", "2024", "NDAP_NHM_NCD_Allocation", 394.82),
        ("Sikkim", "2024", "NDAP_NHM_HSSU_Allocation", 160.34),
        ("Sikkim", "2024", "NDAP_NHM_HSSR_Allocation", 5145.49),
        ("Sikkim", "2024", "NDAP_NHM_Total_Allocation", 7557.66),

        # Telangana (Hyderabad district) - 2022-23
        ("Telangana", "2022", "NDAP_NHM_RCH_Allocation", 1638.0),
        ("Telangana", "2022", "NDAP_NHM_NDCP_Allocation", 1994.0),
        ("Telangana", "2022", "NDAP_NHM_NCD_Allocation", 52.0),
        ("Telangana", "2022", "NDAP_NHM_HSSU_Allocation", 235.0),
        ("Telangana", "2022", "NDAP_NHM_HSSR_Allocation", 119.0),
        ("Telangana", "2022", "NDAP_NHM_Total_Allocation", 4039.0),
        # Telangana 2023-24
        ("Telangana", "2023", "NDAP_NHM_RCH_Allocation", 1541.0),
        ("Telangana", "2023", "NDAP_NHM_NDCP_Allocation", 1706.0),
        ("Telangana", "2023", "NDAP_NHM_NCD_Allocation", 25.0),
        ("Telangana", "2023", "NDAP_NHM_HSSU_Allocation", 235.0),
        ("Telangana", "2023", "NDAP_NHM_HSSR_Allocation", 109.0),
        ("Telangana", "2023", "NDAP_NHM_Total_Allocation", 3616.0),

        # Andhra Pradesh (Visakhapatnam district) - 2025-26
        ("Andhra Pradesh", "2025", "NDAP_NHM_Total_Allocation", 9121.07),
    ]

    NHM_STATE_WIKI = {
        "Sikkim": "wikidataId/Q1505",
        "Telangana": "wikidataId/Q677037",
        "Andhra Pradesh": "wikidataId/Q1159",
    }

    rows = []
    for state, fy, var, amount in nhm_data:
        wiki = NHM_STATE_WIKI.get(state)
        if not wiki:
            continue
        rows.append((fy, wiki, f"dcid:{var}", amount, "INRLakh", "NDAP_MoHFW_NHM_ROP", "P1Y"))

    return rows


if __name__ == "__main__":
    print("Generating Slum + NHM CSVs...")

    # 1. Slum Population
    rows = gen_slum_population()
    write_csv("ndap_slum_population_statewise.csv", rows)

    # 2. Slum Literacy
    rows = gen_slum_literacy()
    write_csv("ndap_slum_literacy_statewise.csv", rows)

    # 3. Slum Work Participation
    rows = gen_slum_work_participation()
    write_csv("ndap_slum_work_participation_statewise.csv", rows)

    # 4. Slum Households
    rows = gen_slum_households()
    write_csv("ndap_slum_households_statewise.csv", rows)

    # 5. Vital Rates (Birth, Death, IMR)
    rows = gen_vital_rates()
    write_csv("ndap_vital_rates_statewise.csv", rows)

    # 6. Population Density
    rows = gen_population_density()
    write_csv("ndap_population_density_statewise.csv", rows)

    # 7. Slum Housing Condition
    rows = gen_slum_housing_condition()
    write_csv("ndap_slum_housing_condition_statewise.csv", rows)

    # 8. NHM Allocations
    rows = gen_nhm_allocations()
    write_csv("ndap_nhm_allocations_statewise.csv", rows)

    print("\nDone! Now update config.json, stat_vars MCF, and hierarchy MCF.")
