"""Fixtures for PR #44 (Israeli source playbook). NOT production artifacts unless stated.

    q3_cartube        the cartube per-version page "2.0 40TFSI 4X4 2024" of Q3 record 94995 (d_fe0670724e1a6a52), rows
                      as production served them (read through the MCP): "סל״ד מומנט מרבי | 1,500" (the rpm the run admitted
                      as torque_nm = 1500), "מומנט | 33 קג״מ" (the real torque, 324 Nm) and the brief's synthetic row
                      "מומנט מרבי (קג"מ) | 32.6" (320 Nm)
    q8_auto_538115    the auto.co.il single-version page /cars/audi/q8/2025/538115/ (d_c0872158b2c6111a), its spec rows
                      as production served them, "label / description / value" blocks included, and the comparison
                      widgets that name other models' bodies ("GLE קופה")
"""

from __future__ import annotations

from fixtures.pr43_audi_pages import Q3_PAYLOAD, Q7_PAYLOAD, Q8_PAYLOAD  # noqa: F401

NAV = "<header><nav><ul><li>ראשי</li><li>מחירון רכב חדש</li><li>חדשות רכב</li></ul></nav></header>"

# --- P3: the Q3 cartube version page ----------------------------------------------------------------------------------

Q3_CARTUBE_URL = "https://www.cartube.co.il/מחירון-רכב-חדש/10-אודי/49-אודי-q3/327-2-0-40tfsi-4x4-2024"


def _rows(rows: list[tuple[str, str]]) -> str:
    return "\n".join(f'<div class="spec-row"><span class="label">{label}</span><span class="value">{value}</span></div>'
                     for label, value in rows)


def q3_cartube_html(*, synthetic_row: bool = True, version: str = "2.0 40TFSI 4X4", power: str = "190",
                    body: str = "קרוסאובר") -> str:
    rows = [("סוג מרכב", body), ("הספק", f'{power} כ״ס'), ("0-100 קמ״ש", "7.30 שנ׳"), ("הנעה", "4X4"), ("סוג מנוע", "טורבו בנזין"),
            ("תיבת הילוכים", "רובוטית כפולת מצמדים"), ("מומנט", "33 קג״מ"), ("מספר הילוכים", "7"),
            ("סל״ד מומנט מרבי", "1,500"), ("מספר בוכנות", "4"), ("נפח מנוע", "1,984 סמ״ק"),
            ("סל״ד הספק מרבי", "4,200"), ("מהירות מרבית", "222 קמ״ש")]
    if synthetic_row:
        rows.append(('מומנט מרבי (קג"מ)', "32.6"))
    return f"""<html><head><title>cartube - מחירון רכב ומפרט טכני | 2024 | אודי Q3 {version}</title></head>
<body>{NAV}<main><h1>אודי Q3 {version}</h1><p>2L • טורבו בנזין</p><h2>ביצועים</h2>
<section class="specs">{_rows(rows)}</section></main></body></html>"""


# --- P4 / P2: the Q8 auto.co.il single-version page ---------------------------------------------------------------------

Q8_538115_URL = "https://www.auto.co.il/cars/audi/q8/2025/538115/"
Q8_VERSION = "55 TFSIe אוט', 3.0 ל' טורבו, פלאג אין הייבריד, S-Line ,4x4"


def _block(label: str, value: str, unit: str = "", description: str = "") -> str:
    unit_html = f"<span class=\"unit\">{unit}</span>" if unit else ""
    desc_html = f"<div class=\"tooltip\"><b>{label}</b><p>{description}</p></div>" if description else ""
    return (f'<li class="spec"><div class="spec-label"><span>{label}</span>{unit_html}{desc_html}</div>'
            f'<div class="spec-value">{value}</div></li>')


def q8_538115_html(*, power: str = "394") -> str:
    return f"""<html><head><title>אודי Q8 2025 {Q8_VERSION} יד שניה</title></head><body>{NAV}<main>
<nav class="crumbs"><a>אוטו</a><a>אודי</a><a>Q8</a><a>2025</a><a>{Q8_VERSION}</a></nav>
<h1>אודי Q8 2025</h1><h2>{Q8_VERSION}</h2><p>יד שניה</p><p>הדגם אינו משווק כרכב חדש</p>
<section><h3>תעודת זהות</h3><ul>
{_block("שנת דגם", "2025")}{_block('שנת השקת הדגם בחו"ל', "2018")}{_block("מקומות ישיבה", "5")}
{_block("מספר דלתות", "5")}{_block("מרכב", "SUV")}</ul></section>
<section><h3>מידות</h3><p>אודי Q8</p><p>{Q8_VERSION}</p><ul>
{_block("אורך", "499", '(ס"מ)')}{_block("רוחב", "200", '(ס"מ)')}{_block("גובה", "163", '(ס"מ)')}
{_block("בסיס גלגלים", "300", '(ס"מ)', "המרחק בין טבור הגלגל הקדמי לזה האחורי, לרוב מצביע על גודל המרחב בתא הנוסעים")}
{_block("משקל", "2415", '(ק"ג)')}{_block("נפח תא מטען", "439", "(ליטר)")}{_block("נפח מכל דלק", "75", "(ליטר)")}</ul></section>
<section><h3>מנוע וביצועים</h3><p>אודי Q8</p><p>{Q8_VERSION}</p><ul>
{_block("נפח מנוע", "2995", '(סמ"ק)')}{_block("סוג מנוע", "היברידי פלאג-אין (נטען)")}{_block("תצורת מנוע", "V")}
{_block("מספר צילינדרים", "6")}{_block("הנעה", "כפולה (4X4)")}
{_block("הספק מירבי", power, '(כ"ס)', 'לכל מנוע יש שיא כוח, ההספק המקסימלי שהוא יכול לייצר. החישוב במכוניות עם מנוע בעירה פנימית הוא שסוס אחד שווה '
       'ערך להנפה של 75 ק"ג לגובה של מטר. במכוניות חשמליות, סוס שווה ערך ל-760 ואט')}
{_block('סל"ד להספק מירבי', "5200", "(סיבובים/לדקה)", "באיזו מהירות צריך להסתובב המנוע (בעירה פנימית) כדי לייצר את שיא ההספק. כך ניתן לדעת האם יש צורך למשוך "
       "הילוכים או שיש מספיק כוח כבר בנהיגה רגועה")}
{_block("מומנט מירבי", "61.2", '(קג"מ)', 'בישראל נתון המומנט נמדד בקילוגרם למטר. מנוע עם מומנט גבוה יותר ובסל"ד נמוך יותר יהיה הרבה יותר גמיש וחזק '
       'ביום יום')}
{_block('סל"ד למומנט מירבי', "1370", "(סיבובים/לדקה)", 'באיזו מהירות צריך להסתובב המנוע (בעירה פנימית) כדי לייצר את שיא המומנט, כך ניתן לדעת כמה "גמיש" המנוע. ככל '
       'שיש יותר מומנט ובסיבובים נמוכים יותר, כך ניתן "לרוץ" על המומנט ולא צריך לאמץ את המנוע')}
{_block('0 ל-100 קמ"ש', "5.7", "(שניות)")}{_block("מהירות מירבית", "240", '(קמ"ש)')}
{_block("תיבת הילוכים", "אוטומטית פלנטרית (רגילה)", "", "שתי תצורות מוכרות הינן תיבה ידנית ותיבה אוטומטית. תיבה אוטומטית מגיעה לרוב באחת משלוש התצורות: פלנטרית "
       "(תיבה אוטומטית רגילה); תיבה רובוטית, משמע תיבה הכוללת לרוב שני מצמדים כמו תיבה ידנית רק עם פיקוד חשמלי; "
       "תיבה רציפה ולה יחס העברה משתנה")}
{_block("מספר הילוכים", "8")}</ul></section>
<section class="compare"><h3>השוואה לרכבים מתחרים</h3>
<p>השוואה בין אודי Q8</p><p>{Q8_VERSION}</p><p>ל - ג'נסיס GV80 קופה</p>
<p>השוואה בין אודי Q8</p><p>{Q8_VERSION}</p><p>ל - מרצדס GLE קופה</p>
<p>השוואה בין אודי Q8</p><p>{Q8_VERSION}</p><p>ל - ב.מ.וו X6</p></section>
</main><footer><p>מגזין אוטו - בוחנים מכוניות משנת 1986</p></footer></body></html>"""


# --- P2: the cartube model page with four version links, two version pages, a two-version page -------------------------

Q3_MODEL_URL = "https://www.cartube.co.il/מחירון-רכב-חדש/10-אודי/49-אודי-q3"
Q3_VERSIONS = {  # url -> (version, power hp)
    Q3_MODEL_URL + "/325-1-5-35tfsi-2024": ("1.5 35TFSI", "150"),
    Q3_CARTUBE_URL: ("2.0 40TFSI 4X4", "190"),
    Q3_MODEL_URL + "/328-2-0-45tfsi-4x4-2024": ("2.0 45TFSI 4X4", "245"),
    Q3_MODEL_URL + "/330-2-0-40tfsi-4x4-sportback-2024": ("SPORTBACK 2.0 40TFSI 4X4", "190"),
}


def q3_model_html() -> str:
    links = "".join(f'<li><a href="{url}">אודי Q3 {version}</a><span>{power} כ״ס</span></li>'
                    for url, (version, power) in Q3_VERSIONS.items())
    return f"""<html><head><title>cartube - מחירון רכב | אודי Q3</title></head><body>{NAV}<main>
<h1>אודי Q3 - מחירון</h1><ul class="versions">{links}</ul>
<p><a href="https://www.cartube.co.il/חדשות-רכב/אודי-q3-חדש">חדשות</a></p></main></body></html>"""


def q3_two_versions_html() -> str:
    return f"""<html><head><title>cartube - אודי Q3 2.0 40TFSI / 45TFSI 4X4 2024</title></head><body>{NAV}<main>
<h1>אודי Q3 2.0 40TFSI 4X4 / 2.0 45TFSI 4X4</h1>
{_rows([("סוג מרכב", "קרוסאובר"), ("נפח מנוע", "1,984 סמ״ק"), ("הנעה", "4X4"), ("הספק", "190 כ״ס / 245 כ״ס")])}
</main></body></html>"""


# --- P4: the auto.co.il Q8 model page (d_224c1eca726a2023): several versions, "in all of them an 8-speed automatic" ------

Q8_MODEL_URL = "https://www.auto.co.il/cars/audi/q8/"
Q8_MODEL_QUOTE = "הנתונים והביצועים במספר גרסאות שופרו, ובכולן תיבה אוטומטית עם 8 הילוכים, הנעה כפולה וקפיצי אוויר."


def q8_model_html() -> str:
    return f"""<html><head><title>אודי Q8 - מחירון, חוות דעת ומידע טכני | אוטו</title></head><body>{NAV}<main>
<h1>אודי Q8 2026</h1><p>קרוסאובר יוקרה בגודל מלא</p>
<p>אודי Q8 עבר מתיחת פנים. {Q8_MODEL_QUOTE}</p>
<h2>גרסאות ומחירים</h2><ul>
<li>אודי Q8 50 TDI, 3.0 ל' טורבו דיזל, 286 כ"ס</li>
<li>אודי Q8 55 TFSIe, 3.0 ל' טורבו, פלאג אין הייבריד, 394 כ"ס</li>
<li>אודי Q8 60 TFSIe, 3.0 ל' טורבו, פלאג אין הייבריד, 490 כ"ס</li></ul>
</main></body></html>"""
