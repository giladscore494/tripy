"""PR #40 proof gate: the binding-v4 rules over every stored fixture run and the golden fixtures. Read-only, offline.

    (a) fields ok before -> after per run, and the rule that raised each newly ok field (binding replay)
    (b) the Document Variant Map precision proxy: for every DVM-bound value (dvm_region / dvm_shared), does an
        independent source (another domain, binding level >= generation, same field) agree, disagree, or is there none?
        Over the replayed runs AND every admission of the golden fixture documents (tests/fixtures/admission_records.py)
    (c) the goldens (admission records, Corolla binding): exact before -> after, and every changed case

    python scripts/pr40_proof_gate.py [--runs-dir tests/fixtures/baseline_runs] [--out pr40_proof_gate.json]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for path in (ROOT, ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from src.binding_replay import replay_run, run_dirs  # noqa: E402
from src.document_binding import LEVELS  # noqa: E402
from src.field_recovery import material_key  # noqa: E402
from src.source_authority import host_of  # noqa: E402

DVM = ("dvm_region", "dvm_shared")


def _dvm(record: dict) -> str | None:
    rules = record.get("binding_rules") or record.get("binding_rules_now") or []
    basis = record.get("binding_basis") or record.get("binding_basis_now")
    return basis if basis in DVM else next((r for r in rules if r in DVM), None)


def precision(items: list[dict], target: str) -> list[dict]:
    """Per DVM-bound item: agree / disagree / none against items of the same field from another domain bound at
    generation or above (any variant_match except different)."""
    out = []
    for item in items:
        rule = _dvm(item)
        level = item.get("binding_level") or item.get("binding_level_now")
        match = item.get("variant_match") or item.get("variant_match_now")
        if not rule or match != "exact":
            continue
        domain = host_of(item.get("source_url"))
        others = [o for o in items if o is not item and o.get("field") == item.get("field")
                  and host_of(o.get("source_url")) != domain
                  and LEVELS.index(o.get("binding_level") or o.get("binding_level_now") or "unknown")
                  >= LEVELS.index("generation")
                  and (o.get("variant_match") or o.get("variant_match_now")) != "different"]
        values = sorted({material_key(o.get("value")) for o in others})
        mine = material_key(item.get("value"))
        verdict = "none" if not values else "agree" if mine in values and len(values) == 1 else \
            "agree_and_disagree" if mine in values else "disagree"
        out.append({"target": target, "field": item.get("field"), "value": item.get("value"), "rule": rule,
                    "level": level, "region": (item.get("variant_map_region") or item.get("variant_map_region_now")
                                               or {}).get("region_id"),
                    "domain": domain, "verdict": verdict, "independent_values": values,
                    "independent_domains": sorted({host_of(o.get("source_url")) for o in others})})
    return out


def replay_gate(runs_dir: Path) -> dict:
    work = Path(tempfile.mkdtemp(prefix="pr40_gate_"))
    shutil.copytree(runs_dir, work / "runs")
    cache = work / "runs" / "_cache"
    rows = []
    proxy = []
    for run_dir in run_dirs(work / "runs"):
        replay = replay_run(run_dir, cache if cache.is_dir() else None, write=False)
        v = replay["summary"]["vehicle"]
        rows.append({"run": f"{run_dir.parent.name}/{run_dir.name}", "fields": v["fields"],
                     "fields_ok_recorded": v["fields_ok_recorded"], "fields_ok_now": v["fields_ok_now"],
                     "fields_newly_ok_by_rule": v["fields_newly_ok_by_rule"],
                     "evidence_rose_by_rule": v["evidence_rose_by_rule"], "rejected_now": v["rejected_now"],
                     "gap_counts_recorded": v["gap_counts_recorded"], "gap_counts_now": v["gap_counts"],
                     "states_recorded": v["states_recorded"], "states_now": v["states_now"],
                     "variant_map_regions": v["variant_map_regions"],
                     "items": [{k: r.get(k) for k in ("field", "value", "source_url", "binding_level_recorded",
                                                      "binding_level_now", "variant_match_now", "binding_basis_now",
                                                      "binding_rules_now", "binding_gap_now", "rejected_now")}
                               for r in replay["items"] if r["kind"] == "evidence"]})
        proxy += precision([r for r in replay["items"] if r["kind"] == "evidence"], rows[-1]["run"])
    shutil.rmtree(work, ignore_errors=True)
    return {"runs": rows, "precision": proxy}


def golden_gate() -> dict:
    from fixtures import admission_records, corolla_binding

    out: dict = {}
    golden = json.loads(admission_records.GOLDEN.read_text("utf-8"))
    current = admission_records.compute()
    changes, proxy = [], []
    by_target: dict[str, list[dict]] = {}
    for key, old in golden.items():
        new = current[key]
        if new.get("accepted"):
            k = json.loads(key)
            by_target.setdefault(k[0], []).append({**new["record"], "_key": k})
        if not old["accepted"] or not new["accepted"]:
            continue
        o, n = old["record"], new["record"]
        if (o.get("binding_level"), o.get("variant_match")) != (n.get("binding_level"), n.get("variant_match")):
            changes.append({"key": json.loads(key)[:4], "before": [o.get("binding_level"), o.get("variant_match")],
                            "after": [n.get("binding_level"), n.get("variant_match")],
                            "basis": n.get("binding_basis"), "veto": n.get("binding_veto"),
                            "region": (n.get("variant_map_region") or {}).get("identity_text")})
    exact = lambda records: sum(1 for r in records if r["accepted"] and r["record"].get("variant_match") == "exact")  # noqa: E731
    out["admission_records"] = {"records": len(golden), "exact_before": exact(golden.values()),
                                "exact_after": exact(current.values()), "changed": changes,
                                "exact_lost": [c for c in changes if c["before"][1] == "exact"]}
    for target, records in sorted(by_target.items()):
        proxy += precision(records, f"golden:{target}")
    out["admission_records"]["dvm_bound"] = sum(1 for records in by_target.values() for r in records if _dvm(r))
    cgold = json.loads(corolla_binding.GOLDEN.read_text("utf-8"))
    ccur = corolla_binding.compute()
    cchanges = [{"key": json.loads(k)[:4], "before": v, "after": ccur.get(k)} for k, v in cgold.items()
                if {x: ccur.get(k, {}).get(x) for x in ("binding_level", "variant_match")}
                != {x: v.get(x) for x in ("binding_level", "variant_match")}]
    out["corolla_binding"] = {"records": len(cgold),
                              "exact_before": sum(1 for v in cgold.values() if v["variant_match"] == "exact"),
                              "exact_after": sum(1 for v in ccur.values() if v["variant_match"] == "exact"),
                              "changed": cchanges}
    out["precision"] = proxy
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs-dir", action="append", default=None,
                        help="a runs folder (with its _cache); repeatable (default: every stored fixture run: "
                             "tests/fixtures/baseline_runs and tests/fixtures/pr40_runs)")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    dirs = [Path(d) for d in args.runs_dir or []] or [ROOT / "tests" / "fixtures" / "baseline_runs",
                                                      ROOT / "tests" / "fixtures" / "pr40_runs"]
    replays = [replay_gate(d) for d in dirs]
    report = {"replay": {"runs": [r for x in replays for r in x["runs"]],
                         "precision": [p for x in replays for p in x["precision"]]}, "golden": golden_gate()}
    proxy = report["replay"]["precision"] + report["golden"]["precision"]
    report["precision_summary"] = dict(Counter(p["verdict"] for p in proxy))
    report["disagreements"] = [p for p in proxy if p["verdict"] in ("disagree", "agree_and_disagree")]
    text = json.dumps(report, ensure_ascii=False, indent=1, default=str)
    if args.out:
        Path(args.out).write_text(text, "utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
