"""PR #42 proof gate: the Document Variant Map on real importer pages. Read-only, offline.

    (a) the binding replay of the PR #42 fixture run (tests/fixtures/pr42_runs: the synthetic heyxpeng.co.il/g6 page and
        the pre-facelift brochure, admitted by main @ 1881314): fields ok before -> after, by rule, and every value
        bound by `dvm_region` (region, tab label, catalog assignment) or vetoed by its region
    (b) the variant map of the fixture documents: region statuses, the tab-labelled groups, the model-code tables and
        the market-trim verdict for the target (101122, G6 2026 MAX, degem_cd 31)
    (c) every stored fixture run (baseline_runs, pr40_runs) and the goldens (admission records, Corolla binding): exact
        before -> after and every changed case (scripts/pr40_proof_gate.py)

    python scripts/pr42_proof_gate.py [--out pr42_proof_gate.json]
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
for path in (ROOT, ROOT / "tests", ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from src.binding_replay import replay_run, run_dirs  # noqa: E402

PR42_RUNS = ROOT / "tests" / "fixtures" / "pr42_runs"
ITEM_KEYS = ("field", "value", "source_url", "binding_level_recorded", "variant_match_recorded", "binding_level_now",
             "variant_match_now", "binding_basis_now", "binding_rules_now")


def _region(item: dict) -> dict:
    region = item.get("variant_map_region_now") or {}
    return {k: region.get(k) for k in ("region_id", "status", "reason", "tab_label", "assignment", "contradicts",
                                       "candidates_after", "region_statuses") if region.get(k) not in (None, [], {})}


def replay_pr42() -> dict:
    work = Path(tempfile.mkdtemp(prefix="pr42_gate_"))
    shutil.copytree(PR42_RUNS, work / "runs")
    out = []
    for run_dir in run_dirs(work / "runs"):
        replay = replay_run(run_dir, work / "runs" / "_cache", write=False)
        v = replay["summary"]["vehicle"]
        items = [r for r in replay["items"] if r["kind"] == "evidence"]
        out.append({
            "run": f"{run_dir.parent.name}/{run_dir.name}",
            "fields_ok_recorded": v["fields_ok_recorded"], "fields_ok_now": v["fields_ok_now"],
            "fields_newly_ok": v["fields_newly_ok"], "fields_newly_ok_by_rule": v["fields_newly_ok_by_rule"],
            "fields_no_longer_ok": v["fields_no_longer_ok"],
            "evidence_exact_recorded": v["evidence_exact_recorded"], "evidence_exact_now": v["evidence_exact_now"],
            "evidence_rose_by_rule": v["evidence_rose_by_rule"], "variant_map_regions": v["variant_map_regions"],
            "dvm_region_bound": [{**{k: r.get(k) for k in ITEM_KEYS}, "region": _region(r),
                                  "market_trim": (r.get("market_trim_now") or {}).get("status")}
                                 for r in items if r.get("binding_basis_now") == "dvm_region"
                                 or "dvm_region" in (r.get("binding_rules_now") or [])],
            "region_vetoed": [{**{k: r.get(k) for k in ITEM_KEYS}, "region": _region(r)} for r in items
                              if "dvm_other_variant" in (r.get("binding_rules_now") or [])],
            "region_unresolved": [{**{k: r.get(k) for k in ITEM_KEYS}, "region": _region(r)} for r in items
                                  if _region(r).get("status") == "unresolved"],
            "unbound_by_dvm": [{k: r.get(k) for k in ITEM_KEYS} for r in items
                               if not (r.get("variant_map_region_now") or {}).get("status")],
        })
    shutil.rmtree(work, ignore_errors=True)
    return {"runs": out}


def maps() -> dict:
    from fixtures import pr42_g6_pages as G
    from fixtures.corolla_harvest import put
    from src.evidence_admission import AdmissionContext
    from src.fields import resolve_requested_fields
    from src.storage.cache import DocumentCache
    try:
        from src.variant_map import market_trim_offer
    except ImportError:                     # the same gate on main (before PR #42) has no market-trim verdict
        market_trim_offer = None

    work = Path(tempfile.mkdtemp(prefix="pr42_maps_"))
    cache = DocumentCache(work / "c")
    specs = resolve_requested_fields(None, propulsion="battery_electric")
    adm = AdmissionContext.for_run(G.PAYLOAD, None, specs, "IL")
    out = {}
    docs ={G.PAGE_URL: put(cache, G.PAGE_URL, G.page_html(), "html"),
            G.BROCHURE_URL: G.put_pdf(cache, G.BROCHURE_URL, G.brochure_pdf())}
    for url, doc in docs.items():
        vmap = adm.material(cache, doc, None).variant_map
        out[url] = {
            "regions_by_status": dict(Counter(r["status"] for r in vmap["regions"])),
            "inventory_powers": sorted({p for item in vmap["inventory"] for p in item["power"]}),
            "regions": [{"id": r["id"], "kind": r["kind"], "text": r["text"][:90], "trim": r["identity"]["trim"],
                         "drivetrain": r["identity"]["drivetrain"], "power": r["identity"]["power"],
                         "unexplained_powers": r.get("unexplained_powers"),
                         "status": r["status"], "assignment": r.get("assignment"),
                         "candidates_after": r.get("candidates_after"), "tab_label": r.get("tab_label"),
                         "gov_model_code": r.get("gov_model_code")} for r in vmap["regions"]],
            "model_codes": vmap.get("model_codes"),
            "market_trim": market_trim_offer(vmap, adm.identity) if market_trim_offer else None}
    shutil.rmtree(work, ignore_errors=True)
    return out


def regression() -> dict:
    from pr40_proof_gate import golden_gate, replay_gate

    replays = [replay_gate(ROOT / "tests" / "fixtures" / d) for d in ("baseline_runs", "pr40_runs")]
    runs = [{k: r[k] for k in ("run", "fields_ok_recorded", "fields_ok_now", "fields_newly_ok_by_rule",
                               "variant_map_regions", "evidence_rose_by_rule")}
            | {"exact_now": sum(1 for i in r["items"] if i["variant_match_now"] == "exact"),
               "items": [{k: i[k] for k in ("field", "value", "binding_level_now", "variant_match_now",
                                            "binding_basis_now")} for i in r["items"]]}
            for x in replays for r in x["runs"]]
    golden = golden_gate()
    return {"runs": runs,
            "golden": {name: {k: v for k, v in golden[name].items() if k != "dvm_bound"}
                       for name in ("admission_records", "corolla_binding")}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    report = {"pr42_replay": replay_pr42(), "maps": maps(), "regression": regression()}
    text = json.dumps(report, ensure_ascii=False, indent=1, default=str)
    if args.out:
        Path(args.out).write_text(text, "utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
