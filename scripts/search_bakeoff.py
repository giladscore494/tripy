"""Search bake-off, command line (PR #46, S4). The same implementation as the web UI's "Search bake-off" page
(src/search_bakeoff.py); the UI runs it as a background job of the server, this runs it in the foreground.

    python scripts/search_bakeoff.py [--backends glm,serper,gemini] [--records 12949,13628] [--fetch-top 1]
                                     [--out DIR]

Keys come from the environment (GLM_API_KEY, SERPER_API_KEY, GEMINI_API_KEY); a selected backend without its key is
refused (exit 2), never replaced. Writes summary.json, records.jsonl and queries.jsonl to --out (default
<TRIPY_DATA_DIR>/bakeoffs/<id>) and prints the metrics table as Markdown. Nothing enters a run's evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.search_bakeoff import (DEFAULT_BACKENDS, BakeoffConfig, live_dependencies, markdown_table,  # noqa: E402
                                new_id, run_bakeoff)
from src.storage.cache import DocumentCache  # noqa: E402
from src.storage.paths import resolve_paths  # noqa: E402
from src.tools.search_backends import unavailable_reason  # noqa: E402


def main(argv: list[str] | None = None, *, make_backend=None, fetch=None, cache=None, records=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--backends", default=",".join(DEFAULT_BACKENDS))
    parser.add_argument("--records", default="", help="comma-separated record ids (default: every benchmark record)")
    parser.add_argument("--fetch-top", type=int, default=1, help="fetch the top candidate (1) or not (0)")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    if make_backend is None:
        missing = [(b, unavailable_reason(b)) for b in backends if b != "glm" and unavailable_reason(b)]
        if missing:
            print("unavailable backends: " + "; ".join(f"{b} ({why})" for b, why in missing), file=sys.stderr)
            return 2
    paths = resolve_paths()
    out = Path(args.out) if args.out else paths.data_dir / "bakeoffs" / new_id()
    cache = cache or DocumentCache(paths.cache_dir)
    if make_backend is None:
        make_backend, live_fetch = live_dependencies(cache)
        fetch = fetch or live_fetch
    config = BakeoffConfig(backends=backends, records=[r.strip() for r in args.records.split(",") if r.strip()],
                           fetch_top=max(0, args.fetch_top), label="cli")

    def progress(p: dict) -> None:
        print(f"[{p['done']}/{p['total']}] record {p['record_id']}", file=sys.stderr)

    summary = run_bakeoff(config, out, make_backend=make_backend, fetch=fetch if config.fetch_top else None,
                          cache=cache, progress=progress, records=records)
    print(markdown_table(summary))
    print(f"\nwritten to {out}", file=sys.stderr)
    print(json.dumps({"out": str(out), "status": summary["status"]}), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
