## Government datasets build

Aggregates only (model / model-year level); no per-vehicle record is stored. Raw files never enter git.

| dataset | status | rows | file | sha256 |
|---|---|---|---|---|
| road_survival | failed: date_unparsed (previous snapshot kept) | — | — | — |
| new_car_prices | built | 101824 | new_car_prices.sqlite.gz | f2cd4060c37f |
| recall_notices | built | 3628 | recall_notices.sqlite.gz | b86f193143a6 |

| dataset | resource | status | access method | file HTTP status | rows | total check |
|---|---|---|---|---|---|---|
| road_survival | `851ecab1-0622-4dbe-a6c7-f950cf82abf9` (cancelled) | ok | datastore_api | 302 | 1218960 | estimate_within_2pct |
| road_survival | `4e6b9724-4c1e-43f0-909a-154d4cc4e046` (cancelled) | ok | datastore_api | 302 | 670293 | estimate_within_2pct |
| road_survival | `ec8cbc34-72e1-4b69-9c48-22821ba0bd6c` (cancelled) | ok | datastore_api | 302 | 499791 | estimate_within_2pct |
| road_survival | `053cea08-09bc-40ec-8f7a-156f0677aff3` (active) | ok | datastore_api | 302 | 4187011 | estimate_within_2pct |
| new_car_prices | `39f455bf-6db0-4926-859d-017f34eacbcb` (prices) | ok | datastore_api | 302 | 101824 | estimate_within_2pct |
| recall_notices | `2c33523f-87aa-44ec-a736-edbb0a82975e` (recalls) | ok | datastore_api | 302 | 3628 | exact_total |

### road_survival — Final vehicle cancellations + the active vehicle registry

- **failed**: date_unparsed ([851ecab1-0622-4dbe-a6c7-f950cf82abf9] moed_aliya_lakvish parsed 0.00% of 1218960 rows (needs >= 95%); unparsed shapes [('(empty)', 653622), ('9999', 565338)]; raw examples ['2025', '2026', '2022', '2021', '2018', '2019', '2016', '', '2008', '2020']; [4e6b9724-4c1e-43f0-909a-154d4cc4e046] moed_aliya_lakvish parsed 0.00% of 670293 rows (needs >= 95%); unparsed shapes [('(empty)', 595518), ('9999', 74775)]; raw examples ['', '2008', '2009', '2010', '2011', '2001', '2016', '2014', '2015', '2012']; [ec8cbc34-72e1-4b69-9c48-22821ba0bd6c] moed_aliya_lakvish parsed 0.00% of 499791 rows (needs >= 95%); unparsed shapes [('(empty)', 498248), ('9999', 1543)]; raw examples ['', '2008', '2009', '2000', '2003']; [053cea08-09bc-40ec-8f7a-156f0677aff3] moed_aliya_lakvish parsed 94.27% of 4187011 rows (needs >= 95%); unparsed shapes [('(empty)', 240113)]; raw examples ['']; [851ecab1-0622-4dbe-a6c7-f950cf82abf9] unkeyed 177467 of 1218960 rows (14.56%, allowed 1%); code examples [{'tozeret_cd': '1391', 'degem_cd': None}, {'tozeret_cd': '593', 'degem_cd': None}, {'tozeret_cd': '665', 'degem_cd': None}, {'tozeret_cd': '255', 'degem_cd': None}, {'tozeret_cd': '388', 'degem_cd': None}, {'tozeret_cd': '388', 'degem_cd': None}, {'tozeret_cd': '127', 'degem_cd': None}, {'tozeret_cd': '101', 'degem_cd': None}, {'tozeret_cd': '593', 'degem_cd': None}, {'tozeret_cd': '928', 'degem_cd': None}]; [4e6b9724-4c1e-43f0-909a-154d4cc4e046] unkeyed 280668 of 670293 rows (41.87%, allowed 1%); code examples [{'tozeret_cd': '0588', 'degem_cd': None}, {'tozeret_cd': '0127', 'degem_cd': None}, {'tozeret_cd': '0928', 'degem_cd': None}, {'tozeret_cd': '0590', 'degem_cd': None}, {'tozeret_cd': '0817', 'degem_cd': None}, {'tozeret_cd': '0928', 'degem_cd': None}, {'tozeret_cd': '0634', 'degem_cd': None}, {'tozeret_cd': '0817', 'degem_cd': None}, {'tozeret_cd': '0940', 'degem_cd': None}, {'tozeret_cd': '0683', 'degem_cd': None}]; [ec8cbc34-72e1-4b69-9c48-22821ba0bd6c] unkeyed 349228 of 499791 rows (69.87%, allowed 1%); code examples [{'tozeret_cd': '0300', 'degem_cd': None}, {'tozeret_cd': '0687', 'degem_cd': None}, {'tozeret_cd': '0687', 'degem_cd': None}, {'tozeret_cd': '0892', 'degem_cd': None}, {'tozeret_cd': '0994', 'degem_cd': None}, {'tozeret_cd': '0791', 'degem_cd': None}, {'tozeret_cd': '0791', 'degem_cd': None}, {'tozeret_cd': '0476', 'degem_cd': None}, {'tozeret_cd': '0791', 'degem_cd': None}, {'tozeret_cd': '0569', 'degem_cd': None}]) [resource 851ecab1-0622-4dbe-a6c7-f950cf82abf9]
- the previous snapshot and its manifest entry are kept

Per resource (dates, codes, ages):

| resource | role | rows | unkeyed | moed_aliya_lakvish parsed | bitul_dt parsed | age p10 / p50 / p90 (n) |
|---|---|---|---|---|---|---|
| `851ecab1-0622-4dbe-a6c7-f950cf82abf9` | cancelled | 1218960 | 177467 (14.56 %) | 0.00 % | 100.00 % | None / None / None (0) |
| `4e6b9724-4c1e-43f0-909a-154d4cc4e046` | cancelled | 670293 | 280668 (41.87 %) | 0.00 % | 100.00 % | None / None / None (0) |
| `ec8cbc34-72e1-4b69-9c48-22821ba0bd6c` | cancelled | 499791 | 349228 (69.87 %) | 0.00 % | 100.00 % | None / None / None (0) |
| `053cea08-09bc-40ec-8f7a-156f0677aff3` | active | 4187011 | 0 (0.00 %) | 94.27 % | — | — |
- `851ecab1-0622-4dbe-a6c7-f950cf82abf9` moed_aliya_lakvish: raw values ['2025', '2026', '2022', '2021', '2018']; unparsed shapes [('(empty)', 653622), ('9999', 565338)]
- `851ecab1-0622-4dbe-a6c7-f950cf82abf9` bitul_dt: raw values ['2026-08-27', '2026-09-23', '2026-08-29', '2026-09-14', '2026-09-08']; unparsed shapes none
- `851ecab1-0622-4dbe-a6c7-f950cf82abf9` unkeyed code examples: [{'tozeret_cd': '1391', 'degem_cd': None}, {'tozeret_cd': '593', 'degem_cd': None}, {'tozeret_cd': '665', 'degem_cd': None}, {'tozeret_cd': '255', 'degem_cd': None}, {'tozeret_cd': '388', 'degem_cd': None}, {'tozeret_cd': '388', 'degem_cd': None}, {'tozeret_cd': '127', 'degem_cd': None}, {'tozeret_cd': '101', 'degem_cd': None}, {'tozeret_cd': '593', 'degem_cd': None}, {'tozeret_cd': '928', 'degem_cd': None}]
- `4e6b9724-4c1e-43f0-909a-154d4cc4e046` moed_aliya_lakvish: raw values ['2008', '2009', '2010', '2011', '2001']; unparsed shapes [('(empty)', 595518), ('9999', 74775)]
- `4e6b9724-4c1e-43f0-909a-154d4cc4e046` bitul_dt: raw values ['2015-02-01', '2015-07-01', '2014-08-19', '2015-10-27', '2012-08-23']; unparsed shapes none
- `4e6b9724-4c1e-43f0-909a-154d4cc4e046` unkeyed code examples: [{'tozeret_cd': '0588', 'degem_cd': None}, {'tozeret_cd': '0127', 'degem_cd': None}, {'tozeret_cd': '0928', 'degem_cd': None}, {'tozeret_cd': '0590', 'degem_cd': None}, {'tozeret_cd': '0817', 'degem_cd': None}, {'tozeret_cd': '0928', 'degem_cd': None}, {'tozeret_cd': '0634', 'degem_cd': None}, {'tozeret_cd': '0817', 'degem_cd': None}, {'tozeret_cd': '0940', 'degem_cd': None}, {'tozeret_cd': '0683', 'degem_cd': None}]
- `ec8cbc34-72e1-4b69-9c48-22821ba0bd6c` moed_aliya_lakvish: raw values ['2008', '2009', '2000', '2003']; unparsed shapes [('(empty)', 498248), ('9999', 1543)]
- `ec8cbc34-72e1-4b69-9c48-22821ba0bd6c` bitul_dt: raw values ['2009-06-30', '2006-09-22', '2004-07-14', '2006-04-27', '2007-06-16']; unparsed shapes none
- `ec8cbc34-72e1-4b69-9c48-22821ba0bd6c` unkeyed code examples: [{'tozeret_cd': '0300', 'degem_cd': None}, {'tozeret_cd': '0687', 'degem_cd': None}, {'tozeret_cd': '0687', 'degem_cd': None}, {'tozeret_cd': '0892', 'degem_cd': None}, {'tozeret_cd': '0994', 'degem_cd': None}, {'tozeret_cd': '0791', 'degem_cd': None}, {'tozeret_cd': '0791', 'degem_cd': None}, {'tozeret_cd': '0476', 'degem_cd': None}, {'tozeret_cd': '0791', 'degem_cd': None}, {'tozeret_cd': '0569', 'degem_cd': None}]
- `053cea08-09bc-40ec-8f7a-156f0677aff3` moed_aliya_lakvish: raw values ['2016-10', '2011-10', '2009-9', '2018-10', '2019-10']; unparsed shapes [('(empty)', 240113)]

### new_car_prices — New-car price list

- licence as stated by the package: 'Other (Open)' (brief: 'Other (Open)')
- resource `39f455bf-6db0-4926-859d-017f34eacbcb` (prices): 101824 rows via datastore_api, no file, sha256 e8fe1d4bccfe, last modified 2026-10-09T02:35:44.381052, schema hash b13e47f58082
  - file attempt: download: HTTP 302 from e.data.gov.il: redirect to a non-gov host accounts.google.com refused
  - datastore total 101824 (estimated: True), total check estimate_within_2pct
  - schema: `semel_yevuan | shem_yevuan | sug_degem | tozeret_cd | tozeret_nm | degem_cd | degem_nm | shnat_yitzur | mehir | kinuy_mishari`
- rows 101824, unkeyed (key or price not parsed) 0, of which no code key 0, keys 100869
- keys with 1 distinct price: 99978; 2–3: 891; more than 3: 0
- keys whose rows carry more than one kinuy_mishari: 698
- price per model name, over the active registry's 67583 (key, kinuy_mishari) pairs whose key is in the price list: by name 67582 (100.0 %, 4185627 vehicles), by single-name key 1 (0.0 %, 91 vehicles), withheld price_model_ambiguous 0 (0.0 %, 0 vehicles)
- join against the gov registry index (tozeret_cd|degem_cd|shnat_yitzur): 67305 matched, 33564 unmatched (66.7 %)

20 multi-price keys (largest number of distinct prices first):

| tozeret_cd | degem_cd | shnat_yitzur | distinct prices | min – max | kinuy_mishari values |
|---|---|---|---|---|---|
| 19 | 10 | 2007 | 2 | 175450 – 670850 | A3, Q7 |
| 19 | 12 | 2007 | 2 | 170400 – 529900 | A3, Q7 |
| 19 | 20 | 2007 | 2 | 186250 – 529900 | A3, Q7 |
| 19 | 22 | 2007 | 2 | 180800 – 533550 | A3, Q7 |
| 19 | 22 | 2008 | 2 | 183500 – 582600 | A3, Q7 |
| 19 | 32 | 2008 | 2 | 294500 – 612150 | A3, Q7 |
| 19 | 1383 | 2024 | 2 | 500500 – 1380000 | R8 COUPE, RS3 SPORTBACK |
| 74 | 256 | 2024 | 2 | 399990 – 730000 | GIULIA QUADRIFO, STELVIO |
| 76 | 1 | 2014 | 2 | 149990 – 220000 | ASTRA, VIVARO |
| 76 | 1 | 2015 | 2 | 153990 – 220000 | ASTRA, VIVARO |
| 76 | 1 | 2016 | 2 | 153900 – 219900 | ASTRA, VIVARO |
| 76 | 1 | 2017 | 2 | 153900 – 219900 | ASTRA, VIVARO |
| 177 | 1 | 2011 | 2 | 121990 – 129990 | DOBLO1.3, FIORINO |
| 177 | 1 | 2012 | 2 | 114990 – 133990 | DOBLO1.3, FIORINO |
| 177 | 2 | 2011 | 2 | 99990 – 134990 | DOBLO, DOBLO PANORAMA |
| 177 | 3 | 2011 | 2 | 139990 – 144990 | DOBLO 1.6 COMBY, DOBLO PANORAMA |
| 177 | 3 | 2012 | 2 | 144990 – 148990 | DOBLO 1.6 COMBY, DOBLO PANORAMA |
| 177 | 3 | 2013 | 2 | 146990 – 149990 | DOBLO 1.6 COMBY, DOBLO PANORAMA |
| 177 | 3 | 2014 | 2 | 145528 – 154990 | DOBLO 1.6 COMBY, DOBLO PANORAMA |
| 177 | 3 | 2015 | 2 | 145528 – 154990 | DOBLO 1.6 COMBY, DOBLO PANORAMA |

<details><summary>QA sample (datastore_search, projected)</summary>

```json
[
 [
  {
   "tozeret_cd": 928,
   "degem_cd": 1000,
   "shnat_yitzur": 1996,
   "mehir": 54950,
   "kinuy_mishari": "טווינגו 2.1 YSAE",
   "semel_yevuan": 1,
   "shem_yevuan": "קרסו מוטורס בע\"מ",
   "sug_degem": "P"
  },
  {
   "tozeret_cd": 928,
   "degem_cd": 4060,
   "shnat_yitzur": 1996,
   "mehir": 61990,
   "kinuy_mishari": "91 NR I4.1 4 דלתות",
   "semel_yevuan": 1,
   "shem_yevuan": "קרסו מוטורס בע\"מ",
   "sug_degem": "P"
  },
  {
   "tozeret_cd": 928,
   "degem_cd": 4061,
   "shnat_yitzur": 1996,
   "mehir": 67450,
   "kinuy_mishari": "91 אוטו AD.NR I4.1 4",
   "semel_yevuan": 1,
   "shem_yevuan": "קרסו מוטורס בע\"מ",
   "sug_degem": "P"
  }
 ]
]
```

</details>

### recall_notices — Vehicle recall notices

- licence as stated by the package: 'Creative Commons Attribution' (brief: 'CC BY')
- resource `2c33523f-87aa-44ec-a736-edbb0a82975e` (recalls): 3628 rows via datastore_api, no file, sha256 ae44430c0ae4, last modified 2026-10-09T02:35:29.070580, schema hash ca749ab5a3b6
  - file attempt: download: HTTP 302 from e.data.gov.il: redirect to a non-gov host accounts.google.com refused
  - datastore total 3628 (estimated: False), total check exact_total
  - schema: `RECALL_ID | TOZAR_CD | TOZAR_TEUR | DEGEM | SHNAT_RECALL | BUILD_BEGIN_A | BUILD_END_A | SUG_RECALL | SUG_TAKALA | TEUR_TAKALA | OFEN_TIKUN | TKINA_EU | YEVUAN_TEUR | TELEPHONE | WEBSITE`
- registry names / catalogue models read from: road_survival (this build: the active registry), new_car_prices (this build)
- rows 3628 (3628 distinct), recall ids 3628
- TOZAR_CD agreement with the registry tozeret_cd (same canonical make): 0 of 84 codes = 0.0 % -> not used as a key (needs >= 98 %)
- model map: 1075 entries; unresolved (make, DEGEM): 941
- multi-model DEGEM split: before 341 entries / 1207 unresolved; after 1075 entries / 941 unresolved (266 DEGEM values split into 734 entries)

Unresolved models (top 50 by notice rows):

| make | TOZAR_TEUR | DEGEM | notice rows | reason |
|---|---|---|---|---|
| BMW | BMW | 3 | 31 | no_exact_catalogue_model |
| CHEVROLET | CHEVROLET | SILVERADO | 30 | no_exact_catalogue_model |
| JEEP | JEEP | GRAND CHEROKEE GRAND CHEROKEE L | 18 | no_exact_catalogue_model |
| CHRYSLER | CHRYSLER | TOWN AND COUNTRY | 14 | no_exact_catalogue_model |
| BMW | BMW | I5 IX1 X1 X2 X5 X6 XM 5 7 | 12 | no_exact_catalogue_model |
| CHEVROLET | CHEVROLET | BOLT | 11 | no_exact_catalogue_model |
| MAZDA | MAZDA | 3 | 11 | no_exact_catalogue_model |
| BMW | BMW | 4 | 10 | no_exact_catalogue_model |
| CHEVROLET | CHEVROLET | SAVANA | 10 | no_exact_catalogue_model |
| RAM | RAM | 1500 | 10 | no_exact_catalogue_model |
| BMW | BMW | 5 | 8 | no_exact_catalogue_model |
| BMW | BMW | X5M X6M X5 X6 X7 XM X3 X2 X1 5 7 | 8 | no_exact_catalogue_model |
| HYUNDAI | HYUNDAI | TUSCON | 8 | no_exact_catalogue_model |
| MAZDA | MAZDA | 6 | 8 | no_exact_catalogue_model |
| BMW | BMW | 5 7 | 7 | no_exact_catalogue_model |
| BUICK | BUICK | REGAL | 7 | no_exact_catalogue_model |
| DODGE | DODGE | DURANGO | 7 | no_exact_catalogue_model |
| FORD | FORD | F 150 F 250 F 350 | 7 | no_exact_catalogue_model |
| — | SCANIA | L P G R S | 7 | make_unresolved |
| RAM | RAM | 2500 | 7 | no_exact_catalogue_model |
| RAM | RAM | 2500 3500 | 7 | no_exact_catalogue_model |
| VOLKSWAGEN | VOLKSWAGEN | TERAMONT CROSS SPORT | 7 | no_exact_catalogue_model |
| BMW | BMW | 7 | 6 | no_exact_catalogue_model |
| BMW | BMW | I5 I7 X2 X7 X5 X6 XM 5 7 | 6 | no_exact_catalogue_model |
| BMW | BMW | IX I20 | 6 | no_exact_catalogue_model |
| FORD | FORD | F 250 F 350 | 6 | no_exact_catalogue_model |
| JEEP | JEEP | LIBERTY | 6 | no_exact_catalogue_model |
| JEEP | JEEP | WRANGLER PHEV GRAND CHEROKEE PHEV | 6 | no_exact_catalogue_model |
| MERCEDES-BENZ | MERCEDES BENZ | GLE 167 | 6 | no_exact_catalogue_model |
| — | FIAT PROFESSIONAL | DUCATO | 6 | make_unresolved |
| RAM | RAM | 1500 2500 3500 | 6 | no_exact_catalogue_model |
| BMW | BMW | MINI F56 | 5 | no_exact_catalogue_model |
| BMW | BMW | X1 | 5 | no_exact_catalogue_model |
| DODGE | DODGE | NITRO | 5 | no_exact_catalogue_model |
| FORD | FORD | F 250 | 5 | no_exact_catalogue_model |
| HUMMER | HUMMER | H3 | 5 | no_exact_catalogue_model |
| JEEP | JEEP | GRAND CHEROKEE COMMANDER | 5 | no_exact_catalogue_model |
| MAZDA | MAZDA | 2 | 5 | no_exact_catalogue_model |
| MAZDA | MAZDA | CX60 | 5 | no_exact_catalogue_model |
| MAZDA | MAZDA | CX9 | 5 | no_exact_catalogue_model |
| MERCEDES-BENZ | MERCEDES BENZ | GLE GLS C CLASS S CLASS SL E CLASS GLC CLS AMG GT 4 DOORS COUPE G CLASS | 5 | no_exact_catalogue_model |
| PONTIAC | PONTIAC | SOLSTICE | 5 | no_exact_catalogue_model |
| TOYOTA | TOYOTA | LANDCRUISER | 5 | no_exact_catalogue_model |
| TOYOTA | TOYOTA | LANDCRUISER LEXUS RX GX LX RC IS CAMRY SEQUOIA 4RUNNER FJ CRUSIER SIENNA AVALON HIGHLANDER RC IS | 5 | no_exact_catalogue_model |
| BMW | BMW | 8 | 4 | no_exact_catalogue_model |
| BUICK | BUICK | ENCLAVE | 4 | no_exact_catalogue_model |
| FORD | FORD | F | 4 | no_exact_catalogue_model |
| FORD | FORD | F 250 F 350 F 550 | 4 | no_exact_catalogue_model |
| FORD | FORD | F 550 | 4 | no_exact_catalogue_model |
| GMC | GMC | SAVANA | 4 | no_exact_catalogue_model |

<details><summary>QA sample (datastore_search, projected)</summary>

```json
[
 [
  {
   "RECALL_ID": 11020,
   "TOZAR_TEUR": "TOYOTA",
   "DEGEM": "AVENSIS",
   "BUILD_BEGIN_A": "2000-01-02",
   "BUILD_END_A": "2008-12-31",
   "SUG_TAKALA": "מנוע ומערכותיו",
   "TEUR_TAKALA": "שסתום צינור דלק אוונסיס",
   "TOZAR_CD": 1
  },
  {
   "RECALL_ID": 11026,
   "TOZAR_TEUR": "MERCEDES BENZ",
   "DEGEM": "VITO,VIANO",
   "BUILD_BEGIN_A": "2008-01-01",
   "BUILD_END_A": "2010-12-31",
   "SUG_TAKALA": "חשמל אליקטרוניקה ומיזוג",
   "TEUR_TAKALA": "ממסר מאוור מזגן",
   "TOZAR_CD": 38
  },
  {
   "RECALL_ID": 11029,
   "TOZAR_TEUR": "SUZUKI MOTORCYCLES",
   "DEGEM": "EXEL  SUZUK",
   "BUILD_BEGIN_A": "2010-01-03",
   "BUILD_END_A": "2010-12-31",
   "SUG_TAKALA": "מנוע ומערכותיו",
   "TEUR_TAKALA": "וסת מתח",
   "TOZAR_CD": 105
  }
 ]
]
```

</details>
