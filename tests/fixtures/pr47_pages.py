"""Fixtures for PR #47 (IL source precision: body aliases, the new cartube template, carzone). Written from the production
run 20261005T210701Z-glm-5.3-flash-one (Toyota Corolla 2024 BUSINESS EDI, record 38626) as read through the MCP: page
texts (document_text), search results (the il_version_pages event) and the carzone compare table (document_structure).
The HTML around the texts is reconstructed (the MCP serves extracted text, not the raw HTML); every label, value and
title is the production text. Only the pages marked SYNTHETIC are not production pages.

    WAGON_URL / cartube_wagon_html()   cartube's NEW template (no numeric ids in make / model segments):
                                       /מחירון-רכב-חדש/טויוטה/טויוטה-קורולה-סטיישן/3952-... (d_2f41c2d9748881fd), its
                                       "label (unit):" rows on one line and the value on the next; 140 hp (system power)
    SENSE_URL / cartube_sense_html()   cartube's OLD template /36-טויוטה/199-טויוטה-קורולה/2068-... (d_1c3957f8b889db29):
                                       the Sense SEDAN page, "label" / "value unit" blocks
    COMPARE_URL / carzone_compare_html()  carzone /Toyota/Corolla/Space/2024/compare (d_a8222798db07d8e4): four columns
                                       (no trim / ביזנס אדישן / קומפורט / קומפורט ביזנס), "-" for no value
    TRIM_URL / carzone_trim_html()     carzone ...?trim=Business_Edition-hybrid-1.8L-AT-4X2-5seats (d_756fe74260c27b3f):
                                       the static HTML shows the DEFAULT version ("היברידי 1.8 ליטר"), not the slug's
    carzone_trim_html(confirmed=True)  SYNTHETIC: the same page whose breadcrumb / H1 name the slug's version
    SEARCH_RESULTS                     the run's site-search results per domain (url, title), in rank order
"""

from __future__ import annotations

from urllib.parse import quote

PAYLOAD_38626 = {
    "identity": {"manufacturer": "טויוטה", "manufacturer_entity": "טויוטה אנגליה", "commercial_name": "COROLLA",
                 "year": 2024, "trim": "BUSINESS EDI", "model_code": "ZWE211L DWXNBW", "segment": "private",
                 "government_record_id": "38626",
                 "government_codes": {"tozeret_cd": 430, "degem_cd": 139, "sug_degem": "P"}},
    "engine_drivetrain": {"engine_cc": 1798, "power_hp": 98, "fuel": "בנזין", "fuel_normalized": "petrol",
                          "propulsion_technology": "היברידי רגיל", "propulsion_normalized": "hybrid", "drive": "4X2",
                          "drivetrain_normalized": "two_wheel_drive", "automatic": 1},
    "structure": {"body": "סטיישן", "body_normalized": "wagon", "doors": 4, "seats": 5, "gross_weight_kg": 1835},
}


def _url(host: str, path: str) -> str:
    return f"https://{host}" + quote(path, safe="/:-?=&_.")


CARTUBE = "www.cartube.co.il"
WAGON_URL = _url(CARTUBE, "/מחירון-רכב-חדש/טויוטה/טויוטה-קורולה-סטיישן/3952-טויוטה-קורולה-סטיישן-1-8-הייבריד")
WAGON_LISTING_URL = _url(CARTUBE, "/מחירון-רכב-חדש/טויוטה/טויוטה-קורולה-סטיישן")
SENSE_URL = _url(CARTUBE, "/מחירון-רכב-חדש/36-טויוטה/199-טויוטה-קורולה/2068-1-8-היברידי-sense-2024")
SENSE_SEARCH_URL = _url(CARTUBE, "/מחירון-רכב-חדש/טויוטה/טויוטה-קורולה/2595-טויוטה-קורולה-1-8-היברידי-sense")
COROLLA_2026_URL = _url(CARTUBE, "/מחירון-רכב-חדש/טויוטה/טויוטה-קורולה/6635-טויוטה-קורולה-1-8-היברידי")
CROSS_URL = _url(CARTUBE, "/מחירון-רכב-חדש/טויוטה/טויוטה-קורולה-קרוס/6633-טויוטה-קורולה-קרוס-1-8-היברידי-active")
SUN_URL = _url(CARTUBE, "/מחירון-רכב-חדש/טויוטה/טויוטה-קורולה/4211-טויוטה-קורולה-1-8-היברידי-sun")
ICAR_SUN_URL = _url("www.icar.co.il", "/טויוטה/טויוטה_קורולה/טויוטה_קורולה_יד_שניה_ד13/version28973/")
AUTO_CROSS_URL = "https://www.auto.co.il/cars/toyota/corolla-cross/2024/492857/"
AUTO_SUN_URL = "https://www.auto.co.il/cars/toyota/corolla/2024/492865/"
AUTO_SENSE_URL = "https://www.auto.co.il/cars/toyota/corolla/2024/492866/"
AUTO_LISTING_URL = "https://www.auto.co.il/cars/toyota/corolla/2024/"
AUTO_CROSS_LISTING_URL = "https://www.auto.co.il/cars/toyota/corolla-cross/2024/"

CARZONE = "https://www.carzone.co.il"
MODEL_URL = f"{CARZONE}/Toyota/Corolla/Space/2024"
COMPARE_URL = f"{CARZONE}/Toyota/Corolla/Space/2024/compare"
TRIM_SLUG = "Business_Edition-hybrid-1.8L-AT-4X2-5seats"
TRIM_URL = f"{MODEL_URL}?trim={TRIM_SLUG}"
COMFORT_TRIM_URL = f"{MODEL_URL}?trim=Comfort-hybrid-1.8L-AT-4X2-5seats"

# the run's site-search results (il_version_pages event, line 17), per domain in rank order: (url, title)
SEARCH_RESULTS = {
    "cartube.co.il": [
        (SENSE_SEARCH_URL, "טויוטה קורולה 1.8 היברידי Sense 2024 | מפרט טכני"),
        (COROLLA_2026_URL, "טויוטה קורולה 1.8 היברידי 2026 | מפרט טכני"),
        (_url(CARTUBE, "/מחירון-רכב-חדש/טויוטה/טויוטה-קורולה"), "טויוטה קורולה | מחירון ומפרט טכני"),
        (_url(CARTUBE, "/חדשות-רכב/חזרה-לישראל-2024-טויוטה-קורולה-סטיישן-במחיר-של-179990-שקל"),
         "חזרה לישראל: 2024 טויוטה קורולה סטיישן במחיר של 179990 שקל"),
        (WAGON_URL, "2024 מתיחת פנים | טויוטה קורולה סטיישן 1.8 הייבריד"),
        (CROSS_URL, "טויוטה קורולה קרוס 1.8 היברידי Active 2026 | מפרט טכני"),
        (SUN_URL, "טויוטה קורולה 1.8 היברידי Sun 2025 | מפרט טכני"),
        (_url(CARTUBE, "/מחירון-רכב-חדש/טויוטה/טויוטה-קורולה-קרוס"), "טויוטה קורולה קרוס | מחירון ומפרט טכני"),
    ],
    "icar.co.il": [
        (ICAR_SUN_URL, "טויוטה קורולה 2024 יד שניה 1.8 היברידי Sun"),
        (_url("www.icar.co.il", "/טויוטה/טויוטה_קורולה/טויוטה_קורולה_יד_שניה_ד13/"),
         "מידע מקיף ומקצועי על טויוטה קורולה 2019-2025"),
        (_url("www.icar.co.il", "/טויוטה/טויוטה_קורולה_קרוס/טויוטה_קורולה_קרוס_יד_שניה_ד10/"),
         "מידע מקיף ומקצועי על טויוטה קורולה קרוס 2022-2025"),
        (_url("www.icar.co.il", "/טויוטה/טויוטה_קורולה_סטיישן/טויוטה_קורולה_סטיישן_יד_שניה_ד10/"),
         "מידע מקיף ומקצועי על טויוטה קורולה סטיישן 2019-2025"),
        (_url("www.icar.co.il", "/טויוטה/טויוטה_C-HR/טויוטה_C-HR_חדש/"), "חוות דעת מומחים על טויוטה C-HR"),
        (_url("www.icar.co.il", "/טויוטה/"), "המידע המקצועי והעדכני ביותר על רכבי טויוטה"),
    ],
    "auto.co.il": [
        (AUTO_CROSS_URL, "טויוטה קורולה קרוס 2024 1.8 ל' היברידי, Dynamic יד שניה"),
        (AUTO_SUN_URL, "טויוטה קורולה 2024 סדאן 1.8 ל' היברידי, SUN יד שניה"),
        (AUTO_SENSE_URL, "טויוטה קורולה 2024 סדאן 1.8 ל' היברידי, SENSE יד שניה"),
        (AUTO_LISTING_URL, "טויוטה קורולה 2024 יד שניה"),
        ("https://www.auto.co.il/cars/toyota/corolla/", "טויוטה קורולה - מחירון, חוות דעת ומידע טכני"),
        (AUTO_CROSS_LISTING_URL, "טויוטה קורולה קרוס 2024 יד שניה"),
        ("https://www.auto.co.il/cars/toyota/corolla-cross/", "טויוטה קורולה קרוס - מחירון, חוות דעת ומידע טכני"),
        ("https://www.auto.co.il/articles/test-drives/road-tests/131910/", "טויוטה קורולה – מבחן דרכים (1.8 ל', היברידי)"),
    ],
    # carzone was not configured in that run; its pages came from the agentic research (documents d_3bfeea8ee89878c6,
    # d_a8222798db07d8e4): the results a carzone site search returns are the model page and the compare page
    "carzone.co.il": [
        (MODEL_URL, "טויוטה קורולה קורולה ספייס 2024 - מחירון ומפרט | Carzone"),
        (COMPARE_URL, "השוואת גרסאות טויוטה קורולה ספייס 2024 | Carzone"),
    ],
}

NAV = ("<header><nav><ul><li>ראשי</li><li>מחירון רכב חדש</li><li>כל החדשות</li><li>טויוטה</li><li>יונדאי</li>"
       "</ul></nav></header>")


# --- cartube, NEW template (the wagon page) ---------------------------------------------------------------------------

WAGON_ROWS = [("דגם:", "טויוטה קורולה סטיישן 1.8 הייבריד"), ("סטטוס דגם:", "מתיחת פנים"), ("מספר דלתות:", "5"),
              ("מספר מושבים:", "5"), ("קבוצת שווי:", "4"), ("אחריות:", "שלוש שנים"), ("שנת השקה:", "2018"),
              ("השקה בישראל:", "6.10.2024")]
WAGON_ENGINE = [("הנעה:", "קדמית"), ("תיבת הילוכים:", "רציפה"), ("סוג מנוע:", "היברידי"), ("מספר בוכנות:", "4"),
                ('נפח מנוע (סמ"ק):', "1,798"), ('הספק מרבי (כ"ס):', "140"), ('סל"ד הספק מרבי:', "5,200"),
                ('מומנט מרבי (קג"מ):', "14.5"), ('סל"ד מומנט מרבי:', "3,600"), ('0-100 קמ"ש (שניות):', "9.4"),
                ('מהירות מרבית (קמ"ש):', "180"), ('צריכת דלק משולבת (ק"מ/ל\'):', "21.7"), ("דרגת זיהום:", "3")]
WAGON_DIMENSIONS = [('אורך (ס"מ):', "465.0"), ('רוחב (ס"מ):', "179.0"), ('גובה (ס"מ):', "146.0"),
                    ('בסיס גלגלים (ס"מ):', "270.0"), ("נפח תא מטען אחורי (ליטר):", "596"),
                    ("נפח תא מטען קיפול (ליטר):", "1,606"), ('משקל עצמי (ק"ג):', "1,400"),
                    ('כושר גרירה (ק"ג):', "450"), ('נגרר עם בלימה (ק"ג):', "750"), ("מידות צמיגים:", "205/55R16")]


def _dl(rows: list[tuple[str, str]]) -> str:
    return "<dl>" + "".join(f"<dt>{label}</dt><dd>{value}</dd>" for label, value in rows) + "</dl>"


def cartube_wagon_html() -> str:
    title = "טויוטה קורולה סטיישן 1.8 הייבריד 2024 מתיחת פנים | מפרט טכני - cartube"
    return f"""<html><head><title>{title}</title></head><body>{NAV}<main>
<nav class="crumbs"><a href="/">ראשי</a><a>מחירון רכב חדש</a><a>טויוטה</a>
<a href="{WAGON_LISTING_URL}">טויוטה קורולה סטיישן</a><a>טויוטה קורולה סטיישן 1.8 הייבריד</a></nav>
<p>2024</p><h1>טויוטה קורולה סטיישן 1.8 הייבריד</h1><h2>מפרט טכני</h2>
{_dl(WAGON_ROWS)}<h3>מערכת הנעה</h3>{_dl(WAGON_ENGINE)}<h3>מידות</h3>{_dl(WAGON_DIMENSIONS)}
<h3>אבזור נוחות</h3>{_dl([("גודל מסך:", '8.0"'), ("חלון גג:", "אין"), ("מרכב:", "סטיישן")])}
<section><h4>גולשים אחרים אהבו</h4><p>יונדאי טוסון פלאג-אין 2026 החדש נחת בישראל - מחיר החל 199,990 שקל</p></section>
</main><footer><p>זכויות יוצרים © 2026 cartube. כל הזכויות שמורות.</p></footer></body></html>"""


def cartube_wagon_listing_html() -> str:
    """The cartube model page of the wagon family (its version links; reconstructed from the wagon page's breadcrumb)."""
    return f"""<html><head><title>טויוטה קורולה סטיישן | מחירון ומפרט טכני - cartube</title></head><body>{NAV}<main>
<h1>טויוטה קורולה סטיישן</h1><ul>
<li><a href="{WAGON_URL}">טויוטה קורולה סטיישן 1.8 הייבריד</a></li>
<li><a href="{CROSS_URL}">טויוטה קורולה קרוס 1.8 היברידי Active</a></li></ul></main></body></html>"""


# --- cartube, OLD template (the Sense sedan page) -------------------------------------------------------------------

SENSE_ROWS = [("מספר מושבים", "5"), ("מספר דלתות", "4"), ("סוג מרכב", "סדאן"), ("הספק", "140 כ״ס"),
              ("0-100 קמ״ש", "9.30 שנ׳"), ("הנעה", "קדמית"), ("סוג מנוע", "היברידי"), ("תיבת הילוכים", "רציפה"),
              ("מומנט", "15 קג״מ"), ("נפח מנוע", "1,798 סמ״ק"), ("מהירות מרבית", "180 קמ״ש"),
              ("נפח תא מטען אחורי", "471 ליטר"), ("משקל עצמי", "1,355 ק״ג"), ("מידות צמיגים", "205/55R16")]


def cartube_sense_html() -> str:
    title = "cartube - מחירון רכב ומפרט טכני | 2024 | טויוטה קורולה 1.8 היברידי Sense"
    rows = "".join(f'<div class="spec-row"><span class="label">{label}</span><span class="value">{value}</span></div>'
                   for label, value in SENSE_ROWS)
    return f"""<html><head><title>{title}</title></head><body>{NAV}<main>
<nav class="crumbs"><a>ראשי</a><a>מחירון רכב חדש</a><a>טויוטה</a><a>קורולה (2024)</a><a>1.8 היברידי Sense</a></nav>
<p>היברידי</p><p>2024</p><h1>טויוטה קורולה 1.8 היברידי Sense</h1><p>1.8L • היברידי</p>
<section class="specs">{rows}</section></main></body></html>"""


# --- carzone: the compare page ----------------------------------------------------------------------------------------

COMPARE_TRIMS = ["היברידי 1.8 ליטר (דור 12 מעודכן)", "ביזנס אדישן היברידי 1.8 ליטר (דור 12 מעודכן)",
                 "קומפורט היברידי 1.8 ליטר (דור 12 מעודכן)", "קומפורט ביזנס היברידי 1.8 ליטר (דור 12 מעודכן)"]
# (section, [(label, [cell per column])]) as production extracted them (a subset of the 130 rows; every row used below)
COMPARE_SECTIONS = [
    ("מידע כללי", [
        ("דגם", ["טויוטה קורולה ספייס"] * 4), ("רמת גימור", COMPARE_TRIMS), ("שנתון", ["2024"] * 4),
        ("שנת השקה", ["2022", "2018", "2022", "2022"]), ("סוג מרכב", ["סטיישן"] * 4), ("מקומות", ["5"] * 4),
        ("סוג מנוע", ["היברידי בנזין אטמוספרי", "היברידי בנזין", "היברידי בנזין", "היברידי בנזין"]),
        ("כוח סוס", ["-"] * 4), ("הנעה", ["קדמית"] * 4), ("תיבת הילוכים", ["אוטומטי רציף"] * 4)]),
    ("מנוע וביצועים", [
        ("נפח מנוע", ["1,798 סמ״ק"] * 4), ("מומנט", ["-"] * 4), ("תאוצה 0-100", ["9.4 שנ׳", "-", "-", "-"]),
        ("מהירות מרבית", ["180 קמ״ש", "-", "-", "-"]), ("הספק מנוע חשמלי", ["95 כ״ס", "-", "-", "-"])]),
    ("בטיחות", [("רמת בטיחות", ["4", "1", "1", "1"]), ("כריות אוויר", ["7", "6", "7", "7"])]),
    ("מידות ומשקל", [
        ("תא מטען", ["596 ליטר"] * 4), ("מיכל דלק", ["43 ליטר", "-", "-", "-"]),
        ("ג׳אנטים", ["פלדה", "סגסוגת", "סגסוגת", "סגסוגת"]), ("קוטר גלגלים", ["16״", "-", "-", "-"]),
        ("צמיג קדמי", ["205/55R16", "-", "-", "-"]), ("צמיג אחורי", ["205/55R16", "-", "-", "-"]),
        ("משקל עצמי", ["1,400 ק״ג", "-", "-", "-"]),
        ("משקל כולל", ["1,885 ק״ג", "1,835 ק״ג", "1,835 ק״ג", "1,885 ק״ג"]),
        ("מרווח גחון", ["135 מ״מ", "-", "-", "-"])]),
    ("מולטימדיה", [("מולטימדיה", ["מקורית עם מסך מגע 8 אינץ'", "-", "-", "-"]),
                   ("חיבור סלולרי", ["Apple CarPlay + Android Auto", "-", "-", "-"])]),
    ("אבזור ונוחות", [("ריפוד", ["בד", "-", "-", "-"]), ("גג שמש", ["ללא"] * 4)]),
]


def carzone_compare_html() -> str:
    heads = "".join(f'<div class="col-head"><button>הסר</button><span>מלפנים שמאל</span><b>טויוטה קורולה ספייס</b>'
                    f"<span>{trim}</span><span>2024</span></div>" for trim in COMPARE_TRIMS)
    body = ""
    for section, rows in COMPARE_SECTIONS:
        body += f"<h3>{section}</h3>"
        for label, cells in rows:
            body += (f'<div class="cmp-row"><div class="cmp-label">{label}</div><div class="cmp-cells">'
                     + "".join(f'<div class="cmp-cell">{c}</div>' for c in cells) + "</div></div>")
    return f"""<html><head><title>השוואת גרסאות טויוטה קורולה ספייס 2024 | Carzone</title></head><body>
<header><nav><a>CARZONE</a><a>רכב חדש</a><a>השוואת רכבים</a></nav></header><main>
<nav class="crumbs"><a>ראשי</a><a>טויוטה קורולה דור 12 מתיחת פנים</a><a>קורולה ספייס</a><a>2024</a></nav>
<h1>השוואת גרסאות</h1><h2>טויוטה קורולה ספייס 2024</h2><div class="heads">{heads}</div>{body}</main>
<footer><p>© כל הזכויות שמורות 2026</p></footer></body></html>"""


# --- carzone: the model page and the trim page ------------------------------------------------------------------------

def carzone_model_html(*, current: str = "היברידי 1.8 ליטר", heading: str = "טויוטה קורולה ספייס") -> str:
    versions = [("היברידי 1.8 ליטר (דור 12 מתיחת פנים)", "179,990", MODEL_URL + "?trim=hybrid-1.8L-AT-4X2-5seats"),
                ("ביזנס אדישן היברידי 1.8 ליטר (דור 12)", "185,000", TRIM_URL),
                ("קומפורט היברידי 1.8 ליטר (דור 12 מתיחת פנים)", "175,000", COMFORT_TRIM_URL)]
    options = "".join(f'<li><a href="{url}">{name}</a><span>₪</span><span>{price}</span></li>'
                      for name, price, url in versions)
    return f"""<html><head><title>טויוטה קורולה קורולה ספייס 2024 - מחירון ומפרט | Carzone</title></head><body>
<header><nav><a>CARZONE</a><a>רכב חדש</a></nav></header><main>
<nav class="crumbs"><a>ראשי</a><a>טויוטה</a><a>קורולה</a><a>קורולה ספייס</a><a>2024</a><a>{current}</a></nav>
<h1>{heading}</h1><p>2024</p><div class="versions"><span>גירסה:</span><span>בחר גירסה</span><ul>{options}</ul></div>
<a href="{COMPARE_URL}">השוו גרסאות</a>
<section><h2>מפרט טכני</h2><h3>טויוטה קורולה ספייס</h3>
<dl><dt>מרכב</dt><dd>סטיישן</dd><dt>מקומות ישיבה</dt><dd>5</dd><dt>סוג מנוע</dt><dd>היברידי בנזין אטמוספרי</dd>
<dt>כוח סוס</dt><dd>140 כ״ס</dd><dt>נפח מנוע</dt><dd>1798 סמ״ק</dd><dt>הנעה</dt><dd>קדמית</dd>
<dt>תאוצה 0-100</dt><dd>9.4 שניות</dd></dl></section></main></body></html>"""


def carzone_trim_html(*, confirmed: bool = False) -> str:
    """The trim page as production fetched it (the default version rendered: the slug's trim is NOT on the page), or
    SYNTHETIC with confirmed=True (breadcrumb and H1 name the slug's version)."""
    if confirmed:
        return carzone_model_html(current="ביזנס אדישן היברידי 1.8 ליטר",
                                  heading="טויוטה קורולה ספייס ביזנס אדישן היברידי 1.8 ליטר")
    return carzone_model_html()
