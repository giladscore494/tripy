"""SYNTHETIC XPeng G6 importer documents for PR #42 (DVM on real importer pages). NOT production artifacts.

Written to reproduce the structure of the official IL page https://heyxpeng.co.il/g6/ (doc d_fce64d9591bb7e72 of
production run 20261003T221935Z, record 101122) and of the importer brochure Xpeng-G6-25-Brochure.pdf, using the
excerpts quoted in the PR #42 brief:

    page      the regulatory safety table (קוד דגם | תיאור דגם), the consumption / range table, a "Black Edition"
              teaser, a charging paragraph ("הספק מרבי של 451 קילוואט", which is NOT motor power), and two tabbed
              spec sections: a heading ("מפרט New G6" / "מפרט G6"), N short tab labels ("Core RWD", "Core Performance
              AWD"), then N repeated label / value groups in the same order
    brochure  the pre-facelift G6 spec matrix whose Hebrew text is visually ordered (reversed) as pdfplumber reads it:
              "ינכט טרפמ" is "מפרט טכני", ")ס"כ( יברמ קפסה" is "הספק מרבי (כ"ס)" with 258 / 286 / 476

    python tests/fixtures/pr42_g6_pages.py <dest>     # writes <dest>/runs/SYNTH-pr42/101122 and <dest>/runs/_cache
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
for path in (ROOT, ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

PAYLOAD = {"identity": {"manufacturer": "אקספנג", "commercial_name": "G6", "year": 2026, "trim": "MAX",
                        "model_code": "NSGHA", "segment": "private", "government_record_id": "101122",
                        "government_codes": {"tozeret_cd": 1547, "degem_cd": 31, "sug_degem": "P"}},
           "engine_drivetrain": {"engine_cc": 0, "power_hp": 486, "propulsion_normalized": "battery_electric",
                                 "drivetrain_normalized": "awd"},
           "structure": {"body_normalized": "suv"}}

PAGE_URL = "https://heyxpeng.co.il/g6/"

# (price, drive, acceleration, range, DC kW, screen, length, curb weight) of each spec group, in tab order
NEW_G6 = (("Core RWD", "החל מ-204,990 ₪", "אחורית", "6.7", "535", "451", "15.6", "4,758", "2,190"),
          ("Core Performance AWD", "החל מ-239,990 ש״ח", "כפולה", "4.1", "510", "451", "15.6", "4,758", "2,295"))
OLD_G6 = (("RWD Standard Range", "החל מ-201,990 ₪", "אחורית", "6.9", "435", "215", "14.96", "4,753", "2,100"),
          ("RWD Long Range", "החל מ-227,990 ש״ח", "אחורית", "6.7", "570", "280", "14.96", "4,753", "2,100"),
          ("AWD Performance", "החל מ-256,990 ש״ח", "כפולה", "4.1", "550", "280", "14.96", "4,753", "2,195"))


def _group(price, drive, accel, rng, dc, screen, length, weight) -> str:
    rows = (("מחיר", price), ("מספר מושבים", "5"), ("הנעה", drive), ('תאוצה 0-100 קמ"ש', f"{accel} שניות"),
            ("טווח נסיעה חשמלית", f'{rng} ק"מ'), ("הספק טעינה מהירה DC", f"{dc} קילוואט"),
            ("מולטימדיה", f"מסך מרכזי {screen} אינץ'"),
            ("נפח תא המטען", "571 ליטר / 1,374 ליטר (עם מושב אחורי מקופל)"),
            ("אורך x רוחב x גובה", f"{length} / 1,920 / 1,650 מ״מ"), ("משקל עצמי", f"{weight} ק״ג"))
    cells = "".join(f'<div class="spec-row"><div class="k">{k}</div><div class="v">{v}</div></div>' for k, v in rows)
    return (f'<div class="panel"><div class="spec-list">{cells}</div><p>XPENG בהתאמה אישית</p>'
            '<a href="/wp-content/uploads/brochures/Xpeng-G6-Brochure.pdf">להורדת המפרט</a>'
            '<p>* מחיר הרכב כולל מע"מ ואגרת רישוי</p></div>')


def _tabs(heading: str, groups: tuple) -> str:
    labels = "".join(f'<button class="tab">{g[0]}</button>' for g in groups)
    return (f'<section class="specs"><h2>{heading}</h2><div class="tabs">{labels}</div>'
            + "".join(_group(*g[1:]) for g in groups) + "</section>")


# F5 verification data: the government catalog rows of XPeng G6 2026 (public.catalog_variants_current, read-only query
# of 2026-10-04: degem_cd -> (upstream_record_id, ramat_gimur)). Every code the importer page lists equals the degem_cd
# of the government record whose trim its description names; record 101136 (CORE, degem_cd 38) is also in the Level
# 1.5 benchmark snapshot (data/benchmark_v1_level15_snapshot.json). 30 / 31 / 32 / 33 are not on the page.
GOV_CODES_2026 = {24: ("101106", "PERF TB"), 26: ("101111", "STAND RANGE"), 27: ("101114", "LONG RANGE"),
                  30: ("101120", "PRO PLUS"), 31: ("101122", "MAX"), 32: ("101124", "BLACKEDITION"),
                  33: ("101126", "PRO PLUS 20"), 34: ("101128", "CORE PLUS"), 35: ("101130", "CORE PERF"),
                  36: ("101132", "BLACKEDITION"), 37: ("101134", "CORE PLUS 20"), 38: ("101136", "CORE")}

CODES = (("38", "G6 RWD Core"), ("34", "G6 RWD Core+‎"), ("37", 'G6 RWD Core+ 20" Wheels'),
         ("35", "G6 AWD Core Performance"), ("36", "G6 AWD Core Performance Black Edition"),
         ("26", "G6 RWD Standard Range/TB"), ("27", "G6 RWD Long Range/TB"), ("24", "G6 AWD Performance TB"))
CONSUMPTION = (("G6 RWD Core/Core+‎", "175", "535"), ('G6 RWD Core+ 20" Wheel', "179", "525"),
               ("G6 AWD Core Performance/Black Edition", "184", "510"), ("G6 RWD Standard Range/TB", "175", "435"),
               ("G6 RWD Long Range/TB", "175", "570"), ("G6 AWD Performance TB", "179", "550"))


def page_html(codes: tuple = CODES, new_g6: tuple = NEW_G6, old_g6: tuple = OLD_G6,
              new_labels: tuple | None = None) -> str:
    """The synthetic importer page. `new_labels` overrides the tab labels of the "מפרט New G6" section (a label count
    that differs from its group count must not be applied)."""
    code_rows = "".join(f"<tr><td>{c}</td><td>{d}</td><td>7</td></tr>" for c, d in codes)
    use_rows = "".join(f"<tr><td>{m}</td><td>{w}</td><td>{r}</td><td>1</td></tr>" for m, w, r in CONSUMPTION)
    new_section = _tabs("מפרט New G6", new_g6)
    if new_labels is not None:
        tabs = "".join(f'<button class="tab">{label}</button>' for label in new_labels)
        new_section = ('<section class="specs"><h2>מפרט New G6</h2><div class="tabs">' + tabs + "</div>"
                       + "".join(_group(*g[1:]) for g in new_g6) + "</section>")
    return f"""<html><head><title>Xpeng G6 - רכב קרוסאובר חשמלי מתקדם | Xpeng Israel</title></head><body>
<header><nav><ul><li><a href="/g6/">G6</a></li><li><a href="/p7/">P7+</a></li><li><a href="/g9/">G9</a></li>
<li><a href="/x9/">X9</a></li><li><a href="/pricing/">מחירון דגמים</a></li></ul></nav></header>
<main>
<h1>NEW XPENG G6</h1>
<div class="hero"><p>החל מ- ₪ 204,990</p><p>12 דקות זמן טעינה 80%-10%</p><p>6.7 שניות תאוצה 0-100 קמ״ש</p>
<p>*הנתונים מתייחסים לדגם G6 Core RWD</p></div>
<h2>נתוני צריכת חשמל, זיהום אוויר ואבזור בטיחותי</h2>
<h3>אבזור בטיחותי</h3>
<table><tr><th>קוד דגם</th><th>תיאור דגם</th><th>רמת אבזור בטיחותי</th></tr>{code_rows}</table>
<h3>צריכת חשמל וזיהום אוויר</h3>
<table><tr><th>דגם</th><th>צריכת חשמל (וואט שעה/ק"מ)</th><th>טווח נסיעה חשמלית (ק"מ)</th><th>דרגת זיהום אוויר</th>
</tr>{use_rows}</table>
<p>*עפ״י נתוני יצרן. לפי נתוני תקן WLTP</p>
<h2>G6 Black Edition</h2>
<p>מראות צד, מסגרת חלונות וחישוקי גלגלים בגימור שחור יוצרים מראה פרימיום.</p>
<p>* זמין באופציה בתוספת תשלום בגרסת Core Performance AWD בלבד.</p>
<h2>סוללת 5C מהדור הבא עם טעינה מהירה במיוחד</h2>
<p>הודות לארכיטקטורת 800V מתקדמת וסוללת 5C, ניתן להשיג טעינה מהירה בהספק מרבי של 451 קילוואט, ולהטעין מ־10% ל־80%
תוך 12 דקות בלבד.</p>
{new_section}
{_tabs("מפרט G6", old_g6)}
<h2>דגמים נוספים</h2><p>P7+ סאלון ספורטיבית חשמלית</p><p>G9 SUV חשמלית חכמה</p>
</main>
<footer><p>זכויות יוצרים XPENG@</p></footer>
</body></html>"""


BROCHURE_URL = "https://heyxpeng.co.il/wp-content/uploads/brochures/Xpeng-G6-25-Brochure.pdf"
# the brochure's spec matrix as pdfplumber reads it: every Hebrew run in visual (reversed) order; one tuple per row,
# its cells left to right (the label sits in the right-most cell of a right-to-left table)
BROCHURE_ROWS = (
    ("AWD", "RWD", "RWD", "G6 ינכט טרפמ"),
    ("Performance", "Long Range", "Standard Range", ""),
    ("476", "286", "258", ')ס"כ( יברמ קפסה'),
    ("4.1", "6.7", "6.9", ')תוינש( ש"מק 100-0 הצואת'),
    ("91.4", "87.5", "67.8", ")kWh( וטורב הללוסה תלוביק"),
    ("550", "570", "435", ")km( תילמשח העיסנ חווט"),
    ("280", "280", "215", ")kW( הניעט קפסה DC"),
    ("2,195", "2,100", "2,100", ')ג"ק( ימצע לקשמ'),
)


def brochure_pdf() -> bytes:
    """The brochure page as a real PDF (Hebrew glyphs drawn in visual order, as the importer's PDF draws them)."""
    from fixtures.mini_pdf import make_pdf, spec_matrix

    return make_pdf([spec_matrix(list(BROCHURE_ROWS), column=110)])


def put_pdf(cache, url: str, body: bytes) -> str:
    """Store a PDF the way fetch_pdf does (text = pdfplumber's text per page)."""
    import io

    import pdfplumber

    with pdfplumber.open(io.BytesIO(body)) as pdf:
        text = "\n\n".join(f"[page {i}]\n{page.extract_text() or ''}" for i, page in enumerate(pdf.pages, start=1))
    meta = {"status": 200, "final_url": url, "doc_type": "pdf", "content_type": "application/pdf", "pages": 1}
    return cache.put("pdf", url, body, meta, text)["document_id"]


# (field, value, quote, url) store_evidence requests of the synthetic run (quotes as admission accepts them on main)
REQUESTS = (
    # the "Core Performance AWD" group (awd|485, the target's technical variant)
    ("acceleration_0_100_s", 4.1, 'תאוצה 0-100 קמ"ש | 4.1 שניות', PAGE_URL),
    ("electric_range_km", 510, 'טווח נסיעה חשמלית | 510 ק"מ', PAGE_URL),
    ("dc_max_charging_power_kw", 451, "הספק טעינה מהירה DC | 451 קילוואט", PAGE_URL),
    ("screen_size_in", 15.6, "מולטימדיה | מסך מרכזי 15.6 אינץ'", PAGE_URL),
    ("cargo_volume_l", 571, "נפח תא המטען | 571 ליטר / 1,374 ליטר (עם מושב אחורי מקופל)", PAGE_URL),
    ("length_mm", 4758, "4,758 / 1,920 / 1,650 מ״מ", PAGE_URL),
    ("width_mm", 1920, "4,758 / 1,920 / 1,650 מ״מ", PAGE_URL),
    ("height_mm", 1650, "4,758 / 1,920 / 1,650 מ״מ", PAGE_URL),
    ("curb_weight_kg", 2295, "משקל עצמי | 2,295 ק״ג", PAGE_URL),
    # the "Core RWD" group: another technical variant (two_wheel_drive|295)
    ("curb_weight_kg", 2190, "משקל עצמי | 2,190 ק״ג", PAGE_URL),
    # the "AWD Performance" group of the pre-facelift section: awd, but {475, 485} stay ambiguous
    ("electric_range_km", 550, 'טווח נסיעה חשמלית | 550 ק"מ', PAGE_URL),
    # the pre-facelift brochure's AWD column (476 hp after F4: awd|475, another technical variant)
    ("curb_weight_kg", 2195, 'משקל עצמי )ק"ג( | 2,195 (AWD)', BROCHURE_URL),
    ("acceleration_0_100_s", 4.1, 'תאוצה 100-0 קמ"ש )שניות( | 4.1 (AWD)', BROCHURE_URL),
)
FIELDS = sorted({r[0] for r in REQUESTS})


def build(dest: Path | str) -> Path:
    """Write the synthetic run and its document cache under <dest>/runs; returns the run folder."""
    from fixtures.corolla_harvest import put
    from src.candidate_harvest import harvest_document
    from src.evidence_admission import AdmissionContext, admit
    from src.fields import resolve_requested_fields
    from src.storage.cache import DocumentCache

    dest = Path(dest)
    cache = DocumentCache(dest / "runs" / "_cache")
    specs = resolve_requested_fields(FIELDS, propulsion="battery_electric")
    adm = AdmissionContext.for_run(PAYLOAD, None, specs, "IL")
    docs = {PAGE_URL: put(cache, PAGE_URL, page_html(), "html"), BROCHURE_URL: put_pdf(cache, BROCHURE_URL,
                                                                                        brochure_pdf())}
    events = [{"kind": "run_started", "seq": 1, "target_market": "IL", "record_id": "101122", "vehicle_label": {},
               "requested_field_specs": [{"name": s["name"], "applicable": True} for s in specs]}]
    for url, doc in docs.items():
        cands, _ = harvest_document(cache, doc, specs)
        events.append({"kind": "candidates_harvested", "seq": len(events) + 1, "document_id": doc, "url": url,
                       "candidates": cands})
    for i, (field, value, quote, url) in enumerate(REQUESTS, start=1):
        decision = admit(adm, cache, {"field": field, "value": value, "quote": quote, "document_id": docs[url]},
                         list(docs.values()))
        if not decision["accepted"]:
            raise AssertionError((field, value, decision))
        events.append({"kind": "evidence", "seq": len(events) + 1,
                       "evidence": {**decision["record"], "evidence_id": f"e{i}"}})
    for path in (dest / "runs" / "_cache" / "documents").glob("*/derived_*"):
        path.unlink()                                   # the replay recomputes every derived extraction
    run_dir = dest / "runs" / "SYNTH-pr42" / "101122"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "input.json").write_text(json.dumps(PAYLOAD, ensure_ascii=False), "utf-8")
    (run_dir / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), "utf-8")
    return run_dir


if __name__ == "__main__":
    print(build(sys.argv[1]))
