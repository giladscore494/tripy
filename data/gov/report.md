## Government datasets build

Aggregates only (model / model-year level); no per-vehicle record is stored. Raw files never enter git.

| dataset | status | rows | file | sha256 |
|---|---|---|---|---|
| road_survival | built | 6576055 | road_survival.sqlite.gz | 5ce7082684be |

| dataset | resource | status | access method | file HTTP status | rows | total check |
|---|---|---|---|---|---|---|
| road_survival | `851ecab1-0622-4dbe-a6c7-f950cf82abf9` (cancelled) | ok | datastore_api | 302 | 1218960 | estimate_within_2pct |
| road_survival | `4e6b9724-4c1e-43f0-909a-154d4cc4e046` (cancelled) | ok | datastore_api | 302 | 670293 | estimate_within_2pct |
| road_survival | `ec8cbc34-72e1-4b69-9c48-22821ba0bd6c` (cancelled) | ok | datastore_api | 302 | 499791 | estimate_within_2pct |
| road_survival | `053cea08-09bc-40ec-8f7a-156f0677aff3` (active) | ok | datastore_api | 302 | 4187011 | estimate_within_2pct |

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
- rows: active 4187011, finally cancelled 2389044; keyed via degem_nm 81866; still unkeyed: active 0, cancelled 207298 (by reason {'no_tozeret_cd': 2, 'degem_nm_ambiguous': 19383, 'degem_nm_unknown': 187913})
- cohort basis **shnat_yitzur** (shnat_yitzur on the cancellation rows: 100.0 %); age in whole years = year(bitul_dt) − shnat_yitzur; reference year 2026
- Inactive vehicles that were not finally cancelled are excluded: the cohort is active + finally cancelled vehicles of the same key.
- rows without a model year: active 0, cancelled 0; cancellations dated before their model year (bad_dates, no age): 743
- coverage_start (the earliest year of bitul_dt with at least 1% of the cancellation rows): 2000; a cohort of an earlier model year is withheld left_truncated (its early cancellations are missing)
- degem_nm placeholder rows (the only candidate is degem_cd 0; keyed into no cohort, not counted toward unkeyed_rows, counted in the per-cohort guard): model years before 2000 492143, from 2000 26056; cohorts from 2000 withheld by the guard only because of placeholder rows: 70
- cohorts 77644, with survival fields: 4895 (of which with no active vehicle left: 72); withheld: placeholder_model_code (degem_cd 0) 1626, small_cohort (< 200) 70225, left_truncated (model year < coverage_start) 326, unkeyed_cancellations (the unkeyed and placeholder cancellations of its (tozeret_cd, shnat_yitzur) over 10% of its cancellations) 571, undated_cancellations (under 95% of the cancellations with an age) 1
- active vehicles in withheld cohorts (of 4187011 in all cohorts): placeholder_model_code 0 (0.00 %), small_cohort 997346 (23.82 %), left_truncated 3032 (0.07 %), unkeyed_cancellations 143851 (3.44 %), undated_cancellations 201 (0.00 %)
- share of a cohort's cancellations keyed via degem_nm (cohorts with a cancellation, n 37625): p50 0.0, p90 0.0
- sanity: share by age 10 over the cohorts of model years 2005–2014 with survival fields (n 1417): p10 0.0518, p50 0.1176, p90 0.2936

Per resource (counts only):

| resource | role | rows | shnat_yitzur parsed | bitul_dt parsed | moed_aliya_lakvish filled (parsed) | keyed directly | keyed via degem_nm | unkeyed: no_tozeret_cd / no_degem_cd / degem_nm_ambiguous / degem_nm_unknown | age years p10 / p50 / p90 (n) |
|---|---|---|---|---|---|---|---|---|---|
| `851ecab1-0622-4dbe-a6c7-f950cf82abf9` | cancelled | 1218960 | 100.00 % | 100.00 % | 46.38 % (0.00 %) | 1041493 | 47175 | 0 / 0 / 4939 / 81069 (7.06 %) | 5 / 14 / 21 (1218960) |
| `4e6b9724-4c1e-43f0-909a-154d4cc4e046` | cancelled | 670293 | 100.00 % | 100.00 % | 11.16 % (0.00 %) | 389625 | 26366 | 0 / 0 / 10334 / 43590 (8.04 %) | 5 / 15 / 21 (670291) |
| `ec8cbc34-72e1-4b69-9c48-22821ba0bd6c` | cancelled | 499791 | 100.00 % | 100.00 % | 0.31 % (0.00 %) | 150563 | 8325 | 2 / 0 / 4110 / 63254 (13.48 %) | 4 / 13 / 20 (498980) |
| `053cea08-09bc-40ec-8f7a-156f0677aff3` | active | 4187011 | 100.00 % | — | 94.27 % (94.27 %) | 4187011 | 0 | 0 / 0 / 0 / 0 (0.00 %) | — |

- `851ecab1-0622-4dbe-a6c7-f950cf82abf9` moed_aliya_lakvish shapes that are not a month (not used): [('9999', 565338)]
- `4e6b9724-4c1e-43f0-909a-154d4cc4e046` moed_aliya_lakvish shapes that are not a month (not used): [('9999', 74775)]
- `ec8cbc34-72e1-4b69-9c48-22821ba0bd6c` moed_aliya_lakvish shapes that are not a month (not used): [('9999', 1543)]
- `ec8cbc34-72e1-4b69-9c48-22821ba0bd6c` unkeyed code shapes (tozeret_cd / degem_cd): [('99-9 / (empty)', 2)]

20 largest cohorts:

| tozeret_cd | degem_cd | shnat_yitzur | cohort | active | cancelled | via degem_nm | median age | share by 10 | withheld |
|---|---|---|---|---|---|---|---|---|---|
| 588 | 292 | 2008 | 21716 | 11432 | 10284 | 0 | 14 | 0.1381 | — |
| 588 | 431 | 2010 | 11234 | 8627 | 2607 | 0 | 12 | 0.0936 | — |
| 1014 | 4 | 2023 | 10317 | 10152 | 165 | 0 | 2 | — | — |
| 413 | 641 | 2008 | 10254 | 6608 | 3646 | 0 | 13 | 0.1084 | — |
| 481 | 246 | 2021 | 10174 | 9866 | 308 | 0 | 3 | — | — |
| 588 | 781 | 2006 | 9891 | 2710 | 7181 | 0 | 15 | 0.1473 | — |
| 885 | 537 | 2022 | 8620 | 8294 | 326 | 0 | 2 | — | — |
| 588 | 431 | 2009 | 8271 | 6211 | 2060 | 0 | 13 | 0.0869 | — |
| 839 | 3 | 2014 | 8228 | 7474 | 754 | 0 | 8 | 0.0699 | — |
| 588 | 661 | 2011 | 8081 | 6388 | 1693 | 0 | 11 | 0.0989 | — |
| 281 | 52 | 2008 | 7998 | 4768 | 3230 | 0 | 14 | 0.1174 | — |
| 588 | 781 | 2005 | 7963 | 1385 | 6578 | 0 | 16 | 0.1422 | — |
| 481 | 246 | 2020 | 7840 | 7553 | 287 | 0 | 4 | — | — |
| 839 | 236 | 2021 | 7739 | 7482 | 257 | 0 | 3 | — | — |
| 590 | 511 | 2008 | 7581 | 2535 | 5046 | 0 | 13 | 0.2009 | — |
| 481 | 235 | 2019 | 7443 | 7067 | 376 | 0 | 4 | — | — |
| 481 | 182 | 2018 | 7163 | 6721 | 442 | 0 | 5 | — | — |
| 588 | 191 | 2001 | 7115 | 227 | 6888 | 0 | 17 | 0.1048 | — |
| 1014 | 4 | 2024 | 7025 | 6971 | 54 | 0 | 1 | — | — |
| 1604 | 22 | 2026 | 7011 | 7005 | 6 | 0 | 0 | — | — |
