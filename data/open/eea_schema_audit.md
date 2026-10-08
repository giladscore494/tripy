## EEA schema audit (per year)

Build: https://github.com/giladscore494/tripy/actions/runs/37705406002

Coverage % weighted by registrations (`absent`: no live column; `0.0`: mapped, no value):

| year | status | status used | source rows | rows | registrations | shard | make | model | type_approval | variant | version | fuel | displacement_cc | power_kw | mass_running_order_kg | wheelbase_mm | co2_wltp | co2_nedc | fuel_consumption_l_100km | energy_wh_km | electric_range_km |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2010 | built | F | 165404 | 122553 | 12173065 | 2.4 MB | 100.0 | 98.2 | 79.5 | 89.8 | 77.2 | 99.8 | 98.1 | 0.0 | 100.0 | 99.0 | 0.0 | 99.9 | 0.0 | 0.0 | absent |
| 2011 | built | F | 141278 | 106471 | 11239878 | 2.0 MB | 100.0 | 99.9 | 98.5 | 99.7 | 96.2 | 100.0 | 98.2 | 71.2 | 100.0 | 99.9 | 0.0 | 100.0 | 0.0 | 0.0 | absent |
| 2012 | built | F | 126419 | 95658 | 10249546 | 1.8 MB | 100.0 | 100.0 | 99.9 | 99.8 | 99.0 | 100.0 | 99.1 | 75.0 | 100.0 | 100.0 | 0.0 | 99.9 | 0.0 | 0.1 | absent |
| 2013 | built | F | 126272 | 100802 | 11316458 | 1.9 MB | 100.0 | 100.0 | 100.0 | 100.0 | 99.4 | 95.0 | 99.7 | 77.3 | 100.0 | 100.0 | 0.0 | 100.0 | 0.0 | 0.3 | absent |
| 2014 | built | F | 120486 | 93492 | 12034206 | 1.8 MB | 100.0 | 100.0 | 100.0 | 100.0 | 99.6 | 100.0 | 99.6 | 75.2 | 100.0 | 100.0 | 0.0 | 100.0 | 0.0 | 0.5 | absent |
| 2015 | built | F | 146029 | 104204 | 13255941 | 1.9 MB | 100.0 | 100.0 | 99.2 | 100.0 | 99.6 | 99.9 | 99.6 | 74.5 | 100.0 | 100.0 | 0.0 | 100.0 | 0.0 | 0.9 | absent |
| 2016 | built | F | 145165 | 104897 | 13563888 | 1.9 MB | 100.0 | 100.0 | 100.0 | 99.9 | 99.8 | 99.7 | 99.5 | 74.0 | 100.0 | 99.9 | 0.0 | 100.0 | 0.0 | 1.0 | absent |
| 2017 | built | F | 137182 | 106481 | 14501224 | 1.9 MB | 100.0 | 100.0 | 100.0 | 100.0 | 99.9 | 100.0 | 99.3 | 76.1 | 100.0 | 100.0 | 0.0 | 100.0 | 0.0 | 1.4 | absent |
| 2018 | built | F | 212726 | 169105 | 13639438 | 2.8 MB | 100.0 | 100.0 | 99.9 | 100.0 | 100.0 | 99.8 | 99.0 | 86.4 | 100.0 | 100.0 | 30.4 | 99.9 | 0.0 | 2.8 | absent |
| 2019 | built | F | 272760 | 246100 | 14273505 | 3.7 MB | 100.0 | 100.0 | 100.0 | 99.9 | 99.9 | 100.0 | 97.7 | 87.4 | 100.0 | 99.9 | 96.7 | 100.0 | 0.0 | 3.6 | absent |
| 2020 | built | F | 259740 | 235021 | 10943487 | 3.5 MB | 100.0 | 99.9 | 100.0 | 99.9 | 99.9 | 100.0 | 94.1 | 89.8 | 100.0 | 99.9 | 99.8 | 100.0 | 0.0 | 9.6 | absent |
| 2021 | built | F | 284061 | 173428 | 9212221 | 3.0 MB | 100.0 | 99.9 | 100.0 | 99.8 | 99.8 | 100.0 | 90.5 | 98.5 | 100.0 | 99.9 | 99.9 | 16.5 | 84.3 | 18.4 | absent |
| 2022 | built | P | 337291 | 163851 | 8614200 | 3.1 MB | 100.0 | 98.4 | 100.0 | 99.8 | 99.7 | 100.0 | 87.1 | 99.7 | 100.0 | 99.9 | 99.9 | 15.7 | 82.9 | 22.2 | absent |
| 2025 | stopped (no_csv_link) | P | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — |

### 2010 (F, discodata)

Live header: `ID, MS, Mp, VFN, Mh, Man, MMS, TAN, T, Va, Ve, Mk, Cn, Ct, Cr, M (kg), Mt, Enedc (g/km), Ewltp (g/km), W (mm), At1 (mm), At2 (mm), Ft, Fm, Ec (cm3), Ep (KW), Z (Wh/km), IT, Ernedc (g/km), Erwltp (g/km), De, Vf, R, Year, Status, Version_file, E (g/km), Er (g/km), Zr, Dr, Fc`

| key | live column | state | type | null % | distinct | coverage % (reg.) |
|---|---|---|---|---|---|---|
| make | Mk | mapped | TEXT | 0.0 | 66 | 100.0 |
| model | Cn | mapped | MIXED | 1.0 | 14593 | 98.2 |
| type_approval | T | mapped | MIXED | 14.6 | 6995 | 79.5 |
| variant | Va | mapped | MIXED | 5.0 | 22356 | 89.8 |
| version | Ve | mapped | MIXED | 14.8 | 23447 | 77.2 |
| fuel | Ft | mapped | TEXT | 3.1 | 7 | 99.8 |
| fuel_mode | Fm | mapped | TEXT | 16.2 | 3 | 89.0 |
| displacement_cc | Ec (cm3) | mapped | INTEGER | 9.7 | 868 | 98.1 |
| power_kw | Ep (KW) | empty | — | 100.0 | 0 | 0.0 |
| co2_wltp | Ewltp (g/km) | empty | — | 100.0 | 0 | 0.0 |
| co2_nedc | E (g/km) | mapped | INTEGER | 2.3 | 367 | 99.9 |
| wheelbase_mm | W (mm) | mapped | INTEGER | 15.7 | 669 | 99.0 |
| mass_running_order_kg | M (kg) | mapped | INTEGER | 1.6 | 1800 | 100.0 |
| energy_wh_km | Z (Wh/km) | mapped | INTEGER | 100.0 | 3 | 0.0 |
| electric_range_km | — | absent | — | 100.0 | 0 | 0.0 |
| fuel_consumption_l_100km | Fc | empty | — | 100.0 | 0 | 0.0 |
| year | Year | mapped | INTEGER | 0.0 | 1 | 100.0 |

### 2011 (F, discodata)

Live header: `ID, MS, Mp, VFN, Mh, Man, MMS, TAN, T, Va, Ve, Mk, Cn, Ct, Cr, M (kg), Mt, Enedc (g/km), Ewltp (g/km), W (mm), At1 (mm), At2 (mm), Ft, Fm, Ec (cm3), Ep (KW), Z (Wh/km), IT, Ernedc (g/km), Erwltp (g/km), De, Vf, R, Year, Status, Version_file, E (g/km), Er (g/km), Zr, Dr, Fc`

| key | live column | state | type | null % | distinct | coverage % (reg.) |
|---|---|---|---|---|---|---|
| make | Mk | mapped | TEXT | 0.0 | 66 | 100.0 |
| model | Cn | mapped | MIXED | 1.6 | 10980 | 99.9 |
| type_approval | T | mapped | MIXED | 2.6 | 2485 | 98.5 |
| variant | Va | mapped | MIXED | 2.8 | 13067 | 99.7 |
| version | Ve | mapped | MIXED | 7.3 | 20282 | 96.2 |
| fuel | Ft | mapped | TEXT | 0.0 | 10 | 100.0 |
| fuel_mode | Fm | mapped | TEXT | 6.7 | 3 | 95.8 |
| displacement_cc | Ec (cm3) | mapped | INTEGER | 8.5 | 621 | 98.2 |
| power_kw | Ep (KW) | mapped | INTEGER | 48.8 | 276 | 71.2 |
| co2_wltp | Ewltp (g/km) | empty | — | 100.0 | 0 | 0.0 |
| co2_nedc | E (g/km) | mapped | INTEGER | 0.5 | 346 | 100.0 |
| wheelbase_mm | W (mm) | mapped | INTEGER | 2.6 | 903 | 99.9 |
| mass_running_order_kg | M (kg) | mapped | INTEGER | 0.4 | 1769 | 100.0 |
| energy_wh_km | Z (Wh/km) | mapped | INTEGER | 100.0 | 10 | 0.0 |
| electric_range_km | — | absent | — | 100.0 | 0 | 0.0 |
| fuel_consumption_l_100km | Fc | empty | — | 100.0 | 0 | 0.0 |
| year | Year | mapped | INTEGER | 0.0 | 1 | 100.0 |

### 2012 (F, discodata)

Live header: `ID, MS, Mp, VFN, Mh, Man, MMS, TAN, T, Va, Ve, Mk, Cn, Ct, Cr, M (kg), Mt, Enedc (g/km), Ewltp (g/km), W (mm), At1 (mm), At2 (mm), Ft, Fm, Ec (cm3), Ep (KW), Z (Wh/km), IT, Ernedc (g/km), Erwltp (g/km), De, Vf, R, Year, Status, Version_file, E (g/km), Er (g/km), Zr, Dr, Fc`

| key | live column | state | type | null % | distinct | coverage % (reg.) |
|---|---|---|---|---|---|---|
| make | Mk | mapped | TEXT | 0.0 | 62 | 100.0 |
| model | Cn | mapped | MIXED | 0.3 | 13301 | 100.0 |
| type_approval | T | mapped | MIXED | 2.8 | 1466 | 99.9 |
| variant | Va | mapped | MIXED | 3.0 | 8616 | 99.8 |
| version | Ve | mapped | MIXED | 5.3 | 16588 | 99.0 |
| fuel | Ft | mapped | TEXT | 0.0 | 11 | 100.0 |
| fuel_mode | Fm | mapped | TEXT | 4.5 | 3 | 97.4 |
| displacement_cc | Ec (cm3) | mapped | INTEGER | 6.7 | 577 | 99.1 |
| power_kw | Ep (KW) | mapped | INTEGER | 37.3 | 279 | 75.0 |
| co2_wltp | Ewltp (g/km) | empty | — | 100.0 | 0 | 0.0 |
| co2_nedc | E (g/km) | mapped | INTEGER | 0.6 | 303 | 99.9 |
| wheelbase_mm | W (mm) | mapped | INTEGER | 0.8 | 490 | 100.0 |
| mass_running_order_kg | M (kg) | mapped | INTEGER | 0.4 | 1710 | 100.0 |
| energy_wh_km | Z (Wh/km) | mapped | INTEGER | 99.9 | 15 | 0.1 |
| electric_range_km | — | absent | — | 100.0 | 0 | 0.0 |
| fuel_consumption_l_100km | Fc | empty | — | 100.0 | 0 | 0.0 |
| year | Year | mapped | INTEGER | 0.0 | 1 | 100.0 |

### 2013 (F, discodata)

Live header: `ID, MS, Mp, VFN, Mh, Man, MMS, TAN, T, Va, Ve, Mk, Cn, Ct, Cr, M (kg), Mt, Enedc (g/km), Ewltp (g/km), W (mm), At1 (mm), At2 (mm), Ft, Fm, Ec (cm3), Ep (KW), Z (Wh/km), IT, Ernedc (g/km), Erwltp (g/km), De, Vf, R, Year, Status, Version_file, E (g/km), Er (g/km), Zr, Dr, Fc`

| key | live column | state | type | null % | distinct | coverage % (reg.) |
|---|---|---|---|---|---|---|
| make | Mk | mapped | TEXT | 0.0 | 68 | 100.0 |
| model | Cn | mapped | MIXED | 0.3 | 12195 | 100.0 |
| type_approval | T | mapped | MIXED | 1.4 | 2334 | 100.0 |
| variant | Va | mapped | MIXED | 2.2 | 10529 | 100.0 |
| version | Ve | mapped | MIXED | 4.9 | 22415 | 99.4 |
| fuel | Ft | mapped | TEXT | 9.9 | 10 | 95.0 |
| fuel_mode | Fm | mapped | TEXT | 13.1 | 3 | 93.6 |
| displacement_cc | Ec (cm3) | mapped | INTEGER | 2.8 | 665 | 99.7 |
| power_kw | Ep (KW) | mapped | INTEGER | 37.5 | 280 | 77.3 |
| co2_wltp | Ewltp (g/km) | empty | — | 100.0 | 0 | 0.0 |
| co2_nedc | E (g/km) | mapped | INTEGER | 0.6 | 318 | 100.0 |
| wheelbase_mm | W (mm) | mapped | INTEGER | 0.8 | 503 | 100.0 |
| mass_running_order_kg | M (kg) | mapped | INTEGER | 0.7 | 1774 | 100.0 |
| energy_wh_km | Z (Wh/km) | mapped | INTEGER | 99.9 | 28 | 0.3 |
| electric_range_km | — | absent | — | 100.0 | 0 | 0.0 |
| fuel_consumption_l_100km | Fc | empty | — | 100.0 | 0 | 0.0 |
| year | Year | mapped | INTEGER | 0.0 | 1 | 100.0 |

### 2014 (F, discodata)

Live header: `ID, MS, Mp, VFN, Mh, Man, MMS, TAN, T, Va, Ve, Mk, Cn, Ct, Cr, M (kg), Mt, Enedc (g/km), Ewltp (g/km), W (mm), At1 (mm), At2 (mm), Ft, Fm, Ec (cm3), Ep (KW), Z (Wh/km), IT, Ernedc (g/km), Erwltp (g/km), De, Vf, R, Year, Status, Version_file, E (g/km), Er (g/km), Zr, Dr, Fc`

| key | live column | state | type | null % | distinct | coverage % (reg.) |
|---|---|---|---|---|---|---|
| make | Mk | mapped | TEXT | 0.0 | 64 | 100.0 |
| model | Cn | mapped | MIXED | 0.4 | 9886 | 100.0 |
| type_approval | T | mapped | MIXED | 2.2 | 1750 | 100.0 |
| variant | Va | mapped | MIXED | 1.9 | 10662 | 100.0 |
| version | Ve | mapped | MIXED | 2.9 | 23142 | 99.6 |
| fuel | Ft | mapped | TEXT | 0.1 | 11 | 100.0 |
| fuel_mode | Fm | mapped | TEXT | 2.7 | 4 | 99.0 |
| displacement_cc | Ec (cm3) | mapped | INTEGER | 1.2 | 530 | 99.6 |
| power_kw | Ep (KW) | mapped | INTEGER | 33.7 | 292 | 75.2 |
| co2_wltp | Ewltp (g/km) | empty | — | 100.0 | 0 | 0.0 |
| co2_nedc | E (g/km) | mapped | INTEGER | 0.4 | 316 | 100.0 |
| wheelbase_mm | W (mm) | mapped | INTEGER | 0.4 | 485 | 100.0 |
| mass_running_order_kg | M (kg) | mapped | INTEGER | 0.2 | 1670 | 100.0 |
| energy_wh_km | Z (Wh/km) | mapped | INTEGER | 99.7 | 43 | 0.5 |
| electric_range_km | — | absent | — | 100.0 | 0 | 0.0 |
| fuel_consumption_l_100km | Fc | empty | — | 100.0 | 0 | 0.0 |
| year | Year | mapped | INTEGER | 0.0 | 1 | 100.0 |

### 2015 (F, discodata)

Live header: `ID, MS, Mp, VFN, Mh, Man, MMS, TAN, T, Va, Ve, Mk, Cn, Ct, Cr, M (kg), Mt, Enedc (g/km), Ewltp (g/km), W (mm), At1 (mm), At2 (mm), Ft, Fm, Ec (cm3), Ep (KW), Z (Wh/km), IT, Ernedc (g/km), Erwltp (g/km), De, Vf, R, Year, Status, Version_file, E (g/km), Er (g/km), Zr, Dr, Fc`

| key | live column | state | type | null % | distinct | coverage % (reg.) |
|---|---|---|---|---|---|---|
| make | Mk | mapped | TEXT | 0.0 | 62 | 100.0 |
| model | Cn | mapped | MIXED | 0.2 | 10961 | 100.0 |
| type_approval | T | mapped | MIXED | 1.2 | 1506 | 99.2 |
| variant | Va | mapped | MIXED | 0.9 | 9890 | 100.0 |
| version | Ve | mapped | MIXED | 2.1 | 22533 | 99.6 |
| fuel | Ft | mapped | TEXT | 0.1 | 14 | 99.9 |
| fuel_mode | Fm | mapped | TEXT | 0.0 | 6 | 100.0 |
| displacement_cc | Ec (cm3) | mapped | INTEGER | 0.6 | 426 | 99.6 |
| power_kw | Ep (KW) | mapped | INTEGER | 32.0 | 287 | 74.5 |
| co2_wltp | Ewltp (g/km) | empty | — | 100.0 | 0 | 0.0 |
| co2_nedc | E (g/km) | mapped | INTEGER | 0.3 | 307 | 100.0 |
| wheelbase_mm | W (mm) | mapped | INTEGER | 0.4 | 422 | 100.0 |
| mass_running_order_kg | M (kg) | mapped | INTEGER | 0.1 | 1743 | 100.0 |
| energy_wh_km | Z (Wh/km) | mapped | INTEGER | 99.2 | 61 | 0.9 |
| electric_range_km | — | absent | — | 100.0 | 0 | 0.0 |
| fuel_consumption_l_100km | Fc | empty | — | 100.0 | 0 | 0.0 |
| year | Year | mapped | INTEGER | 0.0 | 1 | 100.0 |

### 2016 (F, discodata)

Live header: `ID, MS, Mp, VFN, Mh, Man, MMS, TAN, T, Va, Ve, Mk, Cn, Ct, Cr, M (kg), Mt, Enedc (g/km), Ewltp (g/km), W (mm), At1 (mm), At2 (mm), Ft, Fm, Ec (cm3), Ep (KW), Z (Wh/km), IT, Ernedc (g/km), Erwltp (g/km), De, Vf, R, Year, Status, Version_file, E (g/km), Er (g/km), Zr, Dr, Fc`

| key | live column | state | type | null % | distinct | coverage % (reg.) |
|---|---|---|---|---|---|---|
| make | Mk | mapped | TEXT | 0.0 | 51 | 100.0 |
| model | Cn | mapped | MIXED | 1.2 | 11110 | 100.0 |
| type_approval | T | mapped | MIXED | 1.1 | 2631 | 100.0 |
| variant | Va | mapped | MIXED | 5.2 | 8925 | 99.9 |
| version | Ve | mapped | MIXED | 6.4 | 20766 | 99.8 |
| fuel | Ft | mapped | TEXT | 0.3 | 10 | 99.7 |
| fuel_mode | Fm | mapped | TEXT | 0.0 | 4 | 100.0 |
| displacement_cc | Ec (cm3) | mapped | INTEGER | 1.0 | 418 | 99.5 |
| power_kw | Ep (KW) | mapped | INTEGER | 30.5 | 324 | 74.0 |
| co2_wltp | Ewltp (g/km) | empty | — | 100.0 | 0 | 0.0 |
| co2_nedc | E (g/km) | mapped | INTEGER | 0.4 | 356 | 100.0 |
| wheelbase_mm | W (mm) | mapped | INTEGER | 5.5 | 444 | 99.9 |
| mass_running_order_kg | M (kg) | mapped | INTEGER | 0.1 | 1806 | 100.0 |
| energy_wh_km | Z (Wh/km) | mapped | INTEGER | 98.7 | 68 | 1.0 |
| electric_range_km | — | absent | — | 100.0 | 0 | 0.0 |
| fuel_consumption_l_100km | Fc | empty | — | 100.0 | 0 | 0.0 |
| year | Year | mapped | INTEGER | 0.0 | 1 | 100.0 |

### 2017 (F, discodata)

Live header: `ID, MS, Mp, VFN, Mh, Man, MMS, TAN, T, Va, Ve, Mk, Cn, Ct, Cr, M (kg), Mt, Enedc (g/km), Ewltp (g/km), W (mm), At1 (mm), At2 (mm), Ft, Fm, Ec (cm3), Ep (KW), Z (Wh/km), IT, Ernedc (g/km), Erwltp (g/km), De, Vf, R, Year, Status, Version_file, E (g/km), Er (g/km), Zr, Dr, Fc`

| key | live column | state | type | null % | distinct | coverage % (reg.) |
|---|---|---|---|---|---|---|
| make | Mk | mapped | TEXT | 0.0 | 54 | 100.0 |
| model | Cn | mapped | MIXED | 0.2 | 10880 | 100.0 |
| type_approval | T | mapped | MIXED | 0.6 | 1009 | 100.0 |
| variant | Va | mapped | MIXED | 0.8 | 7106 | 100.0 |
| version | Ve | mapped | MIXED | 1.9 | 21209 | 99.9 |
| fuel | Ft | mapped | TEXT | 0.0 | 10 | 100.0 |
| fuel_mode | Fm | mapped | TEXT | 0.0 | 4 | 100.0 |
| displacement_cc | Ec (cm3) | mapped | INTEGER | 1.0 | 311 | 99.3 |
| power_kw | Ep (KW) | mapped | INTEGER | 30.2 | 356 | 76.1 |
| co2_wltp | Ewltp (g/km) | mapped | INTEGER | 99.6 | 101 | 0.0 |
| co2_nedc | Enedc (g/km) | mapped | INTEGER | 0.4 | 305 | 100.0 |
| wheelbase_mm | W (mm) | mapped | INTEGER | 0.1 | 551 | 100.0 |
| mass_running_order_kg | M (kg) | mapped | INTEGER | 0.1 | 1723 | 100.0 |
| energy_wh_km | Z (Wh/km) | mapped | INTEGER | 98.1 | 81 | 1.4 |
| electric_range_km | — | absent | — | 100.0 | 0 | 0.0 |
| fuel_consumption_l_100km | Fc | empty | — | 100.0 | 0 | 0.0 |
| year | Year | mapped | INTEGER | 0.0 | 1 | 100.0 |

### 2018 (F, discodata)

Live header: `ID, MS, Mp, VFN, Mh, Man, MMS, TAN, T, Va, Ve, Mk, Cn, Ct, Cr, M (kg), Mt, Enedc (g/km), Ewltp (g/km), W (mm), At1 (mm), At2 (mm), Ft, Fm, Ec (cm3), Ep (KW), Z (Wh/km), IT, Ernedc (g/km), Erwltp (g/km), De, Vf, R, Year, Status, Version_file, E (g/km), Er (g/km), Zr, Dr, Fc`

| key | live column | state | type | null % | distinct | coverage % (reg.) |
|---|---|---|---|---|---|---|
| make | Mk | mapped | TEXT | 0.0 | 56 | 100.0 |
| model | Cn | mapped | MIXED | 0.2 | 10848 | 100.0 |
| type_approval | T | mapped | MIXED | 0.5 | 884 | 99.9 |
| variant | Va | mapped | MIXED | 0.5 | 6293 | 100.0 |
| version | Ve | mapped | MIXED | 0.5 | 20117 | 100.0 |
| fuel | Ft | mapped | TEXT | 0.3 | 9 | 99.8 |
| fuel_mode | Fm | mapped | MIXED | 0.0 | 6 | 100.0 |
| displacement_cc | Ec (cm3) | mapped | INTEGER | 0.7 | 294 | 99.0 |
| power_kw | Ep (KW) | mapped | INTEGER | 16.6 | 286 | 86.4 |
| co2_wltp | Ewltp (g/km) | mapped | INTEGER | 41.6 | 341 | 30.4 |
| co2_nedc | Enedc (g/km) | mapped | INTEGER | 0.9 | 320 | 99.9 |
| wheelbase_mm | W (mm) | mapped | INTEGER | 0.2 | 489 | 100.0 |
| mass_running_order_kg | M (kg) | mapped | INTEGER | 0.0 | 1793 | 100.0 |
| energy_wh_km | Z (Wh/km) | mapped | INTEGER | 98.5 | 132 | 2.8 |
| electric_range_km | — | absent | — | 100.0 | 0 | 0.0 |
| fuel_consumption_l_100km | Fc | empty | — | 100.0 | 0 | 0.0 |
| year | Year | mapped | INTEGER | 0.0 | 1 | 100.0 |

### 2019 (F, discodata)

Live header: `ID, MS, Mp, VFN, Mh, Man, MMS, TAN, T, Va, Ve, Mk, Cn, Ct, Cr, M (kg), Mt, Enedc (g/km), Ewltp (g/km), W (mm), At1 (mm), At2 (mm), Ft, Fm, Ec (cm3), Ep (KW), Z (Wh/km), IT, Ernedc (g/km), Erwltp (g/km), De, Vf, R, Year, Status, Version_file, E (g/km), Er (g/km), Zr, Dr, Fc`

| key | live column | state | type | null % | distinct | coverage % (reg.) |
|---|---|---|---|---|---|---|
| make | Mk | mapped | TEXT | 0.0 | 61 | 100.0 |
| model | Cn | mapped | MIXED | 0.2 | 8539 | 100.0 |
| type_approval | T | mapped | MIXED | 0.2 | 1724 | 100.0 |
| variant | Va | mapped | MIXED | 1.9 | 6268 | 99.9 |
| version | Ve | mapped | MIXED | 1.9 | 23615 | 99.9 |
| fuel | Ft | mapped | TEXT | 0.0 | 11 | 100.0 |
| fuel_mode | Fm | mapped | TEXT | 0.1 | 6 | 100.0 |
| displacement_cc | Ec (cm3) | mapped | INTEGER | 0.7 | 293 | 97.7 |
| power_kw | Ep (KW) | mapped | INTEGER | 14.0 | 322 | 87.4 |
| co2_wltp | Ewltp (g/km) | mapped | INTEGER | 9.5 | 370 | 96.7 |
| co2_nedc | Enedc (g/km) | mapped | INTEGER | 0.6 | 379 | 100.0 |
| wheelbase_mm | W (mm) | mapped | INTEGER | 1.5 | 409 | 99.9 |
| mass_running_order_kg | M (kg) | mapped | INTEGER | 0.0 | 1831 | 100.0 |
| energy_wh_km | Z (Wh/km) | mapped | INTEGER | 98.4 | 182 | 3.6 |
| electric_range_km | — | absent | — | 100.0 | 0 | 0.0 |
| fuel_consumption_l_100km | Fc | empty | — | 100.0 | 0 | 0.0 |
| year | Year | mapped | INTEGER | 0.0 | 1 | 100.0 |

### 2020 (F, discodata)

Live header: `ID, MS, Mp, VFN, Mh, Man, MMS, TAN, T, Va, Ve, Mk, Cn, Ct, Cr, M (kg), Mt, Enedc (g/km), Ewltp (g/km), W (mm), At1 (mm), At2 (mm), Ft, Fm, Ec (cm3), Ep (KW), Z (Wh/km), IT, Ernedc (g/km), Erwltp (g/km), De, Vf, R, Year, Status, Version_file, E (g/km), Er (g/km), Zr, Dr, Fc`

| key | live column | state | type | null % | distinct | coverage % (reg.) |
|---|---|---|---|---|---|---|
| make | Mk | mapped | TEXT | 0.0 | 66 | 100.0 |
| model | Cn | mapped | MIXED | 0.7 | 8497 | 99.9 |
| type_approval | T | mapped | MIXED | 0.2 | 1448 | 100.0 |
| variant | Va | mapped | MIXED | 1.4 | 5350 | 99.9 |
| version | Ve | mapped | MIXED | 2.0 | 27079 | 99.9 |
| fuel | Ft | mapped | TEXT | 0.0 | 13 | 100.0 |
| fuel_mode | Fm | mapped | TEXT | 0.0 | 6 | 100.0 |
| displacement_cc | Ec (cm3) | mapped | INTEGER | 0.9 | 302 | 94.1 |
| power_kw | Ep (KW) | mapped | INTEGER | 10.0 | 335 | 89.8 |
| co2_wltp | Ewltp (g/km) | mapped | INTEGER | 2.4 | 391 | 99.8 |
| co2_nedc | Enedc (g/km) | mapped | INTEGER | 0.5 | 382 | 100.0 |
| wheelbase_mm | W (mm) | mapped | INTEGER | 1.6 | 413 | 99.9 |
| mass_running_order_kg | M (kg) | mapped | INTEGER | 0.1 | 1828 | 100.0 |
| energy_wh_km | Z (Wh/km) | mapped | INTEGER | 97.3 | 245 | 9.6 |
| electric_range_km | — | absent | — | 100.0 | 0 | 0.0 |
| fuel_consumption_l_100km | Fc | empty | — | 100.0 | 0 | 0.0 |
| year | Year | mapped | INTEGER | 0.0 | 1 | 100.0 |

### 2021 (F, discodata)

Live header: `ID, MS, Mp, VFN, Mh, Man, MMS, TAN, T, Va, Ve, Mk, Cn, Ct, Cr, M (kg), Mt, Enedc (g/km), Ewltp (g/km), W (mm), At1 (mm), At2 (mm), Ft, Fm, Ec (cm3), Ep (KW), Z (Wh/km), IT, Ernedc (g/km), Erwltp (g/km), De, Vf, R, Year, Status, Version_file, E (g/km), Er (g/km), Zr, Dr, Fc`

| key | live column | state | type | null % | distinct | coverage % (reg.) |
|---|---|---|---|---|---|---|
| make | Mk | mapped | TEXT | 0.0 | 68 | 100.0 |
| model | Cn | mapped | MIXED | 0.2 | 4717 | 99.9 |
| type_approval | T | mapped | MIXED | 0.3 | 1299 | 100.0 |
| variant | Va | mapped | MIXED | 1.6 | 4894 | 99.8 |
| version | Ve | mapped | MIXED | 2.0 | 26281 | 99.8 |
| fuel | Ft | mapped | TEXT | 0.0 | 11 | 100.0 |
| fuel_mode | Fm | mapped | TEXT | 0.0 | 6 | 100.0 |
| displacement_cc | Ec (cm3) | mapped | INTEGER | 2.5 | 267 | 90.5 |
| power_kw | Ep (KW) | mapped | INTEGER | 5.0 | 343 | 98.5 |
| co2_wltp | Ewltp (g/km) | mapped | INTEGER | 1.8 | 396 | 99.9 |
| co2_nedc | Enedc (g/km) | mapped | INTEGER | 95.6 | 37 | 16.5 |
| wheelbase_mm | W (mm) | mapped | INTEGER | 1.8 | 409 | 99.9 |
| mass_running_order_kg | M (kg) | mapped | INTEGER | 0.0 | 1772 | 100.0 |
| energy_wh_km | Z (Wh/km) | mapped | INTEGER | 94.8 | 254 | 18.4 |
| electric_range_km | — | absent | — | 100.0 | 0 | 0.0 |
| fuel_consumption_l_100km | Fc | mapped | REAL | 17.8 | 163 | 84.3 |
| year | Year | mapped | INTEGER | 0.0 | 1 | 100.0 |

### 2022 (P, discodata)

Live header: `ID, MS, Mp, VFN, Mh, Man, MMS, TAN, T, Va, Ve, Mk, Cn, Ct, Cr, M (kg), Mt, Enedc (g/km), Ewltp (g/km), W (mm), At1 (mm), At2 (mm), Ft, Fm, Ec (cm3), Ep (KW), Z (Wh/km), IT, Ernedc (g/km), Erwltp (g/km), De, Vf, R, Year, Status, Version_file, E (g/km), Er (g/km), Zr, Dr, Fc`

| key | live column | state | type | null % | distinct | coverage % (reg.) |
|---|---|---|---|---|---|---|
| make | Mk | mapped | TEXT | 0.0 | 134 | 100.0 |
| model | Cn | mapped | MIXED | 3.1 | 6808 | 98.4 |
| type_approval | T | mapped | MIXED | 0.3 | 1497 | 100.0 |
| variant | Va | mapped | MIXED | 1.3 | 4897 | 99.8 |
| version | Ve | mapped | MIXED | 2.8 | 22741 | 99.7 |
| fuel | Ft | mapped | TEXT | 0.0 | 11 | 100.0 |
| fuel_mode | Fm | mapped | TEXT | 0.0 | 6 | 100.0 |
| displacement_cc | Ec (cm3) | mapped | INTEGER | 3.1 | 276 | 87.1 |
| power_kw | Ep (KW) | mapped | INTEGER | 0.5 | 359 | 99.7 |
| co2_wltp | Ewltp (g/km) | mapped | INTEGER | 1.3 | 368 | 99.9 |
| co2_nedc | Enedc (g/km) | mapped | INTEGER | 93.8 | 39 | 15.7 |
| wheelbase_mm | W (mm) | mapped | INTEGER | 1.5 | 464 | 99.9 |
| mass_running_order_kg | M (kg) | mapped | INTEGER | 0.0 | 2016 | 100.0 |
| energy_wh_km | Z (Wh/km) | mapped | INTEGER | 91.3 | 259 | 22.2 |
| electric_range_km | — | absent | — | 100.0 | 0 | 0.0 |
| fuel_consumption_l_100km | Fc | mapped | REAL | 15.4 | 195 | 82.9 |
| year | Year | mapped | INTEGER | 0.0 | 1 | 100.0 |

### Sanity checks

| year | shard rows | rows of another year | HTML / error bodies |
|---|---|---|---|
| 2010 | 122553 | 0 | 0 |
| 2011 | 106471 | 0 | 0 |
| 2012 | 95658 | 0 | 0 |
| 2013 | 100802 | 0 | 0 |
| 2014 | 93492 | 0 | 0 |
| 2015 | 104204 | 0 | 0 |
| 2016 | 104897 | 0 | 0 |
| 2017 | 106481 | 0 | 0 |
| 2018 | 169105 | 0 | 0 |
| 2019 | 246100 | 0 | 0 |
| 2020 | 235021 | 0 | 0 |
| 2021 | 173428 | 0 | 0 |
| 2022 | 163851 | 0 | 0 |
| 2025 | — | — | — |

DISCODATA bodies are parsed only through `eea_response` (H1): an HTML page, an error message or a body without `results` is a query error, never a year without cars.

### Per-year failures

- 2025: stopped (no_csv_link)
