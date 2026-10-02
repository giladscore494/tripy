"""Headless single-vehicle run (same code path as the UI).

    export GLM_API_KEY="..."
    export GLM_MODEL="glm-5.3-flash"
    python -m src.cli --dry-run                 # show what would run; needs no API key
    python -m src.cli                           # research vehicle #44 (101122) once, then stop
    python -m src.cli --record-id 38626         # any single benchmark vehicle

    # Finalize a saved run from its logged research (no search, no fetch; one finalization call):
    python -m src.cli --finalize-existing --batch-id <batch_id> --record-id 101122
    python -m src.cli --finalize-existing --batch-id <batch_id> --record-id 101122 --dry-run

Exactly one vehicle per invocation; it never continues to another vehicle.
Configuration comes from the same environment variables as the app. Ctrl+C
stops the run, saves a partial result.json and exits with code 130 without any
further model call.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from .agent import PROMPT_VERSION, agent_config_from_env, effective_config, tool_config_from_env
from .benchmark import (HANDSHAKE_RECORD_ID, benchmark_vehicles, compute_metrics, research_one, start_batch,
                        vehicle_label)
from .db import Level15Error, build_level15_payload, load_level15
from .fields import parse_field_list, propulsion_of, resolve_requested_fields
from .glm_client import GLMClient, GLMError, GLMSettings
from .pricing import default_pricing
from .recovery import RecoveryError, finalize_existing_run, plan_recovery
from .storage.cache import DocumentCache
from .storage.run_log import new_batch_id

EXIT_INTERRUPTED = 130
FAILED_STATUSES = ("research_failed", "finalization_failed", "error")


def _parse(argv: list[str] | None) -> argparse.Namespace:
    env = os.environ.get
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--record-id", default=HANDSHAKE_RECORD_ID, help="upstream_record_id (default: #44, 101122)")
    p.add_argument("--model", default=env("GLM_MODEL", ""), help="Research model id (GLM_MODEL)")
    p.add_argument("--finalizer-model", default=env("GLM_FINALIZER_MODEL", ""),
                   help="Model id for the compact finalization call only (GLM_FINALIZER_MODEL; empty = --model)")
    p.add_argument("--thinking", choices=["", "enabled", "disabled"], default=env("GLM_THINKING", ""),
                   help='"" = provider default (nothing sent)')
    p.add_argument("--max-steps", type=int, default=int(env("AGENT_MAX_STEPS") or 12),
                   help="Soft research budget in model turns (AGENT_MAX_STEPS, default 12)")
    p.add_argument("--no-new-research-turns", type=int, default=None,
                   help="Finalize after N consecutive turns with no new research artifact; 0 = off "
                        "(AGENT_NO_NEW_RESEARCH_TURNS, default 2)")
    p.add_argument("--fields", default=None,
                   help="Requested enrichment fields for this run: comma list or JSON list of names/specs "
                        "(ENRICHMENT_FIELDS; default: every field in the enrichment schema)")
    p.add_argument("--no-field-recovery", action="store_true",
                   help="Skip targeted retries of failed requested fields (FIELD_RECOVERY_ENABLED=false)")
    p.add_argument("--field-recovery-max-attempts", type=int, default=None,
                   help="Retry attempts per failed field (FIELD_RECOVERY_MAX_ATTEMPTS, default 2)")
    p.add_argument("--field-recovery-max-steps", type=int, default=None,
                   help="Model turns per retry attempt (FIELD_RECOVERY_MAX_STEPS, default 4)")
    p.add_argument("--recovery-mode", choices=["cluster", "legacy"], default=None,
                   help="Tail recovery: cluster (default; RECOVERY_MODE) or the legacy per-field retries")
    p.add_argument("--max-tokens", type=int, default=0, help="0 = provider default")
    p.add_argument("--search-backend", choices=["glm", "duckduckgo"], default=env("SEARCH_BACKEND") or "glm")
    p.add_argument("--data-source", choices=["auto", "database", "snapshot"], default="auto")
    p.add_argument("--level3", action="store_true",
                   help="Include Level 3 open research (off by default; INCLUDE_LEVEL3=true also enables it)")
    p.add_argument("--no-level3", action="store_true", help="Skip Level 3 open research (the default)")
    p.add_argument("--runs-dir", default=env("MILO_RUNS_DIR") or "runs")
    p.add_argument("--dry-run", action="store_true", help="Print the plan and effective config; call nothing")
    p.add_argument("--finalize-existing", action="store_true",
                   help="Finalize a saved run (--batch-id, --record-id) from its logged research; no new research")
    p.add_argument("--batch-id", default="", help="Batch to recover with --finalize-existing")
    p.add_argument("--force", action="store_true", help="With --finalize-existing: also when output already exists")
    p.add_argument("--export-feedback", action="store_true",
                   help="Aggregate every run's training feedback into training_feedback.jsonl / .csv (no API calls)")
    p.add_argument("--feedback-dir", default="", help="With --export-feedback: output folder (default runs/_feedback)")
    return p.parse_args(argv)


def _agent_cfg(args: argparse.Namespace, extra_body: dict):
    overrides = dict(max_steps=args.max_steps, max_tokens=args.max_tokens or None, thinking=args.thinking,
                     extra_body=extra_body)
    if args.level3 or args.no_level3:            # otherwise INCLUDE_LEVEL3 / the default (off) decides
        overrides["include_level3"] = bool(args.level3 and not args.no_level3)
    if args.no_new_research_turns is not None:
        overrides["no_new_research_turns"] = args.no_new_research_turns
    if args.fields:
        overrides["requested_fields"] = parse_field_list(args.fields)
    if args.no_field_recovery:
        overrides["field_recovery_enabled"] = False
    if args.field_recovery_max_attempts is not None:
        overrides["field_recovery_max_attempts"] = args.field_recovery_max_attempts
    if args.field_recovery_max_steps is not None:
        overrides["field_recovery_max_steps"] = args.field_recovery_max_steps
    if args.recovery_mode:
        overrides["recovery_mode"] = args.recovery_mode
    return agent_config_from_env(**overrides)


def _settings(args: argparse.Namespace) -> GLMSettings:
    settings = GLMSettings.from_env()
    settings.model = args.model
    settings.finalizer_model = (args.finalizer_model or "").strip()
    return settings


def _cache(runs_dir: Path) -> DocumentCache:
    return DocumentCache(Path(os.environ.get("MILO_CACHE_DIR") or runs_dir / "_cache"))


def _listener(kind: str, event: dict) -> None:
    if kind in ("tool_call", "api_error", "error", "research_stopped", "finalization_started",
                "finalization_failed", "finalization_finished", "interrupted", "run_finished",
                "recovery_started", "recovery_finished", "duplicate_work", "field_retry_queue",
                "field_recovery_started", "field_recovery_finished", "tool_reused",
                "field_recovery_queue_resolved_indirectly", "evidence_reused", "field_recovery_early_resolved",
                "field_recovery_budget_exhausted", "deterministic_harvest_summary", "document_sweep_started",
                "document_sweep_finished", "document_sweep_skipped", "candidate_missed_by_deterministic_harvest",
                "finalization_checkpoint_written", "tool_blocked", "cluster_recovery_started",
                "cluster_recovery_no_novelty_stop", "cluster_recovery_budget_extended", "cluster_recovery_skipped"):
        brief = {k: v for k, v in event.items()
                 if k not in ("ts", "seq", "result", "body", "glm_config", "headers", "tracking", "queue",
                              "reply_text", "candidates", "fields_with_candidates", "fields_without_candidates",
                              "presented_candidate_keys", "triage", "clusters", "novelty", "states_before",
                              "states_after")}
        if kind == "api_error":
            brief = {"request": brief.get("request_kind"), "attempt": f"{brief.get('attempt')}/{brief.get('max_attempts')}",
                     "status": brief.get("status"), "timeout": brief.get("timeout"),
                     "usage_unknown": brief.get("usage_unknown"), "will_retry": brief.get("will_retry"),
                     "error": str(brief.get("error"))[:160]}
        print(f"[{kind}] {json.dumps(brief, ensure_ascii=False, default=str)[:300]}", flush=True)


def _worst_case_recovery_turns(agent_cfg, payload: dict, vehicle: dict) -> int:
    """Upper bound on extra model turns if every requested field failed primary research."""
    from .field_recovery import max_attempts_for

    if not agent_cfg.field_recovery_enabled:
        return 0
    specs = resolve_requested_fields(agent_cfg.requested_fields or None, propulsion=propulsion_of(payload, vehicle))
    applicable = [s for s in specs if s.get("applicable", True)]
    if agent_cfg.recovery_mode == "cluster":
        from .agent import CLUSTER_TURN_CEILING
        from .tail_planner import cluster_of

        attempts: dict[str, int] = {}
        for s in applicable:
            n = min(agent_cfg.cluster_max_attempts, max_attempts_for(s, agent_cfg.field_recovery_max_attempts))
            attempts[cluster_of(s)] = max(attempts.get(cluster_of(s), 0), n)
        turns = sum(attempts.values()) * min(agent_cfg.cluster_max_turns, CLUSTER_TURN_CEILING)
    else:
        turns = sum(max_attempts_for(s, agent_cfg.field_recovery_max_attempts) * agent_cfg.field_recovery_max_steps
                    for s in applicable)
    cap = agent_cfg.field_recovery_max_total_steps
    return min(turns, cap) if cap else turns


def _summary(result: dict, extra: dict | None = None) -> str:
    m = result.get("metrics") or {}
    out = {**(extra or {}),
           "status": result.get("status"), "stop_reason": result.get("stop_reason"),
           "research_steps": result.get("research_steps"), "error": result.get("error"),
           "research_model": result.get("research_model"), "finalizer_model": result.get("finalizer_model"),
           "duration_s": result.get("duration_s"), "usage_research": result.get("usage_research"),
           "usage_field_recovery": result.get("usage_field_recovery"),
           "usage_finalizer": result.get("usage_finalizer"), "api_stats": result.get("api_stats"),
           "field_recovery": {k: (result.get("field_recovery") or {}).get(k) for k in (
               "mode", "queue", "fields_retried", "fields_recovered", "fields_still_failed", "attempt_count", "turns",
               "tail_fields_at_start", "tail_fields_resolved", "tail_search_calls", "no_novelty_stops")},
           "search_api_calls": result.get("search_api_calls"), "tool_calls": m.get("tool_calls"),
           "documents_opened": m.get("documents_opened"), "evidence_items": m.get("evidence_items"),
           "fields_with_value": m.get("fields_with_value"),
           "finalizer_input_chars": (result.get("finalization") or {}).get("finalizer_input_chars"),
           "cost": result.get("cost"), "cost_note": result.get("cost_note")}
    return json.dumps(out, ensure_ascii=False, indent=1, default=str)


def _finalize_existing(args: argparse.Namespace, extra_body: dict) -> int:
    if not args.batch_id:
        print("error: --finalize-existing needs --batch-id", file=sys.stderr)
        return 2
    runs_dir = Path(args.runs_dir)
    record_id = str(args.record_id)
    agent_cfg = _agent_cfg(args, extra_body)
    cache = _cache(runs_dir)
    settings = _settings(args)
    if args.dry_run:
        try:
            plan = plan_recovery(runs_dir, args.batch_id, record_id, cache=cache, config=agent_cfg)
        except RecoveryError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        plan.update({"finalizer_model": settings.effective_finalizer_model or None,
                     "api_key_present": bool(settings.api_key), "will_search": False, "will_fetch": False})
        print(json.dumps(plan, ensure_ascii=False, indent=1))
        return 0
    try:
        client = GLMClient(settings)
    except GLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    vehicles = {v["upstream_record_id"]: v for v in benchmark_vehicles()}
    vehicle = vehicles.get(record_id)
    try:
        result = finalize_existing_run(runs_dir, args.batch_id, record_id, client=client, cache=cache,
                                       config=agent_cfg, tool_config=tool_config_from_env(), force=args.force,
                                       listener=_listener,
                                       metrics_fn=lambda r: compute_metrics(r, vehicle, cache))
    except RecoveryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted: recovery stopped; the saved run is unchanged apart from appended events.",
              file=sys.stderr)
        return EXIT_INTERRUPTED
    print(_summary(result, {"batch_id": args.batch_id, "record_id": record_id, "recovered": True,
                            "run_dir": str((runs_dir / args.batch_id / record_id).resolve())}))
    return 1 if result["status"] in FAILED_STATUSES else 0


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    try:
        extra_body = json.loads(os.environ.get("GLM_EXTRA_BODY") or "{}")
    except ValueError as exc:
        print(f"error: GLM_EXTRA_BODY is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if args.export_feedback:
        from .training_feedback import export_feedback

        print(json.dumps(export_feedback(Path(args.runs_dir), args.feedback_dir or None), indent=1))
        return 0
    if args.finalize_existing:
        return _finalize_existing(args, extra_body)

    vehicles = {v["upstream_record_id"]: v for v in benchmark_vehicles()}
    vehicle = vehicles.get(str(args.record_id))
    if vehicle is None:
        print(f"error: {args.record_id} is not a Benchmark v1 record id", file=sys.stderr)
        return 2
    try:
        load = load_level15([vehicle["upstream_record_id"]], source=args.data_source)
    except Level15Error as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not load.rows:
        print(f"error: no Level 1.5 row for {vehicle['upstream_record_id']}", file=sys.stderr)
        return 2
    row = load.rows[0]

    settings = _settings(args)
    agent_cfg = _agent_cfg(args, extra_body)
    tool_cfg = tool_config_from_env(search_backend=args.search_backend)
    pricing = default_pricing(args.model)

    if args.dry_run:
        stub = SimpleNamespace(settings=settings, model=settings.model)
        payload = build_level15_payload(row)
        config = effective_config(stub, agent_cfg, tool_cfg)
        print(json.dumps({
            "vehicle": vehicle_label(vehicle),
            "level15_source": load.source, "level15_note": load.note,
            "level15_identity": payload["identity"],
            "level15_engine_drivetrain": payload["engine_drivetrain"],
            "payload_chars": len(json.dumps(payload, ensure_ascii=False)),
            "glm_config": config["glm"],
            "research_model": settings.model or None,
            "finalizer_model": settings.effective_finalizer_model or None,
            "agent_config": config["agent"],
            "requested_fields": {s["name"]: s.get("description") for s in resolve_requested_fields(
                agent_cfg.requested_fields or None, propulsion=propulsion_of(payload, vehicle))
                                 if s.get("applicable", True)},
            "field_recovery_worst_case_model_turns": _worst_case_recovery_turns(agent_cfg, payload, vehicle),
            "tool_config": config["tools"],
            "api_key_present": bool(settings.api_key),
            "model_id_present": bool(settings.model),
            "pricing": pricing,
            "pricing_finalizer": default_pricing(settings.effective_finalizer_model),
            "prompt_version": PROMPT_VERSION,
            "runs_dir": str(Path(args.runs_dir).resolve()),
        }, ensure_ascii=False, indent=1))
        return 0

    try:
        client = GLMClient(settings)
    except GLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    runs_dir = Path(args.runs_dir)
    cache = _cache(runs_dir)
    batch_id = new_batch_id(f"{args.model}-one-{vehicle['upstream_record_id']}", runs_dir)
    start_batch(runs_dir, batch_id, client=client, agent_cfg=agent_cfg, tool_cfg=tool_cfg, pricing=pricing,
                vehicles=[vehicle], level15_source=load.source, level15_note=load.note,
                selection=f"one:{vehicle['upstream_record_id']} (cli)", prompt_version=PROMPT_VERSION)
    run_dir = (runs_dir / batch_id / vehicle["upstream_record_id"]).resolve()
    try:
        result = research_one(vehicle, row, client=client, cache=cache, runs_dir=runs_dir, batch_id=batch_id,
                              agent_cfg=agent_cfg, tool_cfg=tool_cfg, pricing=pricing, level15_source=load.source,
                              listener=_listener)
    except KeyboardInterrupt:
        print(f"interrupted: partial result saved to {run_dir / 'result.json'}. To finalize it later without "
              f"repeating research: python -m src.cli --finalize-existing --batch-id {batch_id} "
              f"--record-id {vehicle['upstream_record_id']}", file=sys.stderr)
        return EXIT_INTERRUPTED
    print(_summary(result, {"batch_id": batch_id, "run_dir": str(run_dir)}))
    return 1 if result["status"] in FAILED_STATUSES else 0


if __name__ == "__main__":
    sys.exit(main())
