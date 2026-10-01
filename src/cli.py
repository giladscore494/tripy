"""Headless single-vehicle run (same code path as the UI).

    python -m src.cli --dry-run                 # show what would run; needs no API key
    python -m src.cli                           # research vehicle #44 (101122) once, then stop
    python -m src.cli --record-id 38626         # any single benchmark vehicle

Exactly one vehicle per invocation; it never continues to another vehicle.
Configuration comes from the same environment variables as the app.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from .agent import PROMPT_VERSION, AgentConfig, effective_glm_config
from .benchmark import HANDSHAKE_RECORD_ID, benchmark_vehicles, research_one, start_batch, vehicle_label
from .db import Level15Error, build_level15_payload, load_level15
from .glm_client import GLMClient, GLMError, GLMSettings
from .pricing import default_pricing
from .storage.cache import DocumentCache
from .storage.run_log import new_batch_id
from .tools import ToolConfig


def _parse(argv: list[str] | None) -> argparse.Namespace:
    env = os.environ.get
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--record-id", default=HANDSHAKE_RECORD_ID, help="upstream_record_id (default: #44, 101122)")
    p.add_argument("--model", default=env("GLM_MODEL", ""))
    p.add_argument("--thinking", choices=["", "enabled", "disabled"], default=env("GLM_THINKING", ""),
                   help='"" = provider default (nothing sent)')
    p.add_argument("--max-steps", type=int, default=int(env("AGENT_MAX_STEPS") or 30))
    p.add_argument("--max-tokens", type=int, default=0, help="0 = provider default")
    p.add_argument("--search-backend", choices=["glm", "duckduckgo"], default=env("SEARCH_BACKEND") or "glm")
    p.add_argument("--data-source", choices=["auto", "database", "snapshot"], default="auto")
    p.add_argument("--no-level3", action="store_true", help="Skip Level 3 open research")
    p.add_argument("--runs-dir", default=env("MILO_RUNS_DIR") or "runs")
    p.add_argument("--dry-run", action="store_true", help="Print the plan and effective config; call nothing")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    vehicles = {v["upstream_record_id"]: v for v in benchmark_vehicles()}
    vehicle = vehicles.get(str(args.record_id))
    if vehicle is None:
        print(f"error: {args.record_id} is not a Benchmark v1 record id", file=sys.stderr)
        return 2
    try:
        extra_body = json.loads(os.environ.get("GLM_EXTRA_BODY") or "{}")
    except ValueError as exc:
        print(f"error: GLM_EXTRA_BODY is not valid JSON: {exc}", file=sys.stderr)
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

    settings = GLMSettings.from_env()
    settings.model = args.model
    agent_cfg = AgentConfig(max_steps=args.max_steps, max_tokens=args.max_tokens or None,
                            include_level3=not args.no_level3, thinking=args.thinking, extra_body=extra_body)
    tool_cfg = ToolConfig(search_backend=args.search_backend)
    pricing = default_pricing(args.model)

    if args.dry_run:
        stub = SimpleNamespace(settings=settings, model=settings.model)
        payload = build_level15_payload(row)
        print(json.dumps({
            "vehicle": vehicle_label(vehicle),
            "level15_source": load.source, "level15_note": load.note,
            "level15_identity": payload["identity"],
            "level15_engine_drivetrain": payload["engine_drivetrain"],
            "payload_chars": len(json.dumps(payload, ensure_ascii=False)),
            "glm_config": effective_glm_config(stub, agent_cfg, tool_cfg),
            "api_key_present": bool(settings.api_key),
            "model_id_present": bool(settings.model),
            "pricing": pricing,
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
    cache = DocumentCache(Path(os.environ.get("MILO_CACHE_DIR") or runs_dir / "_cache"))
    batch_id = new_batch_id(f"{args.model}-one-{vehicle['upstream_record_id']}")
    start_batch(runs_dir, batch_id, client=client, agent_cfg=agent_cfg, tool_cfg=tool_cfg, pricing=pricing,
                vehicles=[vehicle], level15_source=load.source, level15_note=load.note,
                selection=f"one:{vehicle['upstream_record_id']} (cli)", prompt_version=PROMPT_VERSION)

    def listen(kind: str, event: dict) -> None:
        if kind in ("tool_call", "api_error", "error", "run_finished"):
            brief = {k: v for k, v in event.items() if k not in ("ts", "seq", "result", "body")}
            print(f"[{kind}] {json.dumps(brief, ensure_ascii=False, default=str)[:300]}", flush=True)

    result = research_one(vehicle, row, client=client, cache=cache, runs_dir=runs_dir, batch_id=batch_id,
                          agent_cfg=agent_cfg, tool_cfg=tool_cfg, pricing=pricing, level15_source=load.source,
                          listener=listen)
    m = result["metrics"]
    print(json.dumps({
        "batch_id": batch_id, "run_dir": str((runs_dir / batch_id / vehicle["upstream_record_id"]).resolve()),
        "status": result["status"], "error": result["error"], "api_error": result["api_error"],
        "duration_s": result["duration_s"], "usage": result["usage"], "search_api_calls": result["search_api_calls"],
        "tool_calls": m["tool_calls"], "documents_opened": m["documents_opened"], "evidence_items": m["evidence_items"],
        "fields_with_value": m["fields_with_value"], "cost": result["cost"],
    }, ensure_ascii=False, indent=1, default=str))
    return 1 if result["status"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
