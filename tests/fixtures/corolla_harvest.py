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
    if "--write-golden" in sys.argv:
        GOLDEN.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", "utf-8")
    print(json.dumps({"candidates": len(keys), "fields_with_candidates": len(report["fields_with_candidates"])}))
