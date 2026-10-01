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
    p.add_argument("--max-tokens", type=int, default=0, help="0 = provider default")
    p.add_argument("--search-backend", choices=["glm", "duckduckgo"], default=env("SEARCH_BACKEND") or "glm")
    p.add_argument("--data-source", choices=["auto", "database", "snapshot"], default="auto")
    p.add_argument("--no-level3", action="store_true", help="Skip Level 3 open research")
    p.add_argument("--runs-dir", default=env("MILO_RUNS_DIR") or "runs")
    p.add_argument("--dry-run", action="store_true", help="Print the plan and effective config; call nothing")
    p.add_argument("--finalize-existing", action="store_true",
                   help="Finalize a saved run (--batch-id, --record-id) from its logged research; no new research")
    p.add_argument("--batch-id", default="", help="Batch to recover with --finalize-existing")
    p.add_argument("--force", action="store_true", help="With --finalize-existing: also when output already exists")
    return p.parse_args(argv)


def _agent_cfg(args: argparse.Namespace, extra_body: dict):
    overrides = dict(max_steps=args.max_steps, max_tokens=args.max_tokens or None,
                     include_level3=not args.no_level3, thinking=args.thinking, extra_body=extra_body)
    if args.no_new_research_turns is not None:
        overrides["no_new_research_turns"] = args.no_new_research_turns
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
                "recovery_started", "recovery_finished", "duplicate_work"):
        brief = {k: v for k, v in event.items()
                 if k not in ("ts", "seq", "result", "body", "glm_config", "headers", "tracking")}
        if kind == "api_error":
            brief = {"request": brief.get("request_kind"), "attempt": f"{brief.get('attempt')}/{brief.get('max_attempts')}",
                     "status": brief.get("status"), "timeout": brief.get("timeout"),
                     "usage_unknown": brief.get("usage_unknown"), "will_retry": brief.get("will_retry"),
                     "error": str(brief.get("error"))[:160]}
        print(f"[{kind}] {json.dumps(brief, ensure_ascii=False, default=str)[:300]}", flush=True)


def _summary(result: dict, extra: dict | None = None) -> str:
    m = result.get("metrics") or {}
    out = {**(extra or {}),
           "status": result.get("status"), "stop_reason": result.get("stop_reason"),
           "research_steps": result.get("research_steps"), "error": result.get("error"),
           "research_model": result.get("research_model"), "finalizer_model": result.get("finalizer_model"),
           "duration_s": result.get("duration_s"), "usage_research": result.get("usage_research"),
           "usage_finalizer": result.get("usage_finalizer"), "api_stats": result.get("api_stats"),
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
