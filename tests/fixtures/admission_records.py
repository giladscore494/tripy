"""Every admission decision over the admission fixtures (the Corolla documents for targets A / A2 / B and the XPeng G6
spec pages for the G6 MAX): the golden of PR #35's refactor safety test. The binding-layer construction moved out of
admit() into a shared pure helper (evidence_admission.binding_layers) that Binding Replay also uses; admit() must still
accept / reject exactly the same requests and produce the same records (`binding_version` aside).

The golden was written on `main` at ba25738 (PR #34 merged, binding-v2), BEFORE the extraction. Do not rewrite it.

    python tests/fixtures/admission_records.py --write-golden
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

GOLDEN = Path(__file__).resolve().parent / "admission_records_golden.json"
VOLATILE = ("observed_at",)          # the cache's fetch time
XPENG = {"identity": {"manufacturer": "אקספנג", "commercial_name": "G6", "year": 2026, "trim": "MAX"},
         "engine_drivetrain": {"engine_cc": 0, "power_hp": 486, "propulsion_normalized": "battery_electric",
                               "drivetrain_normalized": "awd"},
         "structure": {"body_normalized": "suv"}}
XPENG_SPEC = "https://www.xpeng.co.il/g6/specifications"
XPENG_PRICES = "https://www.xpeng.co.il/g6/prices"
XPENG_COLUMNS = [("Standard", 190, 258, "RWD", "1,995"), ("Long Range", 218, 296, "RWD", "2,040"),
                 ("Performance", 316, 430, "AWD", "2,135"), ("MAX", 357, 486, "AWD", "2,180")]


def xpeng_table() -> str:
    rows = [["גרסה"] + [c[0] for c in XPENG_COLUMNS], ["הספק (kW)"] + [f"{c[1]} kW" for c in XPENG_COLUMNS],
            ['הספק (כ"ס)'] + [f'{c[2]} כ"ס' for c in XPENG_COLUMNS], ["הנעה"] + [c[3] for c in XPENG_COLUMNS],
            ['משקל עצמי (ק"ג)'] + [c[4] for c in XPENG_COLUMNS]]
    body = "".join("<tr>" + "".join(f"<td>{x}</td>" for x in r) + "</tr>" for r in rows)
    return f"""<html><head><title>XPeng G6 2026 - מפרט טכני</title></head><body><h1>XPeng G6</h1>
<p>רכב חשמלי SUV</p><table>{body}</table></body></html>"""


XPENG_PRICES_HTML = """<html><head><title>XPeng G6 2026 - מחירון</title></head><body><h1>XPeng G6 MAX</h1>
<p>רכב חשמלי, SUV, הנעה כפולה AWD</p><p>הספק מרבי: 486 כ"ס</p><p>צמיגים: 255/45 R20</p>
<p>מחיר: 259,900 ש"ח</p><p>עודכן: 03/2025</p></body></html>"""


def targets() -> dict[str, tuple[dict, dict | None]]:
    out = {}
    for name in ("A", "A2", "B"):
        v = family.variant(name)
        out[name] = (v["payload"], v["vehicle"])
    out["G6MAX"] = (XPENG, None)
    return out


def store_documents(cache) -> dict[str, str]:
    docs = {f"{kind}:{url}": harvest.put(cache, url, body, kind) for url, body, kind in harvest.documents()}
    docs.update(harvest.structural_documents(cache))
    docs[f"html:{XPENG_SPEC}"] = harvest.put(cache, XPENG_SPEC, xpeng_table(), "html")
    docs[f"html:{XPENG_PRICES}"] = harvest.put(cache, XPENG_PRICES, XPENG_PRICES_HTML, "html")
    return docs


def decisions(cache) -> dict[str, dict]:
    """{json [target, document, field, value, unit, quote]: {accepted, record | reasons}} of every harvested
    candidate's own store request (accepted AND rejected)."""
    from src.adjudication import candidate_request
    from src.candidate_harvest import harvest_document
    from src.evidence_admission import AdmissionContext, admit
    from src.field_recovery import material_key
    from src.fields import load_schema

    # the goldens predate the D2 per-axle rim fields: they cover the fields they were written for
    specs = [s for s in load_schema() if s["name"] not in ("rim_diameter_front_in", "rim_diameter_rear_in")]
    docs = store_documents(cache)
    out: dict[str, dict] = {}
    for target, (payload, vehicle) in targets().items():
        adm = AdmissionContext.for_run(payload, vehicle, specs, "IL")
        for key, doc in sorted(docs.items()):
            cands, _ = harvest_document(cache, doc, specs)
            for cand in cands:
                decision = admit(adm, cache, candidate_request(cand["field"], {**cand, "document_id": doc}),
                                 list(docs.values()))
                name = json.dumps([target, key, cand["field"], material_key(cand.get("value")), cand.get("unit"),
                                   cand.get("quote")], ensure_ascii=False)
                if decision.get("accepted"):
                    record = {k: v for k, v in decision["record"].items() if k not in VOLATILE}
                    out[name] = {"accepted": True, "record": json.loads(json.dumps(record, default=str))}
                else:
                    out[name] = {"accepted": False, "reasons": decision.get("reasons")}
    return out


def compute() -> dict[str, dict]:
    from src.storage.cache import DocumentCache

    with tempfile.TemporaryDirectory() as tmp:
        return decisions(DocumentCache(Path(tmp) / "cache"))


if __name__ == "__main__":
    results = compute()
    if "--write-golden" in sys.argv:
        lines = [json.dumps(k, ensure_ascii=False) + ": " + json.dumps(results[k], ensure_ascii=False, sort_keys=True)
                 for k in sorted(results)]          # one decision per line
        GOLDEN.write_text("{\n" + ",\n".join(lines) + "\n}\n", "utf-8")
    print(json.dumps({"decisions": len(results), "accepted": sum(1 for r in results.values() if r["accepted"])},
                     indent=1))
