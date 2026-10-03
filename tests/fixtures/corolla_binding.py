"""Server-side binding of every admissible candidate of every Corolla fixture document, for three targets (record 38626
1.8 Hybrid BUSINESS EDI, the same technical variant as PREMIUM, and the 2.0 Hybrid): the golden of PR #34 (binding-v2).
A result that was `exact_market_trim`, `exact` or `different` under binding-v1 must not change; a result that was
`unclear` may only rise, with a recorded `binding_basis`.

Run directly to (re)write the golden file (do this on the code the golden should describe):
    python tests/fixtures/corolla_binding.py --write-golden
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
for path in (ROOT, ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from fixtures import corolla_family as family  # noqa: E402
from fixtures import corolla_harvest as harvest  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "corolla_binding_golden.json"
TARGETS = ("A", "A2", "B")


def binding_results(cache) -> dict[str, dict]:
    """{json [target, document, field, value, unit, quote]: {binding_level, variant_match, binding_basis?}} of every
    candidate admit() accepts."""
    from src.adjudication import candidate_request, dry_run
    from src.candidate_harvest import harvest_document
    from src.evidence_admission import AdmissionContext
    from src.field_recovery import material_key
    from src.fields import load_schema

    specs = load_schema()
    docs = {f"{kind}:{url}": harvest.put(cache, url, body, kind) for url, body, kind in harvest.documents()}
    docs.update(harvest.structural_documents(cache))
    out: dict[str, dict] = {}
    for target in TARGETS:
        v = family.variant(target)
        adm = AdmissionContext.for_run(v["payload"], v["vehicle"], specs, "IL")
        for key, doc in sorted(docs.items()):
            cands, _ = harvest_document(cache, doc, specs)
            for cand in cands:
                decision = dry_run(adm, cache, candidate_request(cand["field"], {**cand, "document_id": doc}),
                                   list(docs.values()))
                if not decision.get("accepted"):
                    continue
                record = decision["record"]
                name = json.dumps([target, key, cand["field"], material_key(cand.get("value")), cand.get("unit"),
                                   cand.get("quote")], ensure_ascii=False)
                out[name] = {k: record.get(k) for k in ("binding_level", "variant_match", "binding_basis")
                             if record.get(k) is not None}
    return out


def compute() -> dict[str, dict]:
    from src.storage.cache import DocumentCache

    with tempfile.TemporaryDirectory() as tmp:
        return binding_results(DocumentCache(Path(tmp) / "cache"))


if __name__ == "__main__":
    results = compute()
    if "--write-golden" in sys.argv:
        GOLDEN.write_text(json.dumps(results, ensure_ascii=False, indent=1, sort_keys=True) + "\n", "utf-8")
    print(json.dumps({"results": len(results)}, indent=1))
