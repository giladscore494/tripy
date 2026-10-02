"""Offline Cadillac LYRIQ-style fixture: ten documents of the kinds a real run downloads (Israeli importer spec
table, Israeli review, US manufacturer page, EU press page with JSON-LD, a reversed-Hebrew price-list PDF text,
news/forum pages). Values are illustrative test data, not facts about the vehicle."""

from src.tools.extract import html_title, visible_text

PAYLOAD = {"identity": {"manufacturer": "קאדילאק", "commercial_name": "LYRIQ", "year": 2025, "trim": "Luxury",
                        "model_code": "6MD26", "government_record_id": "85095"},
           "engine_drivetrain": {"propulsion_normalized": "battery_electric", "drivetrain_normalized": "awd",
                                 "power_hp": 528}}
VEHICLE = {"manufacturer": "קאדילאק", "model": "LYRIQ", "year": 2025, "trim": "Luxury", "propulsion": "battery_electric"}

IL_SPEC = "https://www.cadillac.co.il/lyriq/specs"
IL_REVIEW = "https://www.icar.co.il/lyriq-review"
US_PAGE = "https://www.cadillac.com/en-us/electric/lyriq"   # the path locale makes it a US page (server-side)
EU_PAGE = "https://www.cadillaceurope.com/lyriq"
IL_PRICE_PDF = "https://www.cadillac.co.il/lyriq/pricelist.pdf"

IL_SPEC_HTML = """<html><head><title>LYRIQ מפרט טכני</title></head><body><h1>קאדילאק LYRIQ 2025</h1>
<table>
<tr><th>מפרט</th><th>Luxury</th></tr>
<tr><td>מומנט מרבי</td><td>610 נ"מ</td></tr>
<tr><td>תאוצה 0-100 קמ"ש</td><td>5.3 שניות</td></tr>
<tr><td>מהירות מרבית</td><td>190 קמ"ש</td></tr>
<tr><td>קיבולת סוללה</td><td>102 קוט"ש</td></tr>
<tr><td>טווח נסיעה (WLTP)</td><td>530 ק"מ</td></tr>
<tr><td>צריכת חשמל</td><td>22.5 קוט"ש/100 ק"מ</td></tr>
<tr><td>הספק טעינת DC</td><td>190 קילוואט</td></tr>
<tr><td>הספק טעינת AC</td><td>11 קילוואט</td></tr>
<tr><td>אורך</td><td>4,996 מ"מ</td></tr>
<tr><td>רוחב</td><td>1,978 מ"מ</td></tr>
<tr><td>גובה</td><td>1,623 מ"מ</td></tr>
<tr><td>בסיס גלגלים</td><td>3,094 מ"מ</td></tr>
<tr><td>משקל עצמי</td><td>2,595 ק"ג</td></tr>
<tr><td>מרווח גחון</td><td>155 מ"מ</td></tr>
<tr><td>נפח תא מטען</td><td>588 ליטר</td></tr>
<tr><td>תיבת הילוכים</td><td>חד-הילוכית</td></tr>
<tr><td>מסך מרכזי</td><td>33 אינץ'</td></tr>
<tr><td>Apple CarPlay</td><td>✓</td></tr>
<tr><td>Android Auto</td><td>✓</td></tr>
<tr><td>חימום מושבים</td><td>●</td></tr>
<tr><td>אוורור מושבים</td><td>●</td></tr>
<tr><td>גג פנורמי</td><td>✓</td></tr>
<tr><td>צמיגים קדמיים</td><td>265/50 R22</td></tr>
<tr><td>צמיגים אחוריים</td><td>265/50 R22</td></tr>
<tr><td>אחריות לרכב</td><td>5 שנים או 100,000 ק"מ</td></tr>
<tr><td>אחריות לסוללה</td><td>8 שנים או 160,000 ק"מ</td></tr>
</table></body></html>"""

IL_REVIEW_TEXT = ("קאדילאק LYRIQ במבחן דרכים. המומנט המרבי עומד על 610 נ\"מ והתאוצה מ-0 ל-100 קמ\"ש לוקחת 5.3 שניות.\n"
                  "טעינה מהירה DC מ-10% עד 80% ב-33 דקות.\n"
                  "טעינה ביתית AC בהספק 11 קילוואט נמשכת כ-11 שעות.\n"
                  "בתא הנוסעים: אבזור נוחות עשיר, בקרת אקלים דו-אזורית ומושבים חשמליים עם זיכרון.\n"
                  "מחיר: 399,990 ש\"ח.")

US_TEXT = ("2025 Cadillac LYRIQ Luxury. Peak torque 650 Nm in the AWD model.\n"
           "Range: 502 km (EPA).\n"
           "33-inch diagonal LED display with wireless Apple CarPlay and Android Auto as standard equipment.\n"
           "22-inch alloy wheels. Tires: 275/40 R22 front and rear.\n"
           "Basic warranty: 4 years / 80,000 km. Battery warranty: 8 years / 160,000 km.")

EU_HTML = """<html><head><title>LYRIQ Europe</title><script type="application/ld+json">
{"@type": "Car", "name": "LYRIQ", "wheelbase": "3094 mm", "cargoVolume": "588 l",
 "accelerationTime": "6.0 s", "vehicleEngine": {"torque": "610 Nm"}}</script></head>
<body><p>Electric luxury SUV with a panoramic glass roof.</p></body></html>"""

# pdfplumber-style visual-order Hebrew lines (each Hebrew word reversed, word order reversed)
PRICE_PDF_TEXT = "[page 1]\n" + "\n".join([
    "ןוריחמ 2025 LYRIQ",
    'ח"ש 399,990 ריחמ',
    'ח"ש 2,595 יושיר תרגא',
])

NEWS_TEXT = "Cadillac announces LYRIQ updates for 2025. Deliveries begin in spring."
FORUM_TEXT = "Has anyone tried the LYRIQ? The 0-60 mph time feels quicker than 4.9 seconds."
GADGET_TEXT = "LYRIQ multimedia review: the 33-inch screen runs Google built-in. Head-up display optional."
LEASE_TEXT = "LYRIQ leasing offer: monthly payment ₪3,990 per month for 36 months."
IL_NEWS_TEXT = "יבואן קאדילאק מציג את LYRIQ בישראל. אחריות לרכב: 5 שנים או 100,000 ק\"מ."

DOCUMENTS = [
    (IL_SPEC, IL_SPEC_HTML, "html"),
    (IL_REVIEW, IL_REVIEW_TEXT, "text"),
    (US_PAGE, US_TEXT, "text"),
    (EU_PAGE, EU_HTML, "html"),
    (IL_PRICE_PDF, PRICE_PDF_TEXT, "pdf"),
    ("https://news.example.com/lyriq-2025", NEWS_TEXT, "text"),
    ("https://forum.example.com/t/lyriq", FORUM_TEXT, "text"),
    ("https://gadgets.example.com/lyriq-screen", GADGET_TEXT, "text"),
    ("https://lease.example.co.il/lyriq", LEASE_TEXT, "text"),
    ("https://www.carnews.co.il/lyriq-israel", IL_NEWS_TEXT, "text"),
]


def put_documents(cache) -> list[str]:
    """Store the ten documents in a DocumentCache exactly as the fetch tools would; return document ids."""
    ids = []
    for url, body, kind in DOCUMENTS:
        if kind == "html":
            meta = {"status": 200, "final_url": url, "doc_type": "html", "content_type": "text/html",
                    "title": html_title(body)}
            record = cache.put("fetch", url, body.encode("utf-8"), meta, visible_text(body))
        elif kind == "pdf":
            meta = {"status": 200, "final_url": url, "doc_type": "pdf", "content_type": "application/pdf", "pages": 1}
            record = cache.put("pdf", url, b"%PDF-1.4 fixture", meta, body)
        else:
            meta = {"status": 200, "final_url": url, "doc_type": "text", "content_type": "text/plain"}
            record = cache.put("fetch", url, body.encode("utf-8"), meta, body)
        ids.append(record["document_id"])
    return ids
