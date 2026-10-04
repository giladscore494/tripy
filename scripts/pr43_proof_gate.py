"""PR #43 proof gate: hybrid / combustion binding precision. Read-only, offline.

    (a) the binding replay of the PR #43 fixture runs (tests/fixtures/pr43_runs: the synthetic Audi Q8 / Q7 / Q3 documents
        and the G6 dimension triple, admitted by main @ 43eff6d): per field the state recorded -> now, and per evidence
        item the level / match recorded -> now with the PR #43 flags (system_power_unmapped, several_powertrain_versions,
        relative_variant_reference, stale_publication) and the region that proved a version
    (b) every stored fixture run (baseline_runs, pr40_runs, pr42_runs) and the goldens (admission records, Corolla
        binding): exact before -> after (scripts/pr40_proof_gate.py)

    python scripts/pr43_proof_gate.py [--out pr43_proof_gate.json]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for path in (ROOT, ROOT / "tests", ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from src.binding_replay import replay_run, run_dirs  # noqa: E402

PR43_RUNS = ROOT / "tests" / "fixtures" / "pr43_runs"


def _item(r: dict) -> dict:
    region = r.get("variant_map_region_now") or {}
    out = {k: r.get(k) for k in ("kind", "field", "value", "binding_level_recorded", "variant_match_recorded",
                                 "binding_level_now", "variant_match_now", "binding_basis_now", "binding_gap_now",
                                 "binding_flags_now", "entailment_now") if r.get(k) not in (None, [], {})}
    if r.get("rejected_now"):
        out["rejected_now"] = r["rejected_now"]
    if region.get("status"):
        out["region"] = {k: region.get(k) for k in ("region_id", "status", "reason", "designation", "assignment")
                         if region.get(k) not in (None, [], {})}
    return out


def replay_fixture(runs: Path = PR43_RUNS) -> dict:
    work = Path(tempfile.mkdtemp(prefix="pr43_gate_"))
    shutil.copytree(runs, work / "runs")
    out = []
    for run_dir in run_dirs(work / "runs"):
        replay = replay_run(run_dir, work / "runs" / "_cache", write=False)
        summary = replay["summary"]
        fields = {name: {"state_recorded": f["state_recorded"], "state_now": f["would_be_state"]}
                  for name, f in (summary.get("fields") or {}).items()}
        out.append({"run": f"{run_dir.parent.name}/{run_dir.name}",
                    "fields_ok_recorded": summary["vehicle"]["fields_ok_recorded"],
                    "fields_ok_now": summary["vehicle"]["fields_ok_now"],
                    "field_state_changes": {n: f for n, f in fields.items() if f["state_recorded"] != f["state_now"]},
                    "items": [_item(r) for r in replay["items"]]})
    shutil.rmtree(work, ignore_errors=True)
    return {"runs": out}


def regression() -> dict:
    from pr40_proof_gate import golden_gate, replay_gate

    replays = [replay_gate(ROOT / "tests" / "fixtures" / d) for d in ("baseline_runs", "pr40_runs", "pr42_runs")]
    runs = [{k: r[k] for k in ("run", "fields_ok_recorded", "fields_ok_now")}
            | {"exact_now": sum(1 for i in r["items"] if i["variant_match_now"] == "exact")}
            for x in replays for r in x["runs"]]
    golden = golden_gate()
    return {"runs": runs,
            "golden": {name: {k: v for k, v in golden[name].items() if k != "dvm_bound"}
                       for name in ("admission_records", "corolla_binding")}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    report = {"pr43_replay": replay_fixture(), "regression": regression()}
    text = json.dumps(report, ensure_ascii=False, indent=1, default=str)
    if args.out:
        Path(args.out).write_text(text, "utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
