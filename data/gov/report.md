## Government datasets build

Aggregates only (model / model-year level); no per-vehicle record is stored. Raw files never enter git.

| dataset | status | rows | file | sha256 |
|---|---|---|---|---|
| road_survival | built | 6596415 | road_survival.sqlite.gz | 5e1f539b06d1 |
| new_car_prices | built | 102292 | new_car_prices.sqlite.gz | 62bb3a296f57 |
| recall_notices | built | 3642 | recall_notices.sqlite.gz | b46364e6ceb2 |

| dataset | resource | status | access method | file HTTP status | rows | total check |
|---|---|---|---|---|---|---|
| road_survival | `851ecab1-0622-4dbe-a6c7-f950cf82abf9` (cancelled) | ok | datastore_api | 302 | 1228156 | estimate_within_2pct |
| road_survival | `4e6b9724-4c1e-43f0-909a-154d4cc4e046` (cancelled) | ok | datastore_api | 302 | 670293 | estimate_within_2pct |
| road_survival | `ec8cbc34-72e1-4b69-9c48-22821ba0bd6c` (cancelled) | ok | datastore_api | 302 | 499791 | estimate_within_2pct |
| road_survival | `053cea08-09bc-40ec-8f7a-156f0677aff3` (active) | ok | datastore_api | 302 | 4198175 | estimate_within_2pct |
| new_car_prices | `39f455bf-6db0-4926-859d-017f34eacbcb` (prices) | ok | datastore_api | 302 | 102292 | estimate_within_2pct |
| recall_notices | `2c33523f-87aa-44ec-a736-edbb0a82975e` (recalls) | ok | datastore_api | 302 | 3642 | exact_total |

### road_survival — Final vehicle cancellations + the active vehicle registry

- licence as stated by the package: 'Other (Open)' (brief: 'Other (Open)')
- resource `851ecab1-0622-4dbe-a6c7-f950cf82abf9` (cancelled): 1228156 rows via datastore_api, no file, sha256 b9c5fa221f2f, last modified 2026-11-03T03:35:04.644950, schema hash e4ac4bd39c0c
  - file attempt: download: HTTP 302 from e.data.gov.il: redirect to a non-gov host accounts.google.com refused
  - datastore total 1228156 (estimated: True), total check estimate_within_2pct
  - schema: `mispar_rechev | tozeret_cd | tozeret_nm | degem_cd | degem_nm | sug_rechev_cd | sug_rechev_nm | moed_aliya_lakvish | bitul_dt | misgeret | tozar_manoa | degem_manoa | mispar_manoa | mishkal_kolel | ramat_gimur | ramat_eivzur_betihuty | kvutzat_zihum | shnat_yitzur | baalut | tzeva_rechev | zmig_kidmi | zmig_ahori | sug_delek_nm | horaat_rishum | kinuy_mishari`
- resource `4e6b9724-4c1e-43f0-909a-154d4cc4e046` (cancelled): 670293 rows via datastore_api, no file, sha256 07c83f40530d, last modified 2021-11-01T11:07:35.917770, schema hash e4ac4bd39c0c
  - file attempt: download: HTTP 302 from e.data.gov.il: redirect to a non-gov host accounts.google.com refused
  - datastore total 670293 (estimated: True), total check estimate_within_2pct
  - schema: `mispar_rechev | tozeret_cd | tozeret_nm | degem_cd | degem_nm | sug_rechev_cd | sug_rechev_nm | moed_aliya_lakvish | bitul_dt | misgeret | tozar_manoa | degem_manoa | mispar_manoa | mishkal_kolel | ramat_gimur | ramat_eivzur_betihuty | kvutzat_zihum | shnat_yitzur | baalut | tzeva_rechev | zmig_kidmi | zmig_ahori | sug_delek_nm | horaat_rishum | kinuy_mishari`
- resource `ec8cbc34-72e1-4b69-9c48-22821ba0bd6c` (cancelled): 499791 rows via datastore_api, no file, sha256 92f5d17ab320, last modified 2023-05-02T04:26:32.042795, schema hash e4ac4bd39c0c
  - file attempt: download: HTTP 302 from e.data.gov.il: redirect to a non-gov host accounts.google.com refused
  - datastore total 499791 (estimated: True), total check estimate_within_2pct
  - schema: `mispar_rechev | tozeret_cd | tozeret_nm | degem_cd | degem_nm | sug_rechev_cd | sug_rechev_nm | moed_aliya_lakvish | bitul_dt | misgeret | tozar_manoa | degem_manoa | mispar_manoa | mishkal_kolel | ramat_gimur | ramat_eivzur_betihuty | kvutzat_zihum | shnat_yitzur | baalut | tzeva_rechev | zmig_kidmi | zmig_ahori | sug_delek_nm | horaat_rishum | kinuy_mishari`
- resource `053cea08-09bc-40ec-8f7a-156f0677aff3` (active): 4198175 rows via datastore_api, no file, sha256 189449f0a8d1, last modified 2026-11-03T03:41:19.243253, schema hash d20462096497
  - file attempt: download: HTTP 302 from e.data.gov.il: redirect to a non-gov host accounts.google.com refused
  - datastore total 4198175 (estimated: True), total check estimate_within_2pct
  - schema: `mispar_rechev | tozeret_cd | sug_degem | tozeret_nm | degem_cd | degem_nm | ramat_gimur | ramat_eivzur_betihuty | kvutzat_zihum | shnat_yitzur | degem_manoa | mivchan_acharon_dt | tokef_dt | baalut | misgeret | tzeva_cd | tzeva_rechev | zmig_kidmi | zmig_ahori | sug_delek_nm | horaat_rishum | moed_aliya_lakvish | kinuy_mishari`
- rows: active 4198175, finally cancelled 2398240; keyed via degem_nm 82543; still unkeyed: active 0, cancelled 207600 (by reason {'no_tozeret_cd': 2, 'degem_nm_ambiguous': 19401, 'degem_nm_unknown': 188197})
- cohort basis **shnat_yitzur** (shnat_yitzur on the cancellation rows: 100.0 %); age in whole years = year(bitul_dt) − shnat_yitzur; reference year 2026
- Inactive vehicles that were not finally cancelled are excluded: the cohort is active + finally cancelled vehicles of the same key.
- rows without a model year: active 0, cancelled 0; cancellations dated before their model year (bad_dates, no age): 743
- coverage_start (the earliest year of bitul_dt with at least 1% of the cancellation rows): 2000; a cohort of an earlier model year is withheld left_truncated (its early cancellations are missing)
- degem_nm placeholder rows (the only candidate is degem_cd 0; keyed into no cohort, not counted toward unkeyed_rows, counted in the per-cohort guard): model years before 2000 492093, from 2000 25698; cohorts from 2000 withheld by the guard only because of placeholder rows: 71
- cohorts 78000, with survival fields: 4916 (of which with no active vehicle left: 73); withheld: placeholder_model_code (degem_cd 0) 1626, small_cohort (< 200) 70559, left_truncated (model year < coverage_start) 326, unkeyed_cancellations (the unkeyed and placeholder cancellations of its (tozeret_cd, shnat_yitzur) over 10% of its cancellations) 572, undated_cancellations (under 95% of the cancellations with an age) 1
- active vehicles in withheld cohorts (of 4198175 in all cohorts): placeholder_model_code 0 (0.00 %), small_cohort 996211 (23.73 %), left_truncated 3000 (0.07 %), unkeyed_cancellations 148746 (3.54 %), undated_cancellations 201 (0.00 %)
- share of a cohort's cancellations keyed via degem_nm (cohorts with a cancellation, n 37814): p50 0.0, p90 0.0
- sanity: share by age 10 over the cohorts of model years 2005–2014 with survival fields (n 1416): p10 0.052, p50 0.1172, p90 0.2928

Per resource (counts only):

| resource | role | rows | shnat_yitzur parsed | bitul_dt parsed | moed_aliya_lakvish filled (parsed) | keyed directly | keyed via degem_nm | unkeyed: no_tozeret_cd / no_degem_cd / degem_nm_ambiguous / degem_nm_unknown | age years p10 / p50 / p90 (n) |
|---|---|---|---|---|---|---|---|---|---|
| `851ecab1-0622-4dbe-a6c7-f950cf82abf9` | cancelled | 1228156 | 100.00 % | 100.00 % | 46.58 % (0.00 %) | 1050118 | 47462 | 0 / 0 / 4957 / 81397 (7.03 %) | 5 / 14 / 21 (1228156) |
| `4e6b9724-4c1e-43f0-909a-154d4cc4e046` | cancelled | 670293 | 100.00 % | 100.00 % | 11.16 % (0.00 %) | 389625 | 26663 | 0 / 0 / 10334 / 43555 (8.04 %) | 5 / 15 / 21 (670291) |
| `ec8cbc34-72e1-4b69-9c48-22821ba0bd6c` | cancelled | 499791 | 100.00 % | 100.00 % | 0.31 % (0.00 %) | 150563 | 8418 | 2 / 0 / 4110 / 63245 (13.48 %) | 4 / 13 / 20 (498980) |
| `053cea08-09bc-40ec-8f7a-156f0677aff3` | active | 4198175 | 100.00 % | — | 94.36 % (94.36 %) | 4198175 | 0 | 0 / 0 / 0 / 0 (0.00 %) | — |

- `851ecab1-0622-4dbe-a6c7-f950cf82abf9` moed_aliya_lakvish shapes that are not a month (not used): [('9999', 572117)]
- `4e6b9724-4c1e-43f0-909a-154d4cc4e046` moed_aliya_lakvish shapes that are not a month (not used): [('9999', 74775)]
- `ec8cbc34-72e1-4b69-9c48-22821ba0bd6c` moed_aliya_lakvish shapes that are not a month (not used): [('9999', 1543)]
- `ec8cbc34-72e1-4b69-9c48-22821ba0bd6c` unkeyed code shapes (tozeret_cd / degem_cd): [('99-9 / (empty)', 2)]

20 largest cohorts:

| tozeret_cd | degem_cd | shnat_yitzur | cohort | active | cancelled | via degem_nm | median age | share by 10 | withheld |
|---|---|---|---|---|---|---|---|---|---|
| 588 | 292 | 2008 | 21643 | 11255 | 10388 | 0 | 14 | 0.1385 | — |
| 588 | 431 | 2010 | 11217 | 8582 | 2635 | 0 | 12 | 0.0937 | — |
| 1014 | 4 | 2023 | 10316 | 10149 | 167 | 0 | 2 | — | — |
| 413 | 641 | 2008 | 10193 | 6509 | 3684 | 0 | 13 | 0.1091 | — |
| 481 | 246 | 2021 | 10178 | 9865 | 313 | 0 | 3 | — | — |
| 588 | 781 | 2006 | 9831 | 2616 | 7215 | 0 | 15 | 0.1482 | — |
| 885 | 537 | 2022 | 8614 | 8283 | 331 | 0 | 2 | — | — |
| 588 | 431 | 2009 | 8232 | 6145 | 2087 | 0 | 13 | 0.0873 | — |
| 839 | 3 | 2014 | 8230 | 7470 | 760 | 0 | 8 | 0.0699 | — |
| 588 | 661 | 2011 | 8079 | 6365 | 1714 | 0 | 11 | 0.0989 | — |
| 281 | 52 | 2008 | 7989 | 4711 | 3278 | 0 | 14 | 0.1175 | — |
| 588 | 781 | 2005 | 7966 | 1359 | 6607 | 0 | 16 | 0.1421 | — |
| 481 | 246 | 2020 | 7838 | 7547 | 291 | 0 | 4 | — | — |
| 839 | 236 | 2021 | 7740 | 7480 | 260 | 0 | 3 | — | — |
| 590 | 511 | 2008 | 7568 | 2484 | 5084 | 0 | 13 | 0.201 | — |
| 1604 | 22 | 2026 | 7568 | 7562 | 6 | 0 | 0 | — | — |
| 481 | 235 | 2019 | 7443 | 7063 | 380 | 0 | 5 | — | — |
| 481 | 182 | 2018 | 7167 | 6720 | 447 | 0 | 5 | — | — |
| 588 | 191 | 2001 | 7113 | 222 | 6891 | 0 | 17 | 0.1049 | — |
| 1014 | 4 | 2024 | 7026 | 6970 | 56 | 0 | 1 | — | — |

### new_car_prices — New-car price list

- licence as stated by the package: 'Other (Open)' (brief: 'Other (Open)')
- resource `39f455bf-6db0-4926-859d-017f34eacbcb` (prices): 102292 rows via datastore_api, no file, sha256 aecdbc80b7dd, last modified 2026-11-03T03:35:59.281697, schema hash b13e47f58082
  - file attempt: download: HTTP 302 from e.data.gov.il: redirect to a non-gov host accounts.google.com refused
  - datastore total 102292 (estimated: True), total check estimate_within_2pct
  - schema: `semel_yevuan | shem_yevuan | sug_degem | tozeret_cd | tozeret_nm | degem_cd | degem_nm | shnat_yitzur | mehir | kinuy_mishari`
- rows 102292, unkeyed (key or price not parsed) 0, of which no code key 0, keys 101334
- keys with 1 distinct price: 100440; 2–3: 894; more than 3: 0
- keys whose rows carry more than one kinuy_mishari: 699
- price per model name, over the active registry's 67850 (key, kinuy_mishari) pairs whose key is in the price list: by name 67849 (100.0 %, 4196806 vehicles), by single-name key 1 (0.0 %, 90 vehicles), withheld price_model_ambiguous 0 (0.0 %, 0 vehicles)
- join against the gov registry index (tozeret_cd|degem_cd|shnat_yitzur): 67305 matched, 34029 unmatched (66.4 %)

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
- resource `2c33523f-87aa-44ec-a736-edbb0a82975e` (recalls): 3642 rows via datastore_api, no file, sha256 c582ea1ff527, last modified 2026-11-03T03:35:40.878762, schema hash ca749ab5a3b6
  - file attempt: download: HTTP 302 from e.data.gov.il: redirect to a non-gov host accounts.google.com refused
  - datastore total 3642 (estimated: False), total check exact_total
  - schema: `RECALL_ID | TOZAR_CD | TOZAR_TEUR | DEGEM | SHNAT_RECALL | BUILD_BEGIN_A | BUILD_END_A | SUG_RECALL | SUG_TAKALA | TEUR_TAKALA | OFEN_TIKUN | TKINA_EU | YEVUAN_TEUR | TELEPHONE | WEBSITE`
- registry names / catalogue models read from: road_survival (this build: the active registry), new_car_prices (this build)
- rows 3642 (3642 distinct), recall ids 3642
- TOZAR_CD agreement with the registry tozeret_cd (same canonical make): 0 of 84 codes = 0.0 % -> not used as a key (needs >= 98 %)
- model map: 1078 entries; unresolved (make, DEGEM): 947
- multi-model DEGEM split: before 344 entries / 947 unresolved; after 1078 entries / 947 unresolved (0 DEGEM values split into 734 entries)

Unresolved models (top 50 by notice rows):

| make | TOZAR_TEUR | DEGEM | notice rows | reason |
|---|---|---|---|---|
| BMW | BMW | 3 | 30 | no_exact_catalogue_model |
| CHEVROLET | CHEVROLET | SILVERADO | 30 | no_exact_catalogue_model |
| JEEP | JEEP | GRAND CHEROKEE GRAND CHEROKEE L | 15 | no_exact_catalogue_model |
| BMW | BMW | I5 IX1 X1 X2 X5 X6 XM 5 7 | 13 | no_exact_catalogue_model |
| CHEVROLET | CHEVROLET | BOLT | 11 | no_exact_catalogue_model |
| CHRYSLER | CHRYSLER | TOWN AND COUNTRY | 11 | no_exact_catalogue_model |
| MAZDA | MAZDA | 3 | 11 | no_exact_catalogue_model |
| RAM | RAM | 1500 | 11 | no_exact_catalogue_model |
| BMW | BMW | 4 | 10 | no_exact_catalogue_model |
| CHEVROLET | CHEVROLET | SAVANA | 10 | no_exact_catalogue_model |
| BMW | BMW | 5 | 8 | no_exact_catalogue_model |
| BMW | BMW | X5M X6M X5 X6 X7 XM X3 X2 X1 5 7 | 8 | no_exact_catalogue_model |
| HYUNDAI | HYUNDAI | TUSCON | 8 | no_exact_catalogue_model |
| MAZDA | MAZDA | 6 | 8 | no_exact_catalogue_model |
| — | SCANIA | L P G R S | 8 | make_unresolved |
| BMW | BMW | 5 7 | 7 | no_exact_catalogue_model |
| BUICK | BUICK | REGAL | 7 | no_exact_catalogue_model |
| DODGE | DODGE | DURANGO | 7 | no_exact_catalogue_model |
| FORD | FORD | F 150 F 250 F 350 | 7 | no_exact_catalogue_model |
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
| FORD | FORD | F 350 F 550 | 4 | no_exact_catalogue_model |
| FORD | FORD | F 550 | 4 | no_exact_catalogue_model |

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
