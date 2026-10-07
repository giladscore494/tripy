## EEA schema audit (per year)

Build: https://github.com/giladscore494/tripy/actions/runs/37690665130

Coverage % weighted by registrations (`absent`: no live column; `0.0`: mapped, no value):

| year | status | status used | source rows | rows | registrations | shard | make | model | type_approval | variant | version | fuel | displacement_cc | power_kw | mass_running_order_kg | wheelbase_mm | co2_wltp | co2_nedc | fuel_consumption_l_100km | energy_wh_km | electric_range_km |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2010 | built | F | 165404 | 130255 | 12173065 | 2.6 MB | 100.0 | 98.2 | 79.5 | 89.8 | 77.2 | 99.8 | 98.1 | 0.0 | 100.0 | 98.7 | 0.0 | 99.9 | 0.0 | 0.0 | absent |
| 2011 | built | F | 141278 | 111860 | 11239878 | 2.2 MB | 100.0 | 99.9 | 98.5 | 99.7 | 96.2 | 100.0 | 98.2 | 71.2 | 100.0 | 99.9 | 0.0 | 100.0 | 0.0 | 0.0 | absent |
| 2012 | built | F | 126419 | 101617 | 10249546 | 2.0 MB | 100.0 | 100.0 | 99.9 | 99.8 | 99.0 | 100.0 | 99.1 | 75.0 | 100.0 | 100.0 | 0.0 | 99.9 | 0.0 | 0.1 | absent |
| 2013 | built | F | 126272 | 105260 | 11316458 | 2.0 MB | 100.0 | 100.0 | 100.0 | 100.0 | 99.4 | 95.0 | 99.7 | 77.3 | 100.0 | 100.0 | 0.0 | 100.0 | 0.0 | 0.3 | absent |
| 2014 | built | F | 120486 | 96344 | 12034206 | 1.8 MB | 100.0 | 100.0 | 100.0 | 100.0 | 99.6 | 100.0 | 99.6 | 75.2 | 100.0 | 100.0 | 0.0 | 100.0 | 0.0 | 0.5 | absent |
| 2015 | built | F | 146029 | 110913 | 13255941 | 2.1 MB | 100.0 | 100.0 | 99.2 | 100.0 | 99.6 | 99.9 | 99.6 | 74.5 | 100.0 | 99.3 | 0.0 | 100.0 | 0.0 | 0.9 | absent |
| 2016 | built | F | 145165 | 112401 | 13563888 | 2.1 MB | 100.0 | 100.0 | 100.0 | 99.9 | 99.8 | 99.7 | 99.5 | 74.0 | 100.0 | 99.8 | 0.0 | 100.0 | 0.0 | 1.0 | absent |
| 2017 | built | F | 137182 | 112483 | 14501224 | 2.1 MB | 100.0 | 100.0 | 100.0 | 100.0 | 99.9 | 100.0 | 99.3 | 76.1 | 100.0 | 100.0 | 0.0 | 100.0 | 0.0 | 1.3 | absent |
| 2018 | built | F | 212726 | 178917 | 13639438 | 3.0 MB | 100.0 | 100.0 | 99.9 | 100.0 | 100.0 | 99.8 | 99.0 | 86.4 | 100.0 | 100.0 | 30.4 | 99.9 | 0.0 | 2.3 | absent |
| 2019 | built | F | 272760 | 248545 | 14273505 | 3.8 MB | 100.0 | 100.0 | 100.0 | 99.9 | 99.9 | 100.0 | 97.7 | 87.4 | 100.0 | 99.9 | 96.7 | 100.0 | 0.0 | 3.4 | absent |
| 2020 | built | F | 259740 | 237352 | 10943487 | 3.5 MB | 100.0 | 99.9 | 100.0 | 99.9 | 99.9 | 100.0 | 94.1 | 89.8 | 100.0 | 99.9 | 99.8 | 100.0 | 0.0 | 9.5 | absent |
| 2021 | built | F | 284061 | 181847 | 9212221 | 3.2 MB | 100.0 | 99.9 | 100.0 | 99.8 | 99.8 | 100.0 | 90.5 | 98.5 | 100.0 | 99.9 | 99.9 | 16.5 | 82.5 | 18.2 | absent |
| 2022 | built | P | 337291 | 174053 | 8614200 | 3.3 MB | 100.0 | 98.4 | 100.0 | 99.8 | 99.7 | 100.0 | 87.1 | 99.7 | 100.0 | 99.9 | 99.9 | 15.7 | 82.5 | 22.0 | absent |
| 2025 | stopped (no_csv_link) | P | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — |

### 2010 (F, discodata)

Live header: `ID, MS, Mp, VFN, Mh, Man, MMS, TAN, T, Va, Ve, Mk, Cn, Ct, Cr, M (kg), Mt, Enedc (g/km), Ewltp (g/km), W (mm), At1 (mm), At2 (mm), Ft, Fm, Ec (cm3), Ep (KW), Z (Wh/km), IT, Ernedc (g/km), Erwltp (g/km), De, Vf, R, Year, Status, Version_file, E (g/km), Er (g/km), Zr, Dr, Fc`

| key | live column | state | type | null % | distinct | coverage % (reg.) |
|---|---|---|---|---|---|---|
| make | Mk | mapped | TEXT | 0.0 | 66 | 100.0 |
| model | Cn | mapped | MIXED | 0.9 | 15077 | 98.2 |
| type_approval | T | mapped | MIXED | 13.8 | 7058 | 79.5 |
| variant | Va | mapped | MIXED | 4.7 | 22491 | 89.8 |
| version | Ve | mapped | MIXED | 13.9 | 23585 | 77.2 |
| fuel | Ft | mapped | TEXT | 2.9 | 15 | 99.8 |
| fuel_mode | Fm | mapped | TEXT | 15.4 | 3 | 89.0 |
| displacement_cc | Ec (cm3) | mapped | INTEGER | 9.3 | 868 | 98.1 |
| power_kw | Ep (KW) | empty | — | 100.0 | 0 | 0.0 |
| co2_wltp | Ewltp (g/km) | empty | — | 100.0 | 0 | 0.0 |
| co2_nedc | E (g/km) | mapped | INTEGER | 2.2 | 367 | 99.9 |
| wheelbase_mm | W (mm) | mapped | INTEGER | 17.1 | 718 | 98.7 |
| mass_running_order_kg | M (kg) | mapped | INTEGER | 1.6 | 1818 | 100.0 |
| energy_wh_km | Z (Wh/km) | mapped | INTEGER | 100.0 | 3 | 0.0 |
| electric_range_km | — | absent | — | 100.0 | 0 | 0.0 |
| fuel_consumption_l_100km | Fc | empty | — | 100.0 | 0 | 0.0 |
| year | Year | mapped | INTEGER | 0.0 | 1 | 100.0 |

### 2011 (F, discodata)

Live header: `ID, MS, Mp, VFN, Mh, Man, MMS, TAN, T, Va, Ve, Mk, Cn, Ct, Cr, M (kg), Mt, Enedc (g/km), Ewltp (g/km), W (mm), At1 (mm), At2 (mm), Ft, Fm, Ec (cm3), Ep (KW), Z (Wh/km), IT, Ernedc (g/km), Erwltp (g/km), De, Vf, R, Year, Status, Version_file, E (g/km), Er (g/km), Zr, Dr, Fc`

| key | live column | state | type | null % | distinct | coverage % (reg.) |
|---|---|---|---|---|---|---|
| make | Mk | mapped | TEXT | 0.0 | 66 | 100.0 |
| model | Cn | mapped | MIXED | 1.5 | 11392 | 99.9 |
| type_approval | T | mapped | MIXED | 2.5 | 2578 | 98.5 |
| variant | Va | mapped | MIXED | 2.7 | 13220 | 99.7 |
| version | Ve | mapped | MIXED | 7.0 | 20351 | 96.2 |
| fuel | Ft | mapped | TEXT | 0.0 | 18 | 100.0 |
| fuel_mode | Fm | mapped | TEXT | 6.4 | 3 | 95.8 |
| displacement_cc | Ec (cm3) | mapped | INTEGER | 8.3 | 621 | 98.2 |
| power_kw | Ep (KW) | mapped | INTEGER | 47.7 | 276 | 71.2 |
| co2_wltp | Ewltp (g/km) | empty | — | 100.0 | 0 | 0.0 |
| co2_nedc | E (g/km) | mapped | INTEGER | 0.5 | 346 | 100.0 |
| wheelbase_mm | W (mm) | mapped | INTEGER | 2.5 | 1007 | 99.9 |
| mass_running_order_kg | M (kg) | mapped | INTEGER | 0.4 | 1794 | 100.0 |
| energy_wh_km | Z (Wh/km) | mapped | INTEGER | 100.0 | 10 | 0.0 |
| electric_range_km | — | absent | — | 100.0 | 0 | 0.0 |
| fuel_consumption_l_100km | Fc | empty | — | 100.0 | 0 | 0.0 |
| year | Year | mapped | INTEGER | 0.0 | 1 | 100.0 |

### 2012 (F, discodata)

Live header: `ID, MS, Mp, VFN, Mh, Man, MMS, TAN, T, Va, Ve, Mk, Cn, Ct, Cr, M (kg), Mt, Enedc (g/km), Ewltp (g/km), W (mm), At1 (mm), At2 (mm), Ft, Fm, Ec (cm3), Ep (KW), Z (Wh/km), IT, Ernedc (g/km), Erwltp (g/km), De, Vf, R, Year, Status, Version_file, E (g/km), Er (g/km), Zr, Dr, Fc`

| key | live column | state | type | null % | distinct | coverage % (reg.) |
|---|---|---|---|---|---|---|
| make | Mk | mapped | TEXT | 0.0 | 62 | 100.0 |
| model | Cn | mapped | MIXED | 0.3 | 13809 | 100.0 |
| type_approval | T | mapped | MIXED | 2.7 | 1546 | 99.9 |
| variant | Va | mapped | MIXED | 2.9 | 8656 | 99.8 |
| version | Ve | mapped | MIXED | 5.0 | 16596 | 99.0 |
| fuel | Ft | mapped | TEXT | 0.0 | 19 | 100.0 |
| fuel_mode | Fm | mapped | TEXT | 4.2 | 4 | 97.4 |
| displacement_cc | Ec (cm3) | mapped | INTEGER | 6.6 | 577 | 99.1 |
| power_kw | Ep (KW) | mapped | INTEGER | 35.6 | 279 | 75.0 |
| co2_wltp | Ewltp (g/km) | empty | — | 100.0 | 0 | 0.0 |
| co2_nedc | E (g/km) | mapped | INTEGER | 0.6 | 303 | 99.9 |
| wheelbase_mm | W (mm) | mapped | INTEGER | 1.5 | 513 | 100.0 |
| mass_running_order_kg | M (kg) | mapped | INTEGER | 0.4 | 1743 | 100.0 |
| energy_wh_km | Z (Wh/km) | mapped | INTEGER | 99.9 | 15 | 0.1 |
| electric_range_km | — | absent | — | 100.0 | 0 | 0.0 |
| fuel_consumption_l_100km | Fc | empty | — | 100.0 | 0 | 0.0 |
| year | Year | mapped | INTEGER | 0.0 | 1 | 100.0 |

### 2013 (F, discodata)

Live header: `ID, MS, Mp, VFN, Mh, Man, MMS, TAN, T, Va, Ve, Mk, Cn, Ct, Cr, M (kg), Mt, Enedc (g/km), Ewltp (g/km), W (mm), At1 (mm), At2 (mm), Ft, Fm, Ec (cm3), Ep (KW), Z (Wh/km), IT, Ernedc (g/km), Erwltp (g/km), De, Vf, R, Year, Status, Version_file, E (g/km), Er (g/km), Zr, Dr, Fc`

| key | live column | state | type | null % | distinct | coverage % (reg.) |
|---|---|---|---|---|---|---|
| make | Mk | mapped | TEXT | 0.0 | 68 | 100.0 |
| model | Cn | mapped | MIXED | 0.3 | 12477 | 100.0 |
| type_approval | T | mapped | MIXED | 1.4 | 2411 | 100.0 |
| variant | Va | mapped | MIXED | 2.1 | 10536 | 100.0 |
| version | Ve | mapped | MIXED | 4.7 | 22442 | 99.4 |
| fuel | Ft | mapped | TEXT | 9.4 | 25 | 95.0 |
| fuel_mode | Fm | mapped | TEXT | 12.5 | 3 | 93.6 |
| displacement_cc | Ec (cm3) | mapped | INTEGER | 2.7 | 665 | 99.7 |
| power_kw | Ep (KW) | mapped | INTEGER | 36.2 | 280 | 77.3 |
| co2_wltp | Ewltp (g/km) | empty | — | 100.0 | 0 | 0.0 |
| co2_nedc | E (g/km) | mapped | INTEGER | 0.6 | 318 | 100.0 |
| wheelbase_mm | W (mm) | mapped | INTEGER | 1.2 | 514 | 100.0 |
| mass_running_order_kg | M (kg) | mapped | INTEGER | 0.7 | 1787 | 100.0 |
| energy_wh_km | Z (Wh/km) | mapped | INTEGER | 99.9 | 28 | 0.3 |
| electric_range_km | — | absent | — | 100.0 | 0 | 0.0 |
| fuel_consumption_l_100km | Fc | empty | — | 100.0 | 0 | 0.0 |
| year | Year | mapped | INTEGER | 0.0 | 1 | 100.0 |

### 2014 (F, discodata)

Live header: `ID, MS, Mp, VFN, Mh, Man, MMS, TAN, T, Va, Ve, Mk, Cn, Ct, Cr, M (kg), Mt, Enedc (g/km), Ewltp (g/km), W (mm), At1 (mm), At2 (mm), Ft, Fm, Ec (cm3), Ep (KW), Z (Wh/km), IT, Ernedc (g/km), Erwltp (g/km), De, Vf, R, Year, Status, Version_file, E (g/km), Er (g/km), Zr, Dr, Fc`

| key | live column | state | type | null % | distinct | coverage % (reg.) |
|---|---|---|---|---|---|---|
| make | Mk | mapped | TEXT | 0.0 | 64 | 100.0 |
| model | Cn | mapped | MIXED | 0.4 | 10195 | 100.0 |
| type_approval | T | mapped | MIXED | 2.2 | 1803 | 100.0 |
| variant | Va | mapped | MIXED | 1.8 | 10677 | 100.0 |
| version | Ve | mapped | MIXED | 2.8 | 23170 | 99.6 |
| fuel | Ft | mapped | TEXT | 0.1 | 25 | 100.0 |
| fuel_mode | Fm | mapped | TEXT | 2.7 | 4 | 99.0 |
| displacement_cc | Ec (cm3) | mapped | INTEGER | 1.1 | 530 | 99.6 |
| power_kw | Ep (KW) | mapped | INTEGER | 33.0 | 292 | 75.2 |
| co2_wltp | Ewltp (g/km) | empty | — | 100.0 | 0 | 0.0 |
| co2_nedc | E (g/km) | mapped | INTEGER | 0.4 | 316 | 100.0 |
| wheelbase_mm | W (mm) | mapped | INTEGER | 0.6 | 505 | 100.0 |
| mass_running_order_kg | M (kg) | mapped | INTEGER | 0.2 | 1701 | 100.0 |
| energy_wh_km | Z (Wh/km) | mapped | INTEGER | 99.6 | 45 | 0.5 |
| electric_range_km | — | absent | — | 100.0 | 0 | 0.0 |
| fuel_consumption_l_100km | Fc | empty | — | 100.0 | 0 | 0.0 |
| year | Year | mapped | INTEGER | 0.0 | 1 | 100.0 |

### 2015 (F, discodata)

Live header: `ID, MS, Mp, VFN, Mh, Man, MMS, TAN, T, Va, Ve, Mk, Cn, Ct, Cr, M (kg), Mt, Enedc (g/km), Ewltp (g/km), W (mm), At1 (mm), At2 (mm), Ft, Fm, Ec (cm3), Ep (KW), Z (Wh/km), IT, Ernedc (g/km), Erwltp (g/km), De, Vf, R, Year, Status, Version_file, E (g/km), Er (g/km), Zr, Dr, Fc`

| key | live column | state | type | null % | distinct | coverage % (reg.) |
|---|---|---|---|---|---|---|
| make | Mk | mapped | TEXT | 0.0 | 62 | 100.0 |
| model | Cn | mapped | MIXED | 0.2 | 11287 | 100.0 |
| type_approval | T | mapped | MIXED | 1.1 | 1547 | 99.2 |
| variant | Va | mapped | MIXED | 0.9 | 9919 | 100.0 |
| version | Ve | mapped | MIXED | 1.9 | 22542 | 99.6 |
| fuel | Ft | mapped | TEXT | 0.1 | 28 | 99.9 |
| fuel_mode | Fm | mapped | TEXT | 0.0 | 7 | 100.0 |
| displacement_cc | Ec (cm3) | mapped | INTEGER | 0.6 | 426 | 99.6 |
| power_kw | Ep (KW) | mapped | INTEGER | 30.3 | 287 | 74.5 |
| co2_wltp | Ewltp (g/km) | empty | — | 100.0 | 0 | 0.0 |
| co2_nedc | E (g/km) | mapped | INTEGER | 0.3 | 307 | 100.0 |
| wheelbase_mm | W (mm) | mapped | INTEGER | 1.9 | 430 | 99.3 |
| mass_running_order_kg | M (kg) | mapped | INTEGER | 0.1 | 1764 | 100.0 |
| energy_wh_km | Z (Wh/km) | mapped | INTEGER | 99.2 | 62 | 0.9 |
| electric_range_km | — | absent | — | 100.0 | 0 | 0.0 |
| fuel_consumption_l_100km | Fc | empty | — | 100.0 | 0 | 0.0 |
| year | Year | mapped | INTEGER | 0.0 | 1 | 100.0 |

### 2016 (F, discodata)

Live header: `ID, MS, Mp, VFN, Mh, Man, MMS, TAN, T, Va, Ve, Mk, Cn, Ct, Cr, M (kg), Mt, Enedc (g/km), Ewltp (g/km), W (mm), At1 (mm), At2 (mm), Ft, Fm, Ec (cm3), Ep (KW), Z (Wh/km), IT, Ernedc (g/km), Erwltp (g/km), De, Vf, R, Year, Status, Version_file, E (g/km), Er (g/km), Zr, Dr, Fc`

| key | live column | state | type | null % | distinct | coverage % (reg.) |
|---|---|---|---|---|---|---|
| make | Mk | mapped | TEXT | 0.0 | 51 | 100.0 |
| model | Cn | mapped | MIXED | 1.1 | 11476 | 100.0 |
| type_approval | T | mapped | MIXED | 1.0 | 2670 | 100.0 |
| variant | Va | mapped | MIXED | 4.9 | 8985 | 99.9 |
| version | Ve | mapped | MIXED | 6.0 | 20796 | 99.8 |
| fuel | Ft | mapped | TEXT | 0.2 | 20 | 99.7 |
| fuel_mode | Fm | mapped | TEXT | 0.0 | 4 | 100.0 |
| displacement_cc | Ec (cm3) | mapped | INTEGER | 1.0 | 418 | 99.5 |
| power_kw | Ep (KW) | mapped | INTEGER | 28.7 | 324 | 74.0 |
| co2_wltp | Ewltp (g/km) | empty | — | 100.0 | 0 | 0.0 |
| co2_nedc | E (g/km) | mapped | INTEGER | 0.3 | 356 | 100.0 |
| wheelbase_mm | W (mm) | mapped | INTEGER | 5.3 | 467 | 99.8 |
| mass_running_order_kg | M (kg) | mapped | INTEGER | 0.1 | 1817 | 100.0 |
| energy_wh_km | Z (Wh/km) | mapped | INTEGER | 98.8 | 69 | 1.0 |
| electric_range_km | — | absent | — | 100.0 | 0 | 0.0 |
| fuel_consumption_l_100km | Fc | empty | — | 100.0 | 0 | 0.0 |
| year | Year | mapped | INTEGER | 0.0 | 1 | 100.0 |

### 2017 (F, discodata)

Live header: `ID, MS, Mp, VFN, Mh, Man, MMS, TAN, T, Va, Ve, Mk, Cn, Ct, Cr, M (kg), Mt, Enedc (g/km), Ewltp (g/km), W (mm), At1 (mm), At2 (mm), Ft, Fm, Ec (cm3), Ep (KW), Z (Wh/km), IT, Ernedc (g/km), Erwltp (g/km), De, Vf, R, Year, Status, Version_file, E (g/km), Er (g/km), Zr, Dr, Fc`

| key | live column | state | type | null % | distinct | coverage % (reg.) |
|---|---|---|---|---|---|---|
| make | Mk | mapped | TEXT | 0.0 | 54 | 100.0 |
| model | Cn | mapped | MIXED | 0.2 | 11367 | 100.0 |
| type_approval | T | mapped | MIXED | 0.6 | 1034 | 100.0 |
| variant | Va | mapped | MIXED | 0.8 | 7115 | 100.0 |
| version | Ve | mapped | MIXED | 1.8 | 21217 | 99.9 |
| fuel | Ft | mapped | TEXT | 0.0 | 26 | 100.0 |
| fuel_mode | Fm | mapped | TEXT | 0.0 | 4 | 100.0 |
| displacement_cc | Ec (cm3) | mapped | INTEGER | 1.1 | 311 | 99.3 |
| power_kw | Ep (KW) | mapped | INTEGER | 28.8 | 356 | 76.1 |
| co2_wltp | Ewltp (g/km) | mapped | INTEGER | 99.6 | 101 | 0.0 |
| co2_nedc | Enedc (g/km) | mapped | INTEGER | 0.4 | 305 | 100.0 |
| wheelbase_mm | W (mm) | mapped | INTEGER | 0.0 | 564 | 100.0 |
| mass_running_order_kg | M (kg) | mapped | INTEGER | 0.1 | 1745 | 100.0 |
| energy_wh_km | Z (Wh/km) | mapped | INTEGER | 98.2 | 82 | 1.3 |
| electric_range_km | — | absent | — | 100.0 | 0 | 0.0 |
| fuel_consumption_l_100km | Fc | empty | — | 100.0 | 0 | 0.0 |
| year | Year | mapped | INTEGER | 0.0 | 1 | 100.0 |

### 2018 (F, discodata)

Live header: `ID, MS, Mp, VFN, Mh, Man, MMS, TAN, T, Va, Ve, Mk, Cn, Ct, Cr, M (kg), Mt, Enedc (g/km), Ewltp (g/km), W (mm), At1 (mm), At2 (mm), Ft, Fm, Ec (cm3), Ep (KW), Z (Wh/km), IT, Ernedc (g/km), Erwltp (g/km), De, Vf, R, Year, Status, Version_file, E (g/km), Er (g/km), Zr, Dr, Fc`

| key | live column | state | type | null % | distinct | coverage % (reg.) |
|---|---|---|---|---|---|---|
| make | Mk | mapped | TEXT | 0.0 | 56 | 100.0 |
| model | Cn | mapped | MIXED | 0.2 | 11411 | 100.0 |
| type_approval | T | mapped | MIXED | 0.4 | 923 | 99.9 |
| variant | Va | mapped | MIXED | 0.5 | 6377 | 100.0 |
| version | Ve | mapped | MIXED | 0.5 | 20246 | 100.0 |
| fuel | Ft | mapped | TEXT | 0.3 | 19 | 99.8 |
| fuel_mode | Fm | mapped | MIXED | 0.0 | 6 | 100.0 |
| displacement_cc | Ec (cm3) | mapped | INTEGER | 0.7 | 294 | 99.0 |
| power_kw | Ep (KW) | mapped | INTEGER | 15.7 | 286 | 86.4 |
| co2_wltp | Ewltp (g/km) | mapped | INTEGER | 42.4 | 341 | 30.4 |
| co2_nedc | Enedc (g/km) | mapped | INTEGER | 0.9 | 320 | 99.9 |
| wheelbase_mm | W (mm) | mapped | INTEGER | 0.5 | 508 | 100.0 |
| mass_running_order_kg | M (kg) | mapped | INTEGER | 0.0 | 1826 | 100.0 |
| energy_wh_km | Z (Wh/km) | mapped | INTEGER | 98.5 | 139 | 2.3 |
| electric_range_km | — | absent | — | 100.0 | 0 | 0.0 |
| fuel_consumption_l_100km | Fc | empty | — | 100.0 | 0 | 0.0 |
| year | Year | mapped | INTEGER | 0.0 | 1 | 100.0 |

### 2019 (F, discodata)

Live header: `ID, MS, Mp, VFN, Mh, Man, MMS, TAN, T, Va, Ve, Mk, Cn, Ct, Cr, M (kg), Mt, Enedc (g/km), Ewltp (g/km), W (mm), At1 (mm), At2 (mm), Ft, Fm, Ec (cm3), Ep (KW), Z (Wh/km), IT, Ernedc (g/km), Erwltp (g/km), De, Vf, R, Year, Status, Version_file, E (g/km), Er (g/km), Zr, Dr, Fc`

| key | live column | state | type | null % | distinct | coverage % (reg.) |
|---|---|---|---|---|---|---|
| make | Mk | mapped | TEXT | 0.0 | 61 | 100.0 |
| model | Cn | mapped | MIXED | 0.2 | 9056 | 100.0 |
| type_approval | T | mapped | MIXED | 0.2 | 1801 | 100.0 |
| variant | Va | mapped | MIXED | 1.9 | 6279 | 99.9 |
| version | Ve | mapped | MIXED | 1.9 | 23645 | 99.9 |
| fuel | Ft | mapped | TEXT | 0.0 | 11 | 100.0 |
| fuel_mode | Fm | mapped | TEXT | 0.1 | 7 | 100.0 |
| displacement_cc | Ec (cm3) | mapped | INTEGER | 0.7 | 293 | 97.7 |
| power_kw | Ep (KW) | mapped | INTEGER | 13.9 | 322 | 87.4 |
| co2_wltp | Ewltp (g/km) | mapped | INTEGER | 9.5 | 370 | 96.7 |
| co2_nedc | Enedc (g/km) | mapped | INTEGER | 0.6 | 379 | 100.0 |
| wheelbase_mm | W (mm) | mapped | INTEGER | 1.5 | 417 | 99.9 |
| mass_running_order_kg | M (kg) | mapped | INTEGER | 0.0 | 1846 | 100.0 |
| energy_wh_km | Z (Wh/km) | mapped | INTEGER | 98.3 | 187 | 3.4 |
| electric_range_km | — | absent | — | 100.0 | 0 | 0.0 |
| fuel_consumption_l_100km | Fc | empty | — | 100.0 | 0 | 0.0 |
| year | Year | mapped | INTEGER | 0.0 | 1 | 100.0 |

### 2020 (F, discodata)

Live header: `ID, MS, Mp, VFN, Mh, Man, MMS, TAN, T, Va, Ve, Mk, Cn, Ct, Cr, M (kg), Mt, Enedc (g/km), Ewltp (g/km), W (mm), At1 (mm), At2 (mm), Ft, Fm, Ec (cm3), Ep (KW), Z (Wh/km), IT, Ernedc (g/km), Erwltp (g/km), De, Vf, R, Year, Status, Version_file, E (g/km), Er (g/km), Zr, Dr, Fc`

| key | live column | state | type | null % | distinct | coverage % (reg.) |
|---|---|---|---|---|---|---|
| make | Mk | mapped | TEXT | 0.0 | 66 | 100.0 |
| model | Cn | mapped | MIXED | 0.7 | 8921 | 99.9 |
| type_approval | T | mapped | MIXED | 0.2 | 1470 | 100.0 |
| variant | Va | mapped | MIXED | 1.4 | 5399 | 99.9 |
| version | Ve | mapped | MIXED | 2.0 | 27149 | 99.9 |
| fuel | Ft | mapped | TEXT | 0.0 | 13 | 100.0 |
| fuel_mode | Fm | mapped | TEXT | 0.0 | 6 | 100.0 |
| displacement_cc | Ec (cm3) | mapped | INTEGER | 1.0 | 302 | 94.1 |
| power_kw | Ep (KW) | mapped | INTEGER | 10.0 | 335 | 89.8 |
| co2_wltp | Ewltp (g/km) | mapped | INTEGER | 2.4 | 391 | 99.8 |
| co2_nedc | Enedc (g/km) | mapped | INTEGER | 0.5 | 382 | 100.0 |
| wheelbase_mm | W (mm) | mapped | INTEGER | 1.6 | 416 | 99.9 |
| mass_running_order_kg | M (kg) | mapped | INTEGER | 0.1 | 1830 | 100.0 |
| energy_wh_km | Z (Wh/km) | mapped | INTEGER | 97.2 | 250 | 9.5 |
| electric_range_km | — | absent | — | 100.0 | 0 | 0.0 |
| fuel_consumption_l_100km | Fc | empty | — | 100.0 | 0 | 0.0 |
| year | Year | mapped | INTEGER | 0.0 | 1 | 100.0 |

### 2021 (F, discodata)

Live header: `ID, MS, Mp, VFN, Mh, Man, MMS, TAN, T, Va, Ve, Mk, Cn, Ct, Cr, M (kg), Mt, Enedc (g/km), Ewltp (g/km), W (mm), At1 (mm), At2 (mm), Ft, Fm, Ec (cm3), Ep (KW), Z (Wh/km), IT, Ernedc (g/km), Erwltp (g/km), De, Vf, R, Year, Status, Version_file, E (g/km), Er (g/km), Zr, Dr, Fc`

| key | live column | state | type | null % | distinct | coverage % (reg.) |
|---|---|---|---|---|---|---|
| make | Mk | mapped | TEXT | 0.0 | 68 | 100.0 |
| model | Cn | mapped | MIXED | 0.2 | 5186 | 99.9 |
| type_approval | T | mapped | MIXED | 0.2 | 1311 | 100.0 |
| variant | Va | mapped | MIXED | 1.5 | 4897 | 99.8 |
| version | Ve | mapped | MIXED | 1.9 | 26306 | 99.8 |
| fuel | Ft | mapped | TEXT | 0.0 | 11 | 100.0 |
| fuel_mode | Fm | mapped | TEXT | 0.0 | 6 | 100.0 |
| displacement_cc | Ec (cm3) | mapped | INTEGER | 2.6 | 267 | 90.5 |
| power_kw | Ep (KW) | mapped | INTEGER | 4.8 | 343 | 98.5 |
| co2_wltp | Ewltp (g/km) | mapped | INTEGER | 1.8 | 396 | 99.9 |
| co2_nedc | Enedc (g/km) | mapped | INTEGER | 95.4 | 37 | 16.5 |
| wheelbase_mm | W (mm) | mapped | INTEGER | 1.7 | 419 | 99.9 |
| mass_running_order_kg | M (kg) | mapped | INTEGER | 0.0 | 1774 | 100.0 |
| energy_wh_km | Z (Wh/km) | mapped | INTEGER | 94.7 | 256 | 18.2 |
| electric_range_km | — | absent | — | 100.0 | 0 | 0.0 |
| fuel_consumption_l_100km | Fc | mapped | REAL | 19.8 | 164 | 82.5 |
| year | Year | mapped | INTEGER | 0.0 | 1 | 100.0 |

### 2022 (P, discodata)

Live header: `ID, MS, Mp, VFN, Mh, Man, MMS, TAN, T, Va, Ve, Mk, Cn, Ct, Cr, M (kg), Mt, Enedc (g/km), Ewltp (g/km), W (mm), At1 (mm), At2 (mm), Ft, Fm, Ec (cm3), Ep (KW), Z (Wh/km), IT, Ernedc (g/km), Erwltp (g/km), De, Vf, R, Year, Status, Version_file, E (g/km), Er (g/km), Zr, Dr, Fc`

| key | live column | state | type | null % | distinct | coverage % (reg.) |
|---|---|---|---|---|---|---|
| make | Mk | mapped | TEXT | 0.0 | 134 | 100.0 |
| model | Cn | mapped | MIXED | 2.9 | 7552 | 98.4 |
| type_approval | T | mapped | MIXED | 0.3 | 1513 | 100.0 |
| variant | Va | mapped | MIXED | 1.2 | 4917 | 99.8 |
| version | Ve | mapped | MIXED | 2.6 | 22769 | 99.7 |
| fuel | Ft | mapped | TEXT | 0.0 | 11 | 100.0 |
| fuel_mode | Fm | mapped | TEXT | 0.0 | 6 | 100.0 |
| displacement_cc | Ec (cm3) | mapped | INTEGER | 3.3 | 276 | 87.1 |
| power_kw | Ep (KW) | mapped | INTEGER | 0.5 | 359 | 99.7 |
| co2_wltp | Ewltp (g/km) | mapped | INTEGER | 1.2 | 368 | 99.9 |
| co2_nedc | Enedc (g/km) | mapped | INTEGER | 93.6 | 39 | 15.7 |
| wheelbase_mm | W (mm) | mapped | INTEGER | 1.5 | 466 | 99.9 |
| mass_running_order_kg | M (kg) | mapped | INTEGER | 0.0 | 2021 | 100.0 |
| energy_wh_km | Z (Wh/km) | mapped | INTEGER | 90.9 | 264 | 22.0 |
| electric_range_km | — | absent | — | 100.0 | 0 | 0.0 |
| fuel_consumption_l_100km | Fc | mapped | REAL | 16.2 | 196 | 82.5 |
| year | Year | mapped | INTEGER | 0.0 | 1 | 100.0 |

### Sanity checks

| year | shard rows | rows of another year | HTML / error bodies |
|---|---|---|---|
| 2010 | 130255 | 0 | 0 |
| 2011 | 111860 | 0 | 0 |
| 2012 | 101617 | 0 | 0 |
| 2013 | 105260 | 0 | 0 |
| 2014 | 96344 | 0 | 0 |
| 2015 | 110913 | 0 | 0 |
| 2016 | 112401 | 0 | 0 |
| 2017 | 112483 | 0 | 0 |
| 2018 | 178917 | 0 | 0 |
| 2019 | 248545 | 0 | 0 |
| 2020 | 237352 | 0 | 0 |
| 2021 | 181847 | 0 | 0 |
| 2022 | 174053 | 0 | 0 |
| 2025 | — | — | — |

DISCODATA bodies are parsed only through `eea_response` (H1): an HTML page, an error message or a body without `results` is a query error, never a year without cars.

### Per-year failures

- 2025: stopped (no_csv_link)
