"""A SYNTHETIC G6-like finished run for the PR #40 proof gate (NOT production artifacts).

Modelled on the production evidence described for run 20261003T193112Z (XPeng G6 2026 AWD MAX, record 101122): an
official IL importer page covering RWD and AWD (values stated once: wheelbase 2890, height 1650, AC 11 kW, cargo 571; per
drivetrain: range, curb weight), an importer warranty page that names no model, an official foreign (DE) spec page and a
non-official review as independent sources for the precision proxy. The page text never says "electric". Every evidence
item is a store_evidence request the run "made", admitted (and bound) by the code that builds the fixture: the stored
copy in tests/fixtures/pr40_runs was built on main @ 230df43 (binding-v3), so a replay shows binding-v3 -> today.

    python tests/fixtures/pr40_g6_run.py <dest>      # writes <dest>/runs/SYNTH-pr40/101122 and <dest>/runs/_cache
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
                        "model_code": "NSGHA", "segment": "private", "government_record_id": "101122"},
           "engine_drivetrain": {"engine_cc": 0, "power_hp": 486, "propulsion_normalized": "battery_electric",
                                 "drivetrain_normalized": "awd"},
           "structure": {"body_normalized": "suv"}}

IMPORTER = "https://www.heyxpeng.co.il/g6"
IMPORTER_HTML = """<html><head><title>XPENG G6 | אקספנג ישראל</title></head><body><h1>XPENG G6</h1>
<table><tr><td></td><td>RWD 286 כ"ס</td><td>AWD 486 כ"ס</td></tr>
<tr><td>טווח נסיעה (WLTP)</td><td>570 ק"מ</td><td>550 ק"מ</td></tr>
<tr><td>משקל עצמי</td><td>2,100 ק"ג</td><td>2,190 ק"ג</td></tr>
<tr><td>תאוצה 0-100</td><td>6.6 שניות</td><td>4.1 שניות</td></tr></table>
<p>בסיס גלגלים 2,890 מ"מ</p><p>גובה 1,650 מ"מ</p><p>נפח תא מטען 571 ליטר</p>
<p>הספק טעינת AC מרבי 11 kW</p><p>הספק טעינה מהירה עד 451 kW</p></body></html>"""
WARRANTY = "https://www.heyxpeng.co.il/warranty"
WARRANTY_HTML = """<html><head><title>אחריות | אקספנג ישראל</title></head><body><h1>אחריות</h1>
<p>אחריות לרכב: 7 שנים או 160,000 ק"מ, המוקדם מביניהם.</p>
<p>אחריות סוללה: 8 שנים או 160,000 ק"מ.</p></body></html>"""
FOREIGN = "https://www.xpeng.com/de-de/g6/specs"
FOREIGN_HTML = """<html><head><title>XPENG G6 AWD Performance - Technical data</title></head><body>
<h1>XPENG G6 AWD Performance 486 PS</h1><p>Electric SUV</p><p>Wheelbase 2,890 mm</p><p>Height 1,650 mm</p>
<p>Boot space 571 litres</p><p>WLTP range 550 km</p><p>Kerb weight 2,190 kg</p></body></html>"""
REVIEW = "https://www.carnews.co.il/xpeng-g6-review"
REVIEW_HTML = """<html><head><title>מבחן דרכים: XPeng G6</title></head><body><h1>XPeng G6</h1>
<p>בסיס הגלגלים 2,890 מ"מ ותא המטען 571 ליטר.</p></body></html>"""

DOCUMENTS = ((IMPORTER, IMPORTER_HTML), (WARRANTY, WARRANTY_HTML), (FOREIGN, FOREIGN_HTML), (REVIEW, REVIEW_HTML))
# (field, value, quote, url) store_evidence requests of the run
REQUESTS = (
    ("wheelbase_mm", 2890, 'בסיס גלגלים 2,890 מ"מ', IMPORTER),
    ("height_mm", 1650, 'גובה 1,650 מ"מ', IMPORTER),
    ("cargo_volume_l", 571, "נפח תא מטען 571 ליטר", IMPORTER),
    ("ac_max_charging_power_kw", 11, "הספק טעינת AC מרבי 11 kW", IMPORTER),
    ("dc_max_charging_power_kw", 451, "הספק טעינה מהירה עד 451 kW", IMPORTER),
    ("electric_range_km", 550, 'טווח נסיעה (WLTP) | 550 ק"מ', IMPORTER),
    ("curb_weight_kg", 2190, 'משקל עצמי | 2,190 ק"ג', IMPORTER),
    ("acceleration_0_100_s", 4.1, "תאוצה 0-100 | 4.1 שניות", IMPORTER),
    ("vehicle_warranty", "7 years / 160,000 km", 'אחריות לרכב: 7 שנים או 160,000 ק"מ', WARRANTY),
    ("warranty_years", 7, 'אחריות לרכב: 7 שנים או 160,000 ק"מ', WARRANTY),
    ("warranty_km", 160000, 'אחריות לרכב: 7 שנים או 160,000 ק"מ', WARRANTY),
    ("wheelbase_mm", 2890, "Wheelbase 2,890 mm", FOREIGN),
    ("height_mm", 1650, "Height 1,650 mm", FOREIGN),
    ("cargo_volume_l", 571, "Boot space 571 litres", FOREIGN),
    ("electric_range_km", 550, "WLTP range 550 km", FOREIGN),
    ("curb_weight_kg", 2190, "Kerb weight 2,190 kg", FOREIGN),
    ("wheelbase_mm", 2890, 'בסיס הגלגלים 2,890 מ"מ', REVIEW),
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
    docs = {url: put(cache, url, html, "html") for url, html in DOCUMENTS}
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
    run_dir = dest / "runs" / "SYNTH-pr40" / "101122"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "input.json").write_text(json.dumps(PAYLOAD, ensure_ascii=False), "utf-8")
    (run_dir / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), "utf-8")
    return run_dir


if __name__ == "__main__":
    print(build(sys.argv[1]))
