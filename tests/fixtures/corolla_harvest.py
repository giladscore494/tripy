"""Every Corolla fixture document (record 38626 and its family) as the fetch tools would store it, and the candidate
coverage of the deterministic harvest over them. Used by the golden test of PR #31 (no candidate the harvester
emitted before Parts A-C may disappear) and by the before / after coverage table.

Run directly to (re)write the golden file:  python tests/fixtures/corolla_harvest.py --write-golden
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
for path in (ROOT, ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from fixtures import corolla_family as family  # noqa: E402
from fixtures import corolla_orchestration as orch  # noqa: E402
from fixtures import corolla_tail as tail  # noqa: E402
from fixtures import corolla_touring as touring  # noqa: E402

from src.tools.extract import html_title, visible_text  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "corolla_harvest_golden.json"


def documents() -> list[tuple[str, str, str]]:
    """(url, body, kind: html | pdf | text) of every distinct Corolla fixture document."""
    out: dict[str, tuple[str, str, str]] = {}
    for url, body, kind in ((touring.CARTUBE, touring.CARTUBE_TEXT, "text"),
                            (touring.TWO_LITRE, touring.TWO_LITRE_TEXT, "text"),
                            (touring.COMPARE, touring.COMPARE_HTML, "html"),
                            (touring.TOYOTA_UK, touring.TOYOTA_UK_TEXT, "pdf"),
                            (touring.LAUNCH, touring.LAUNCH_HTML, "html"),
                            (touring.GENERIC, touring.GENERIC_TEXT, "text")):
        out[f"touring:{url}"] = (url, body, kind)
    routes = [("tail", tail.ROUTES), ("orchestration", orch.ROUTES), ("family", family.ROUTES)]
    for name, table in routes:
        for url, (body, ctype) in table.items():
            kind = "html" if "html" in ctype else ("pdf" if "pdf" in ctype or url.endswith(".pdf") else "text")
            out.setdefault(f"{name}:{url}", (url, body, kind))
    return list(out.values())


def put(cache, url: str, body: str, kind: str) -> str:
    if kind == "html":
        meta = {"status": 200, "final_url": url, "doc_type": "html", "content_type": "text/html",
                "title": html_title(body)}
        return cache.put("fetch", url, body.encode("utf-8"), meta, visible_text(body))["document_id"]
    if kind == "pdf":
        meta = {"status": 200, "final_url": url, "doc_type": "pdf", "content_type": "application/pdf", "pages": 1}
        return cache.put("pdf", url, b"%PDF-1.4 fixture", meta, body)["document_id"]
    meta = {"status": 200, "final_url": url, "doc_type": "text", "content_type": "text/plain"}
    return cache.put("fetch", url, body.encode("utf-8"), meta, body)["document_id"]


# PR #31: the layouts the 38626 runs could not read (test data written to reproduce them, not copies of real pages):
# a Hebrew importer spec page whose labels and values sit in sibling elements / cards / a definition list, and an
# importer brochure PDF whose spec matrix has no ruling lines.
IMPORTER_SPEC = "https://www.toyota.co.il/new-cars/corolla/specifications"
IMPORTER_SPEC_HTML = """<html><head><title>טויוטה קורולה 2024 - מפרט טכני</title></head><body>
<header><nav><ul><li><span>דגמים</span><span>קורולה</span></li></ul></nav></header>
<h1>טויוטה קורולה 2024 1.8 היברידי</h1>
<h2>מידות ומשקלים</h2>
<dl class="dims"><dt>אורך</dt><dd>4,630</dd><dt>רוחב</dt><dd>1,780</dd><dt>גובה</dt><dd>1,435</dd></dl>
<h3>רמת גימור BUSINESS EDI</h3>
<ul class="spec-list">
<li class="spec"><span class="k">בסיס גלגלים (מ"מ)</span><span class="v">2,700</span></li>
<li class="spec"><span class="k">נפח מיכל דלק (ליטר)</span><span class="v">43</span></li>
<li class="spec"><span class="k">משקל עצמי (ק"ג)</span><span class="v">1,385</span></li>
<li class="spec"><span class="k">נפח תא מטען (ליטר)</span><span class="v">471</span></li>
</ul>
<div class="cards">
<div class="card-row"><div class="label">אחריות</div><div class="value">3 שנים או 100,000 ק"מ</div></div>
<div class="card-row"><div class="label">מחיר</div><div class="value">179,990 ש"ח</div></div>
<div class="card-row"><div class="label">רמת גימור</div><div class="value">BUSINESS EDI</div></div>
<div class="card-row"><div class="label">תיבת הילוכים</div><div class="value">e-CVT</div></div>
</div>
<footer><div><div>שירות לקוחות</div><div>*2800</div></div></footer>
</body></html>"""
BROCHURE = "https://www.toyota.co.il/media/corolla-2024-brochure.pdf"
BROCHURE_ROWS = [("Version", "1.8 Hybrid Business", "1.8 Hybrid Premium"), ("Ground clearance", "135 mm", "135 mm"),
                 ("Maximum torque", "142 Nm", "142 Nm"), ("Top speed", "180 km/h", "180 km/h"),
                 ("Tyres", "205/55 R16", "225/45 R17")]


def structural_documents(cache) -> dict[str, str]:
    """Store the PR #31 structural fixtures; {key: document_id}."""
    from fixtures.mini_pdf import make_pdf, spec_matrix

    out = {f"html:{IMPORTER_SPEC}": put(cache, IMPORTER_SPEC, IMPORTER_SPEC_HTML, "html")}
    body = make_pdf([spec_matrix(BROCHURE_ROWS)])
    text = "[page 1]\n" + "\n".join("   ".join(r) for r in BROCHURE_ROWS)
    meta = {"status": 200, "final_url": BROCHURE, "doc_type": "pdf", "content_type": "application/pdf", "pages": 1}
    out[f"pdf:{BROCHURE}"] = cache.put("pdf", BROCHURE, body, meta, text)["document_id"]
    return out


def harvest_before_after(cache, doc_ids: dict[str, str]) -> tuple[dict, dict]:
    """({key: candidates} with the pre-#31 harvester path, {key: candidates} with Parts A-C). The "before" path is the
    same segment harvest without the PR #31 inputs: no HTML source (no structural pairs), the default ruling-line PDF
    tables only, and no unit-anchor addition."""
    from src.candidate_harvest import dictionary_for, harvest_document, harvest_segments, document_segments
    from src.fields import load_schema
    from src.tools.extract import _html_tables, _pdf_tables, document_structured

    specs = load_schema()
    d = dictionary_for(specs)
    before, after = {}, {}
    for key, doc in doc_ids.items():
        meta = cache.get(doc) or {}
        is_html = meta.get("doc_type") == "html"
        html = cache.read_body(doc).decode("utf-8") if is_html else None
        try:
            tables = _html_tables(html) if is_html else (_pdf_tables(cache.read_body(doc))
                                                       if meta.get("doc_type") == "pdf" else [])
        except Exception:  # noqa: BLE001 - a fixture "PDF" body that is not a real PDF
            tables = []
        structured = document_structured(cache, doc, html) if is_html else None
        before[key] = harvest_segments(document_segments(cache.read_text(doc), tables, structured,
                                                         is_pdf=meta.get("doc_type") == "pdf", dictionary=d), d)
        after[key] = harvest_document(cache, doc, specs)[0]
    return before, after


def coverage_table(cache) -> dict:
    """Fields with >= 1 candidate before vs after Parts A-C: the existing Corolla fixtures, the structural fixtures,
    and both together (all 45 schema fields; 37 apply to the 38626 hybrid)."""
    from src.fields import resolve_requested_fields

    applicable = {s["name"] for s in resolve_requested_fields(None, propulsion="hybrid") if s.get("applicable", True)}
    existing = {f"{kind}:{url}": put(cache, url, body, kind) for url, body, kind in documents()}
    structural = structural_documents(cache)
    rows = {}
    for name, ids in (("existing Corolla fixtures", existing), ("structural fixtures (PR #31)", structural),
                      ("all", {**existing, **structural})):
        before, after = harvest_before_after(cache, ids)
        b = {c["field"] for v in before.values() for c in v} & applicable
        a = {c["field"] for v in after.values() for c in v} & applicable
        rows[name] = {"documents": len(ids), "before": len(b), "after": len(a), "gained": sorted(a - b),
                      "lost": sorted(b - a)}
    return {"applicable_fields": len(applicable), "rows": rows}


def harvest_all(cache) -> dict[str, list[dict]]:
    """{document key (kind:url): candidates} for every fixture document, full schema (all fields)."""
    from src.candidate_harvest import harvest_document
    from src.fields import load_schema

    specs = load_schema()
    out = {}
    for url, body, kind in documents():
        doc = put(cache, url, body, kind)
        cands, _ = harvest_document(cache, doc, specs)
        out[f"{kind}:{url}"] = cands
    return out


def candidate_keys(harvest: dict[str, list[dict]]) -> list[str]:
    """Stable identities of harvested candidates: document, field, value, unit."""
    from src.field_recovery import material_key

    return sorted({json.dumps([doc, c.get("field"), material_key(c.get("value")), c.get("unit")], ensure_ascii=False)
                   for doc, cands in harvest.items() for c in cands})


def fields_with_candidates(harvest: dict[str, list[dict]], fields: list[str] | None = None) -> list[str]:
    names = {c.get("field") for cands in harvest.values() for c in cands}
    return sorted(n for n in names if fields is None or n in fields)


if __name__ == "__main__":
    import tempfile

    from src.storage.cache import DocumentCache

    with tempfile.TemporaryDirectory() as tmp:
        harvest = harvest_all(DocumentCache(Path(tmp)))
    keys = candidate_keys(harvest)
    report = {"candidate_keys": keys, "fields_with_candidates": fields_with_candidates(harvest)}
    if "--table" in sys.argv:
        with tempfile.TemporaryDirectory() as tmp:
            print(json.dumps(coverage_table(DocumentCache(Path(tmp))), ensure_ascii=False, indent=1))
    if "--write-golden" in sys.argv:
        GOLDEN.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", "utf-8")
    print(json.dumps({"candidates": len(keys), "fields_with_candidates": len(report["fields_with_candidates"])}))
