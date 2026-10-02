"""Offline Toyota Corolla Touring Sports fixture (benchmark vehicle #1, record 38626: 2024, BUSINESS EDI, 1.8 Hybrid,
Israel) shaped after the first real layered run. Document texts are test data written to reproduce the observed
failure modes (2.0 Hybrid cargo contamination, e-CVT gear count, label-only booleans, note provenance, split climate
control, launch price), not copies of the real pages."""

import json
from pathlib import Path

from src.db import build_level15_payload
from src.tools.extract import html_title, visible_text

SNAPSHOT = Path(__file__).resolve().parent.parent.parent / "data" / "benchmark_v1_level15_snapshot.json"
ROW = next(r for r in json.loads(SNAPSHOT.read_text("utf-8"))["rows"] if r["upstream_record_id"] == "38626")
PAYLOAD = build_level15_payload(ROW)
VEHICLE = {"manufacturer": "טויוטה", "model": "COROLLA", "year": 2024, "trim": "BUSINESS EDI",
           "model_code": "ZWE211L DWXNBW", "propulsion": "hybrid", "drivetrain": "4X2"}

CARTUBE = "https://www.cartube.co.il/toyota/corolla-touring-sports-2024-1-8-hybrid-business"
CARTUBE_TEXT = "\n".join([
    "טויוטה קורולה טורינג ספורט 2024 1.8 היברידי Business",
    "תיבת הילוכים: e-CVT",
    'גובה 146.0 ס"מ',
    'גובה 143.5 ס"מ',
    "מושבים חשמליים: אין",
    "חימום מושבים: אין",
    "חלון גג: אין",
    "בקרת אקלים: מפוצלת",
    "אחריות: שלוש שנים",
    'מומנט מנוע בנזין: 142 ניוטון-מטר; מומנט משולב: 185 ניוטון-מטר',
    'נפח תא מטען 581-588 ליטר',
    'צריכת דלק משולבת: 4.5 ליטר ל-100 ק"מ',
])
# A review of the 2.0 Hybrid: same model, body and year, different powertrain (bigger battery, smaller boot)
TWO_LITRE = "https://www.example.com/reviews/corolla-touring-sports-2-0-hybrid-2024"
TWO_LITRE_TEXT = ("Toyota Corolla Touring Sports 2.0 Hybrid 2024 review. The larger hybrid battery costs boot space: "
                  "Boot space: 581 litres.")
# A multi-variant comparison table on one page (1.8 AND 2.0 Hybrid columns)
COMPARE = "https://www.cartube.co.il/toyota/corolla-touring-sports-2024-compare"
COMPARE_HTML = """<html><head><title>טויוטה קורולה טורינג ספורט 2024 - השוואת גרסאות</title></head><body>
<h1>טויוטה קורולה טורינג ספורט 2024 היברידי</h1>
<table>
<tr><th>גרסה</th><th>1.8 Hybrid 140</th><th>2.0 Hybrid 196</th></tr>
<tr><td>נפח תא מטען</td><td>596 ליטר</td><td>581 ליטר</td></tr>
<tr><td>גובה</td><td>1,460 מ"מ</td><td>1,460 מ"מ</td></tr>
</table></body></html>"""
# Official manufacturer document of another market (UK)
TOYOTA_UK = "https://www.toyota.co.uk/corolla-touring-sports-specs.pdf"
TOYOTA_UK_TEXT = ("[page 1]\nToyota Corolla Touring Sports 2024 1.8 Hybrid 140\nFuel tank capacity 43 l\n"
                  "Unladen height 1,435 mm")
# A launch article with its own publication metadata (time-sensitive price)
LAUNCH = "https://www.carnews.co.il/toyota-corolla-touring-sports-2024-launch"
LAUNCH_HTML = """<html><head><title>קורולה טורינג ספורט 2024 הושקה בישראל</title>
<script type="application/ld+json">{"@type": "NewsArticle", "datePublished": "2024-01-10T08:00:00+02:00"}</script>
</head><body><p>טויוטה קורולה טורינג ספורט 2024 1.8 היברידי Business הושקה בישראל.</p>
<p>מחיר: 179,990 ש"ח (מחירון ינואר 2024).</p></body></html>"""
# A page that never names the model (cannot bind to the target at all)
GENERIC = "https://www.example.org/family-cars-boot-space"
GENERIC_TEXT = "Family estates compared. Boot space: 596 litres on the best of them."


def put_documents(cache) -> dict[str, str]:
    """Store every fixture document as the fetch tools would; return {url: document_id}."""
    out = {}
    for url, body, kind in ((CARTUBE, CARTUBE_TEXT, "text"), (TWO_LITRE, TWO_LITRE_TEXT, "text"),
                            (COMPARE, COMPARE_HTML, "html"), (TOYOTA_UK, TOYOTA_UK_TEXT, "pdf"),
                            (LAUNCH, LAUNCH_HTML, "html"), (GENERIC, GENERIC_TEXT, "text")):
        if kind == "html":
            meta = {"status": 200, "final_url": url, "doc_type": "html", "content_type": "text/html",
                    "title": html_title(body)}
            out[url] = cache.put("fetch", url, body.encode("utf-8"), meta, visible_text(body))["document_id"]
        elif kind == "pdf":
            meta = {"status": 200, "final_url": url, "doc_type": "pdf", "content_type": "application/pdf", "pages": 1,
                    "pdf_metadata": {"CreationDate": "D:20240301120000"}}
            out[url] = cache.put("pdf", url, b"%PDF-1.4 fixture", meta, body)["document_id"]
        else:
            meta = {"status": 200, "final_url": url, "doc_type": "text", "content_type": "text/plain"}
            out[url] = cache.put("fetch", url, body.encode("utf-8"), meta, body)["document_id"]
    return out
