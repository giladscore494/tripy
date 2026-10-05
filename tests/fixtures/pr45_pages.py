"""Fixtures for PR #45 (IL version-page resolver + precision fixes). Pages are written from the production run
20261005T011156Z (Audi, 7 vehicles) as read through the MCP; NOT production artifacts unless stated.

    a4_530178        auto.co.il /cars/audi/a4/2020/530178/ (d_192b422774e183cd): its title as stored, HTML entities
                     escaped twice ("אוט&#x27;, 2.0 ל&#x27; טורבו"), its "label / (unit) / description / value" blocks
                     (1984 cc, 245 hp, 4X4). The production verdict was `displacement_absent`
    a6_506502        auto.co.il /cars/audi/a6/2018/506502/ (d_75f7e96b637d1479): one version page whose rows are
                     internally mixed (1984 cc next to 341 hp and 51 kgm)
    a6_319_pdf       static.auto.co.il/media/rfhhvopt/319.pdf (d_6b6887dbb101022c): the Audi A6 catalog, two columns (55 TFSI
                     V6 2995 cc 340 hp; 45 TFSI 2.0 245 hp), one torque value that belongs to the V6; text as production
                     extracted it (visual order, page 3 and the model-code table of page 4)
    q3 sportback     the cartube Q3 Sportback version page (…/61-אודי-q3-ספורטבק/343-2-0-40tfsi-s-line-4x4-2024): the same
                     spec rows as the plain Q3 page (body row "קרוסאובר", 190 hp), length 4449 / top speed 220
"""

from __future__ import annotations

from fixtures.pr44_pages import NAV, Q3_CARTUBE_URL, Q3_MODEL_URL, _block, _rows  # noqa: F401

# --- targets (Level 1.5 records of the benchmark snapshot) ------------------------------------------------------------

A4_PAYLOAD = {"identity": {"manufacturer": "אאודי", "commercial_name": "A4", "year": 2020, "trim": "S-LINE",
                           "model_code": "B8-8WCCAY", "government_record_id": "13362",
                           "government_codes": {"tozeret_cd": 19, "degem_cd": 994, "sug_degem": "P"}},
              "engine_drivetrain": {"engine_cc": 1984, "power_hp": 265, "propulsion_normalized": "conventional",
                                    "drivetrain_normalized": "awd", "automatic": 1},
              "structure": {"body_normalized": "sedan"}}
A6_PAYLOAD = {"identity": {"manufacturer": "אאודי", "commercial_name": "A6", "year": 2018, "trim": "LIMITED",
                           "model_code": "4GC08Y", "government_record_id": "12949",
                           "government_codes": {"tozeret_cd": 19, "degem_cd": 763, "sug_degem": "P"}},
              "engine_drivetrain": {"engine_cc": 1984, "power_hp": 252, "propulsion_normalized": "conventional",
                                    "drivetrain_normalized": "awd", "automatic": 1},
              "structure": {"body_normalized": "sedan"}}
A1_PAYLOAD = {"identity": {"manufacturer": "אאודי", "commercial_name": "A1 SPORTBACK", "year": 2022,
                           "trim": "STYLE COMFRT", "model_code": "GBAAMG", "government_record_id": "29465",
                           "government_codes": {"tozeret_cd": 268, "degem_cd": 101, "sug_degem": "P"}},
              "engine_drivetrain": {"engine_cc": 999, "power_hp": 110, "propulsion_normalized": "conventional",
                                    "drivetrain_normalized": "two_wheel_drive", "automatic": 1},
              "structure": {"body_normalized": "hatchback"}}

# --- R2: the A4 auto.co.il version page --------------------------------------------------------------------------------

A4_530178_URL = "https://www.auto.co.il/cars/audi/a4/2020/530178/"
A4_VERSION = "45, אוט', 2.0 ל' טורבו, S-Line, 4x4"
# the title as production stored it (meta title, entities not decoded): "&#x27;" for every apostrophe
A4_TITLE_ESCAPED = "אודי A4 2020 45, אוט&#x27;, 2.0 ל&#x27; טורבו, S-Line, 4x4 יד שניה"

POWER_HELP = ('לכל מנוע יש שיא כוח, ההספק המקסימלי שהוא יכול לייצר. החישוב במכוניות עם מנוע בעירה פנימית הוא שסוס אחד שווה '
              'ערך להנפה של 75 ק"ג לגובה של מטר. במכוניות חשמליות, סוס שווה ערך ל-760 ואט')
TORQUE_HELP = ('בישראל נתון המומנט נמדד בקילוגרם למטר. מנוע עם מומנט גבוה יותר ובסל"ד נמוך יותר יהיה הרבה יותר גמיש וחזק '
               'ביום יום')


def auto_version_html(*, title: str, h1: str, version: str, cc: str, power: str, torque: str, body: str = "סדאן",
                      drive: str = "כפולה (4X4)", gearbox: str = "אוטומטית רובוטית כפולת מצמד",
                      length: str = "476", top_speed: str = "250") -> str:
    return f"""<html><head><title>{title}</title></head><body>{NAV}<main>
<nav class="crumbs"><a>אוטו</a><a>אודי</a><a>{h1}</a><a>{version}</a></nav>
<h1>{h1}</h1><h2>{version}</h2><p>יד שניה</p><p>הדגם אינו משווק כרכב חדש</p>
<section><h3>תעודת זהות</h3><ul>{_block("שנת דגם", h1.split()[-1])}{_block("מקומות ישיבה", "5")}
{_block("מספר דלתות", "4")}{_block("מרכב", body)}</ul></section>
<section><h3>מידות</h3><p>{h1}</p><p>{version}</p><ul>
{_block("אורך", length, '(ס"מ)')}{_block("רוחב", "185", '(ס"מ)')}{_block("גובה", "138", '(ס"מ)')}
{_block("משקל", "1620", '(ק"ג)')}{_block("נפח מכל דלק", "58", "(ליטר)")}</ul></section>
<section><h3>מנוע וביצועים</h3><p>{h1}</p><p>{version}</p><ul>
{_block("נפח מנוע", cc, '(סמ"ק)')}{_block("סוג מנוע", "טורבו בנזין")}{_block("תצורת מנוע", "טורי")}
{_block("מספר צילינדרים", "4")}{_block("הנעה", drive)}
{_block("הספק מירבי", power, '(כ"ס)', POWER_HELP)}
{_block("מומנט מירבי", torque, '(קג"מ)', TORQUE_HELP)}
{_block('0 ל-100 קמ"ש', "5.8", "(שניות)")}{_block("מהירות מירבית", top_speed, '(קמ"ש)')}
{_block("תיבת הילוכים", gearbox)}{_block("מספר הילוכים", "7")}</ul></section>
</main><footer><p>מגזין אוטו - בוחנים מכוניות משנת 1986</p></footer></body></html>"""


def a4_530178_html() -> str:
    return auto_version_html(title=A4_TITLE_ESCAPED, h1="אודי A4 2020", version=A4_VERSION, cc="1984", power="245",
                             torque="37.7")


# --- R5: the internally mixed A6 used-car page --------------------------------------------------------------------------

A6_506502_URL = "https://www.auto.co.il/cars/audi/a6/2018/506502/"
A6_VERSION = "TFSI 45 אוט', 2.0 ל' טורבו, 4x4"


def a6_506502_html() -> str:
    return auto_version_html(title=f"אודי A6 2018 {A6_VERSION} יד שניה", h1="אודי A6 2018", version=A6_VERSION,
                             cc="1984", power="341", torque="51", length="494")


# --- R4: the A6 catalog PDF (two version columns, one V6 torque value) -------------------------------------------------

A6_319_URL = "https://static.auto.co.il/media/rfhhvopt/319.pdf"
A6_319_TORQUE_QUOTE = 'מומנט מירבי (סל"ד/קג"מ) 51.0/1,370-4,500'
A6_319_TEXT = """[page 1]
A6
( 2834) *Audi םיפסונ םיטרפל
*

[page 3]
A6 Design 55 TFSI quattro A6 Design 45 TFSI quattro*
היגולונכט
V6 cylinder gasoline engine with gasoline direct injection
and turbocharging, Mild Hybrid
Electric Vehicle 48V )MHEV)
םיכוליה תביתו עונמ
quattro quattro הענה
םיכוליה 7 - S tronic םיכוליה 7 - S tronic םיכוליה תבית
2,995 2.0 TFSI )ק"מס( עונמ חפנ
V6 4 םירדניליצ 'סמ
51.0/1,370-4,500 )מ"גק/ד"לס( יברימ טנמומ
340/5,000-6,400 245 )ס"כ/ד"לס( יברימ קפסה
םיעוציב
250 250 )ש"מק( תיברימ תוריהמ
5.1 )תוינש( ש"מק 0-100 הצואת
םילקשמ
1,845 )ג"ק( ימצע לקשמ
תודימ
4,939 4,939 )מ"מ( ךרוא
1,886 1,886 )מ"מ( בחור
2,933 )מ"מ( םינרס קחרמ
73 73 )רטיל( קלד לכימ תלוביק

[page 4]
:יתוחיטבה רוזבאה תמר
יתוחיטבה רוזבאה תמר םגד רואית םגד דוק
הכומנ ההובג 7 A6 45 TFSI 4A2C7Y
7 A6 55 TFSI 4A2C2Y
3.12.18 הספדה דעומ
"""

# --- R3: the cartube Q3 Sportback version page --------------------------------------------------------------------------

Q3_SPORTBACK_URL = ("https://www.cartube.co.il/מחירון-רכב-חדש/10-אודי/61-אודי-q3-ספורטבק/"
                    "343-2-0-40tfsi-s-line-4x4-2024")


def cartube_version_html(*, model: str, version: str, power: str = "190", body: str = "קרוסאובר",
                         length: str = "4484", top_speed: str = "222") -> str:
    rows = [("סוג מרכב", body), ("הספק", f"{power} כ״ס"), ("הנעה", "4X4"), ("סוג מנוע", "טורבו בנזין"),
            ("תיבת הילוכים", "רובוטית כפולת מצמדים"), ("מספר הילוכים", "7"), ("נפח מנוע", "1,984 סמ״ק"),
            ("אורך", f"{length} מ״מ"), ("מהירות מרבית", f"{top_speed} קמ״ש")]
    return f"""<html><head><title>cartube - מחירון רכב ומפרט טכני | 2024 | אודי {model} {version}</title></head>
<body>{NAV}<main><h1>אודי {model} {version}</h1><p>2L • טורבו בנזין</p><h2>ביצועים</h2>
<section class="specs">{_rows(rows)}</section></main></body></html>"""


def q3_sportback_html() -> str:
    return cartube_version_html(model="Q3 ספורטבק", version="2.0 40TFSI S-LINE 4X4", length="4449", top_speed="220")


def q3_plain_html() -> str:
    return cartube_version_html(model="Q3", version="2.0 40TFSI 4X4")
