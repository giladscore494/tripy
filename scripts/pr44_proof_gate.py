"""PR #44 proof gate: the Israeli source playbook. Read-only.

    fixtures (default, offline)
        P3  the Q3 cartube rows: torque candidates and admission ("סל״ד מומנט מרבי | 1,500" rejected; 32.6 / 33 קג"מ ->
            320 / 324 Nm)
        P4  the Q8 auto.co.il page 538115 (production rows): engine-invariant fields exact, power-dependent fields not;
            the Q8 2020 article, the Q8 model page and the Q7 "הבכיר" sentence stay non-exact
        regression: every stored fixture run and the goldens, exact before -> after (scripts/pr43_proof_gate.regression)
    --runs-dir R [--cache-dir C] --run RUN_ID ...   (production, after deploy: /data/runs)
        the binding replay of each run with this code: fields ok per vehicle recorded -> now, and the named checks (the
        Q3 torque 1500, the Q7 22" rim, the Q8 5.8 s, the Q8 gearbox of the 2020 article and of the model page)
    --runs-dir R --fresh RUN_ID
        a fresh run: ok fields per vehicle, their median, vehicles with 0 ok, and every ok value with its source URL and
        binding basis (for spot-checking)

    python scripts/pr44_proof_gate.py [--out pr44_proof_gate.json]
    python scripts/pr44_proof_gate.py --runs-dir /data/runs --run 20261004T171912Z-glm-5.3-flash-manufacturer ...
    python scripts/pr44_proof_gate.py --runs-dir /data/runs --fresh <run_id>
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for path in (ROOT, ROOT / "tests", ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from src.binding_replay import replay_run  # noqa: E402

NAMED = {  # (record, field, value, url fragment) -> must NOT be ok / exact now
    "q3_torque_1500": ("94995", "torque_nm", 1500, None),
    "q7_rim_22": ("15400", "rim_diameter_in", 22, None),
    "q8_acceleration_5_8": ("15376", "acceleration_0_100_s", 5.8, None),
    "q8_gearbox_2020_article": ("15376", "gearbox_type", "automatic", "q8-tfsi-e"),
    "q8_gear_count_2020_article": ("15376", "gear_count", 8, "q8-tfsi-e"),
    "q8_gearbox_model_page": ("15376", "gearbox_type", "automatic", "auto.co.il/cars/audi/q8/"),
    "q8_gear_count_model_page": ("15376", "gear_count", 8, "auto.co.il/cars/audi/q8/"),
}


def _record(decision: dict) -> dict:
    record = decision.get("record") or {}
    return {"accepted": decision.get("accepted"), "reasons": decision.get("reasons"),
            "note": decision.get("semantic_note"), "value": record.get("value"),
            "binding_level": record.get("binding_level"), "variant_match": record.get("variant_match"),
            "binding_basis": record.get("binding_basis"), "binding_flags": record.get("binding_flags"),
            "version_page": (record.get("version_page") or {}).get("status"),
            "version_page_reason": (record.get("version_page") or {}).get("reason")}


def fixtures() -> dict:
    from fixtures import pr43_audi_pages as A
    from fixtures import pr44_pages as P
    from fixtures.corolla_harvest import put
    from src.candidate_harvest import harvest_document
    from src.evidence_admission import AdmissionContext, admit
    from src.fields import resolve_requested_fields
    from src.storage.cache import DocumentCache

    cache = DocumentCache(Path(tempfile.mkdtemp(prefix="pr44_gate_")) / "cache")

    def decide(payload, url, html, field, value, quote):
        propulsion = payload["engine_drivetrain"]["propulsion_normalized"]
        adm = AdmissionContext.for_run(payload, None, resolve_requested_fields(None, propulsion=propulsion), "IL")
        doc = put(cache, url, html, "html")
        return _record(admit(adm, cache, {"field": field, "value": value, "quote": quote, "document_id": doc}, [doc]))

    q3_doc = put(cache, P.Q3_CARTUBE_URL, P.q3_cartube_html(), "html")
    torque = sorted(c["value"] for c in harvest_document(cache, q3_doc, resolve_requested_fields(
        None, propulsion="conventional"))[0] if c["field"] == "torque_nm")
    p3 = {"torque_candidates": torque,
          "rpm_row_1500": decide(A.Q3_PAYLOAD, P.Q3_CARTUBE_URL, P.q3_cartube_html(), "torque_nm", 1500,
                                 "סל״ד מומנט מרבי | 1,500"),
          "kgfm_32_6": decide(A.Q3_PAYLOAD, P.Q3_CARTUBE_URL, P.q3_cartube_html(), "torque_nm", 320,
                              'מומנט מרבי (קג"מ) | 32.6'),
          "kgfm_33": decide(A.Q3_PAYLOAD, P.Q3_CARTUBE_URL, P.q3_cartube_html(), "torque_nm", 324, "מומנט | 33 קג״מ")}
    rows = {"gearbox_type": ("automatic", "תיבת הילוכים | אוטומטית פלנטרית (רגילה)"),
            "gear_count": (8, "מספר הילוכים | 8"), "length_mm": (4990, 'אורך (ס"מ) | 499'),
            "width_mm": (2000, 'רוחב (ס"מ) | 200'), "height_mm": (1630, 'גובה (ס"מ) | 163'),
            "wheelbase_mm": (3000, 'בסיס גלגלים (ס"מ) | 300'),
            "acceleration_0_100_s": (5.7, '0 ל-100 קמ"ש (שניות) | 5.7'),
            "top_speed_kmh": (240, 'מהירות מירבית (קמ"ש) | 240'), "curb_weight_kg": (2415, 'משקל (ק"ג) | 2415')}
    p4 = {"q8_538115": {f: decide(A.Q8_PAYLOAD, P.Q8_538115_URL, P.q8_538115_html(), f, v, q)
                        for f, (v, q) in rows.items()},
          "q8_2020_article": {f: decide(A.Q8_PAYLOAD, A.Q8_2020_URL, A.q8_2020_html(), f, v, q) for f, v, q in (
              ("acceleration_0_100_s", 5.8, A.Q8_2020_QUOTE), ("gearbox_type", "automatic", A.Q8_GEARBOX),
              ("gear_count", 8, A.Q8_GEARBOX))},
          "q8_model_page": {f: decide(A.Q8_PAYLOAD, P.Q8_MODEL_URL, P.q8_model_html(), f, v, P.Q8_MODEL_QUOTE)
                            for f, v in (("gearbox_type", "automatic"), ("gear_count", 8))},
          "q7_senior_rim_22": decide(A.Q7_PAYLOAD, A.Q7_URL, A.q7_html(), "rim_diameter_in", 22, A.Q7_QUOTE_22)}
    return {"p3": p3, "p4": p4}


def _named(record: str, items: list[dict], summary: dict) -> dict:
    out = {}
    for name, (rec, field, value, url) in NAMED.items():
        if rec != record:
            continue
        hits = [i for i in items if i["field"] == field and str(i.get("value")) == str(value)
                and (url is None or url in str(i.get("source_url") or ""))]
        state = (summary.get("fields") or {}).get(field) or {}
        exact = [i for i in hits if i.get("variant_match_now") == "exact" and not i.get("rejected_now")]
        out[name] = {"items": len(hits), "exact_now": len(exact), "rejected_now": sum(1 for i in hits
                                                                                       if i.get("rejected_now")),
                     "field_state_now": state.get("would_be_state"),
                     "ok_now_through_this_value": bool(exact) and state.get("would_be_ok", False)}
    return out


def replay(runs_dir: Path, cache_dir: Path | None, run_ids: list[str]) -> dict:
    out = []
    for run_id in run_ids:
        for run_dir in sorted(p for p in (runs_dir / run_id).iterdir() if (p / "events.jsonl").is_file()):
            result = replay_run(run_dir, cache_dir, write=False)
            v = result["summary"]["vehicle"]
            out.append({"run": run_id, "record": run_dir.name, "fields_ok_recorded": v["fields_ok_recorded"],
                        "fields_ok_now": v["fields_ok_now"], "fields_newly_ok": v["fields_newly_ok"],
                        "fields_no_longer_ok": v["fields_no_longer_ok"],
                        "named": _named(run_dir.name, result["items"], result["summary"])})
    return {"vehicles": out}


def fresh(runs_dir: Path, cache_dir: Path | None, run_id: str) -> dict:
    vehicles = []
    for run_dir in sorted(p for p in (runs_dir / run_id).iterdir() if (p / "events.jsonl").is_file()):
        result = replay_run(run_dir, cache_dir, write=False)
        fields = result["summary"]["fields"]
        ok = sorted(n for n, f in fields.items() if f["would_be_ok"])
        values = [{"field": i["field"], "value": i.get("value"), "source_url": i.get("source_url"),
                   "binding_basis": i.get("binding_basis_now"), "source_authority": i.get("source_authority")}
                  for i in result["items"] if i["kind"] == "evidence" and i["field"] in ok
                  and i.get("variant_match_now") == "exact" and not i.get("rejected_now")]
        vehicles.append({"record": run_dir.name, "fields_ok": len(ok), "ok_fields": ok, "ok_values": values})
    counts = [v["fields_ok"] for v in vehicles]
    return {"run": run_id, "vehicles": vehicles, "median_ok": statistics.median(counts) if counts else None,
            "vehicles_with_0_ok": sum(1 for c in counts if c == 0),
            "targets": {"median_ok_at_least": 10, "vehicles_with_0_ok_at_most": 2}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs-dir")
    parser.add_argument("--cache-dir")
    parser.add_argument("--run", action="append", default=[])
    parser.add_argument("--fresh")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    if args.runs_dir:
        runs = Path(args.runs_dir)
        cache = Path(args.cache_dir) if args.cache_dir else None
        report = {"replay": replay(runs, cache, args.run)} if args.run else {}
        if args.fresh:
            report["fresh"] = fresh(runs, cache, args.fresh)
    else:
        from pr43_proof_gate import regression

        report = {"fixtures": fixtures(), "regression": regression()}
    text = json.dumps(report, ensure_ascii=False, indent=1, default=str)
    if args.out:
        Path(args.out).write_text(text, "utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
