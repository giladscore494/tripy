"""PR #46 proof gate 1: unit-level replay of P1 on production run 20261005T184853Z, record 12949 (Audi A6 2018, 2.0 l,
252 hp). The document is tests/fixtures/pr46_a6_319.json: static.auto.co.il/media/rfhhvopt/319.pdf exactly as production
cached it (text + pdfplumber tables). Prints, per dimension fact, the binding the run recorded and the binding now.

    python scripts/pr46_proof_gate.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from fixtures import pr45_pages as F  # noqa: E402
from fixtures.corolla_harvest import put  # noqa: E402
from src.binding_replay import replay_fact  # noqa: E402
from src.evidence_admission import AdmissionContext  # noqa: E402
from src.fields import resolve_requested_fields  # noqa: E402
from src.storage.cache import DocumentCache  # noqa: E402

FACTS = [("length_mm", 4939, 'אורך (מ"מ) 4,939 4,939'), ("width_mm", 1886, 'רוחב (מ"מ) 1,886 1,886'),
         ("height_mm", 1411, 'גובה (מ"מ) 1,411 1,411')]


def main() -> int:
    fixture = json.loads((ROOT / "tests" / "fixtures" / "pr46_a6_319.json").read_text("utf-8"))
    cache = DocumentCache(Path(tempfile.mkdtemp()) / "cache")
    doc = put(cache, fixture["url"], fixture["text"], "pdf")
    cache.put_derived(doc, "tables_v3", fixture["tables"])
    adm = AdmissionContext.for_run(F.A6_PAYLOAD, None, resolve_requested_fields(None, propulsion="conventional"), "IL")
    failed = 0
    print(f"run {fixture['run_id']} record {fixture['record_id']} document {fixture['url']}")
    print("| field | value | recorded | now | gap | document powers | target listed |")
    print("|---|---|---|---|---|---|---|")
    for field, value, quote in FACTS:
        pool = {doc: [{"field": field, "value": value, "quote": quote, "extraction_method": "alias_proximity",
                       "document_id": doc}]}
        row = replay_fact(adm, cache, field=field, value=value, quote=quote, document_id=doc,
                          source_url=fixture["url"], candidates=pool)
        versions = row.get("document_versions_now") or {}
        ok = row["variant_match_now"] == "exact"
        failed += ok
        print(f"| {field} | {value} | exact_technical_variant (ok) | {row['binding_level_now']} "
              f"({'ok' if ok else 'not ok'}) | {', '.join(row.get('binding_gap_now') or []) or '-'} | "
              f"{versions.get('powers')} | {json.dumps(versions.get('target_listed'))} |")
    print("PASS: length / width / height are not ok" if not failed else f"FAIL: {failed} fact(s) still ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
