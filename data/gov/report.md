## Government datasets build

Aggregates only (model / model-year level); no per-vehicle record is stored. Raw files never enter git.

| dataset | status | rows | file | sha256 |
|---|---|---|---|---|
| new_car_prices | built | 101824 | new_car_prices.sqlite.gz | f2cd4060c37f |
| road_survival | built | 6576055 | road_survival.sqlite.gz | 8b693480f113 |
| recall_notices | built | 3628 | recall_notices.sqlite.gz | 089b18819dca |

| dataset | resource | status | access method | file HTTP status | rows | total check |
|---|---|---|---|---|---|---|
| new_car_prices | `39f455bf-6db0-4926-859d-017f34eacbcb` (prices) | ok | datastore_api | 302 | 101824 | estimate_within_2pct |
| road_survival | `851ecab1-0622-4dbe-a6c7-f950cf82abf9` (cancelled) | ok | datastore_api | 302 | 1218960 | estimate_within_2pct |
| road_survival | `4e6b9724-4c1e-43f0-909a-154d4cc4e046` (cancelled) | ok | datastore_api | 302 | 670293 | estimate_within_2pct |
| road_survival | `ec8cbc34-72e1-4b69-9c48-22821ba0bd6c` (cancelled) | ok | datastore_api | 302 | 499791 | estimate_within_2pct |
| road_survival | `053cea08-09bc-40ec-8f7a-156f0677aff3` (active) | ok | datastore_api | 302 | 4187011 | estimate_within_2pct |
| recall_notices | `2c33523f-87aa-44ec-a736-edbb0a82975e` (recalls) | ok | datastore_api | 302 | 3628 | exact_total |

### new_car_prices — New-car price list

- licence as stated by the package: 'Other (Open)' (brief: 'Other (Open)')
- resource `39f455bf-6db0-4926-859d-017f34eacbcb` (prices): 101824 rows via datastore_api, no file, sha256 e8fe1d4bccfe, last modified 2026-10-09T02:35:44.381052, schema hash b13e47f58082
  - file attempt: download: HTTP 302 from e.data.gov.il: redirect to a non-gov host accounts.google.com refused
  - datastore total 101824 (estimated: True), total check estimate_within_2pct
  - schema: `semel_yevuan | shem_yevuan | sug_degem | tozeret_cd | tozeret_nm | degem_cd | degem_nm | shnat_yitzur | mehir | kinuy_mishari`
- rows 101824, unkeyed (key or price not parsed) 0, keys 100869
- keys with 1 distinct price: 99978; 2–3: 891; more than 3: 0
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

### road_survival — Final vehicle cancellations + the active vehicle registry

- licence as stated by the package: 'Other (Open)' (brief: 'Other (Open)')
- resource `851ecab1-0622-4dbe-a6c7-f950cf82abf9` (cancelled): 1218960 rows via datastore_api, no file, sha256 e23d4f4c853e, last modified 2026-10-09T02:34:57.865431, schema hash e4ac4bd39c0c
  - file attempt: download: HTTP 302 from e.data.gov.il: redirect to a non-gov host accounts.google.com refused
  - datastore total 1218960 (estimated: True), total check estimate_within_2pct
  - schema: `mispar_rechev | tozeret_cd | tozeret_nm | degem_cd | degem_nm | sug_rechev_cd | sug_rechev_nm | moed_aliya_lakvish | bitul_dt | misgeret | tozar_manoa | degem_manoa | mispar_manoa | mishkal_kolel | ramat_gimur | ramat_eivzur_betihuty | kvutzat_zihum | shnat_yitzur | baalut | tzeva_rechev | zmig_kidmi | zmig_ahori | sug_delek_nm | horaat_rishum | kinuy_mishari`
- resource `4e6b9724-4c1e-43f0-909a-154d4cc4e046` (cancelled): 670293 rows via datastore_api, no file, sha256 07c83f40530d, last modified 2021-11-01T11:07:35.917770, schema hash e4ac4bd39c0c
  - file attempt: download: HTTP 302 from e.data.gov.il: redirect to a non-gov host accounts.google.com refused
  - datastore total 670293 (estimated: True), total check estimate_within_2pct
  - schema: `mispar_rechev | tozeret_cd | tozeret_nm | degem_cd | degem_nm | sug_rechev_cd | sug_rechev_nm | moed_aliya_lakvish | bitul_dt | misgeret | tozar_manoa | degem_manoa | mispar_manoa | mishkal_kolel | ramat_gimur | ramat_eivzur_betihuty | kvutzat_zihum | shnat_yitzur | baalut | tzeva_rechev | zmig_kidmi | zmig_ahori | sug_delek_nm | horaat_rishum | kinuy_mishari`
- resource `ec8cbc34-72e1-4b69-9c48-22821ba0bd6c` (cancelled): 499791 rows via datastore_api, no file, sha256 92f5d17ab320, last modified 2023-05-02T04:26:32.042795, schema hash e4ac4bd39c0c
  - file attempt: download: HTTP 302 from e.data.gov.il: redirect to a non-gov host accounts.google.com refused
  - datastore total 499791 (estimated: True), total check estimate_within_2pct
  - schema: `mispar_rechev | tozeret_cd | tozeret_nm | degem_cd | degem_nm | sug_rechev_cd | sug_rechev_nm | moed_aliya_lakvish | bitul_dt | misgeret | tozar_manoa | degem_manoa | mispar_manoa | mishkal_kolel | ramat_gimur | ramat_eivzur_betihuty | kvutzat_zihum | shnat_yitzur | baalut | tzeva_rechev | zmig_kidmi | zmig_ahori | sug_delek_nm | horaat_rishum | kinuy_mishari`
- resource `053cea08-09bc-40ec-8f7a-156f0677aff3` (active): 4187011 rows via datastore_api, no file, sha256 9a2a02bbdfa1, last modified 2026-10-09T02:45:32.005995, schema hash d20462096497
  - file attempt: download: HTTP 302 from e.data.gov.il: redirect to a non-gov host accounts.google.com refused
  - datastore total 4187011 (estimated: True), total check estimate_within_2pct
  - schema: `mispar_rechev | tozeret_cd | sug_degem | tozeret_nm | degem_cd | degem_nm | ramat_gimur | ramat_eivzur_betihuty | kvutzat_zihum | shnat_yitzur | degem_manoa | mivchan_acharon_dt | tokef_dt | baalut | misgeret | tzeva_cd | tzeva_rechev | zmig_kidmi | zmig_ahori | sug_delek_nm | horaat_rishum | moed_aliya_lakvish | kinuy_mishari`
- rows: active 4187011, finally cancelled 2389044 (unkeyed: active 0, cancelled 807363)
- shnat_yitzur coverage on the cancellation rows: 100.0 % -> cohort basis **shnat_yitzur**
- Inactive vehicles that were not finally cancelled are excluded: the cohort is active + finally cancelled vehicles of the same key.
- rows without a cohort year: active 0, cancelled 0; cancellations dated before their first-on-road month: 0
- cohorts 77644, with survival fields (cohort_size >= 200): 5736; reference month 2026-10

20 largest cohorts:

| tozeret_cd | degem_cd | shnat_yitzur | cohort | active | cancelled | median age | share by 10 |
|---|---|---|---|---|---|---|---|
| 588 | 292 | 2008 | 21716 | 11432 | 10284 | — | 0.0 |
| 588 | 431 | 2010 | 11234 | 8627 | 2607 | — | 0.0 |
| 1014 | 4 | 2023 | 10317 | 10152 | 165 | — | — |
| 413 | 641 | 2008 | 10254 | 6608 | 3646 | — | 0.0 |
| 481 | 246 | 2021 | 10174 | 9866 | 308 | — | — |
| 588 | 781 | 2006 | 9891 | 2710 | 7181 | — | 0.0 |
| 885 | 537 | 2022 | 8620 | 8294 | 326 | — | — |
| 588 | 431 | 2009 | 8271 | 6211 | 2060 | — | 0.0 |
| 839 | 3 | 2014 | 8228 | 7474 | 754 | — | 0.0 |
| 588 | 661 | 2011 | 8081 | 6388 | 1693 | — | 0.0 |
| 281 | 52 | 2008 | 7998 | 4768 | 3230 | — | 0.0 |
| 588 | 781 | 2005 | 7963 | 1385 | 6578 | — | 0.0 |
| 481 | 246 | 2020 | 7840 | 7553 | 287 | — | — |
| 839 | 236 | 2021 | 7739 | 7482 | 257 | — | — |
| 590 | 511 | 2008 | 7581 | 2535 | 5046 | — | 0.0 |
| 481 | 235 | 2019 | 7443 | 7067 | 376 | — | — |
| 481 | 182 | 2018 | 7163 | 6721 | 442 | — | — |
| 588 | 191 | 2001 | 7115 | 227 | 6888 | — | 0.0 |
| 1014 | 4 | 2024 | 7025 | 6971 | 54 | — | — |
| 1604 | 22 | 2026 | 7011 | 7005 | 6 | — | — |

### recall_notices — Vehicle recall notices

- licence as stated by the package: 'Creative Commons Attribution' (brief: 'CC BY')
- resource `2c33523f-87aa-44ec-a736-edbb0a82975e` (recalls): 3628 rows via datastore_api, no file, sha256 ae44430c0ae4, last modified 2026-10-09T02:35:29.070580, schema hash ca749ab5a3b6
  - file attempt: download: HTTP 302 from e.data.gov.il: redirect to a non-gov host accounts.google.com refused
  - datastore total 3628 (estimated: False), total check exact_total
  - schema: `RECALL_ID | TOZAR_CD | TOZAR_TEUR | DEGEM | SHNAT_RECALL | BUILD_BEGIN_A | BUILD_END_A | SUG_RECALL | SUG_TAKALA | TEUR_TAKALA | OFEN_TIKUN | TKINA_EU | YEVUAN_TEUR | TELEPHONE | WEBSITE`
- registry names / catalogue models read from: road_survival (this build: the active registry), new_car_prices (this build)
- rows 3628 (3628 distinct), recall ids 3628
- TOZAR_CD agreement with the registry tozeret_cd (same canonical make): 0 of 84 codes = 0.0 % -> not used as a key (needs >= 98 %)
- model map: 336 entries; unresolved (make, DEGEM): 1212

Unresolved models (top 50 by notice rows):

| make | TOZAR_TEUR | DEGEM | notice rows | reason |
|---|---|---|---|---|
| BMW | BMW | 3 | 31 | no_exact_catalogue_model |
| CHEVROLET | CHEVROLET | SILVERADO | 30 | no_exact_catalogue_model |
| SUBARU | SUBARU | B4 OUTBACK | 20 | no_exact_catalogue_model |
| JEEP | JEEP | GRAND CHEROKEE GRAND CHEROKEE L | 18 | no_exact_catalogue_model |
| BMW | BMW | X5 X6 | 16 | no_exact_catalogue_model |
| CHRYSLER | CHRYSLER | TOWN AND COUNTRY | 14 | no_exact_catalogue_model |
| BMW | BMW | I5 IX1 X1 X2 X5 X6 XM 5 7 | 12 | no_exact_catalogue_model |
| CHEVROLET | CHEVROLET | BOLT | 11 | no_exact_catalogue_model |
| MAZDA | MAZDA | 3 | 11 | no_exact_catalogue_model |
| TOYOTA | TOYOTA | COROLLA YARIS | 11 | no_exact_catalogue_model |
| BMW | BMW | 4 | 10 | no_exact_catalogue_model |
| CHEVROLET | CHEVROLET | SAVANA | 10 | no_exact_catalogue_model |
| RAM | RAM | 1500 | 10 | no_exact_catalogue_model |
| TOYOTA | TOYOTA | COROLLA CROSS BZ4X CROWN | 10 | no_exact_catalogue_model |
| SUBARU | SUBARU | IMPREZA WRX | 9 | no_exact_catalogue_model |
| BMW | BMW | 5 | 8 | no_exact_catalogue_model |
| BMW | BMW | X5M X6M X5 X6 X7 XM X3 X2 X1 5 7 | 8 | no_exact_catalogue_model |
| HYUNDAI | HYUNDAI | TUSCON | 8 | no_exact_catalogue_model |
| JEEP | JEEP | WRANGLER GRAND CHEROKEE | 8 | no_exact_catalogue_model |
| MAZDA | MAZDA | 6 | 8 | no_exact_catalogue_model |
| BMW | BMW | 5 7 | 7 | no_exact_catalogue_model |
| BUICK | BUICK | REGAL | 7 | no_exact_catalogue_model |
| DODGE | DODGE | DURANGO | 7 | no_exact_catalogue_model |
| FORD | FORD | EDGE BRONCO | 7 | no_exact_catalogue_model |
| FORD | FORD | F 150 F 250 F 350 | 7 | no_exact_catalogue_model |
| HONDA | HONDA | ODYSSEY PILOT | 7 | no_exact_catalogue_model |
| — | SCANIA | L P G R S | 7 | make_unresolved |
| OPEL | OPEL | MOKKA FRONTERA CORSA | 7 | no_exact_catalogue_model |
| RAM | RAM | 2500 | 7 | no_exact_catalogue_model |
| RAM | RAM | 2500 3500 | 7 | no_exact_catalogue_model |
| SUBARU | SUBARU | WRX IMPREZA | 7 | no_exact_catalogue_model |
| TOYOTA | TOYOTA | AVENSIS COROLLA YARIS | 7 | no_exact_catalogue_model |
| VOLKSWAGEN | VOLKSWAGEN | TERAMONT CROSS SPORT | 7 | no_exact_catalogue_model |
| BMW | BMW | 7 | 6 | no_exact_catalogue_model |
| BMW | BMW | I5 I7 X2 X7 X5 X6 XM 5 7 | 6 | no_exact_catalogue_model |
| BMW | BMW | IX I20 | 6 | no_exact_catalogue_model |
| DODGE | DODGE | CHALLENGER CHARGER | 6 | no_exact_catalogue_model |
| FORD | FORD | F 250 F 350 | 6 | no_exact_catalogue_model |
| HONDA | HONDA | CIVIC CR V | 6 | no_exact_catalogue_model |
| JEEP | JEEP | LIBERTY | 6 | no_exact_catalogue_model |
| JEEP | JEEP | WRANGLER PHEV GRAND CHEROKEE PHEV | 6 | no_exact_catalogue_model |
| MERCEDES-BENZ | MERCEDES BENZ | GLE 167 | 6 | no_exact_catalogue_model |
| — | FIAT PROFESSIONAL | DUCATO | 6 | make_unresolved |
| RAM | RAM | 1500 2500 3500 | 6 | no_exact_catalogue_model |
| TOYOTA | TOYOTA | AVALON CAMRY RAV4 HIGHLANDER SIENNA | 6 | no_exact_catalogue_model |
| TOYOTA | TOYOTA | YARIS C HR | 6 | no_exact_catalogue_model |
| BMW | BMW | MINI F56 | 5 | no_exact_catalogue_model |
| BMW | BMW | X1 | 5 | no_exact_catalogue_model |
| CHRYSLER | CHRYSLER | 300 300C | 5 | no_exact_catalogue_model |
| DODGE | DODGE | NITRO | 5 | no_exact_catalogue_model |

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
