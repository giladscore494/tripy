"""E4: the facts API's open-data latency per key against the committed snapshots (no MILO, no network).

    python scripts/facts_latency_bench.py ROWS.json [--no-index] [--no-warmup]

ROWS.json: a list of Level 1.5 rows (public.catalog_variants_current columns, e.g. exported read-only from MILO).
Prints per key the cold time (the open part computed in a fresh process: shards decompressed on first use, the shard
index built by the warm-up first unless --no-warmup / --no-index), then a warm pass, then 3-key calls (the open part
recomputed each time, i.e. without the facts cache), each with p50 / p95 (nearest rank).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _rank(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("rows")
    parser.add_argument("--no-index", action="store_true")
    parser.add_argument("--no-warmup", action="store_true")
    args = parser.parse_args()
    if args.no_index:
        os.environ["TRIPY_SHARD_INDEX"] = "0"
    from src.facts.record import government_facts, open_data_part
    from src.open_data import shard_index

    rows = json.loads(Path(args.rows).read_text("utf-8"))
    if not args.no_index and not args.no_warmup:
        started = time.perf_counter()
        shard_index.start_warmup()
        if shard_index._STATE.get("warmup") is not None:
            shard_index._STATE["warmup"].join()
        print(f"warm-up {time.perf_counter() - started:.1f} s {shard_index.status()}")

    def timed(row: dict) -> float:
        started = time.perf_counter()
        open_data_part(row, government_facts(row)[0])
        return (time.perf_counter() - started) * 1000.0

    for label in ("cold", "warm"):
        times = []
        for row in rows:
            times.append(timed(row))
            print(f"{label} {row.get('upstream_record_id')} {row.get('shnat_yitzur')} {times[-1]:.0f} ms")
        print(f"{label}: p50 {_rank(times, .5):.0f} ms, p95 {_rank(times, .95):.0f} ms, n {len(times)}")
    calls = [sum(timed(row) for row in rows[i:i + 3]) for i in range(max(1, len(rows) - 2))]
    print(f"3-key calls (open part recomputed): p50 {_rank(calls, .5):.0f} ms, p95 {_rank(calls, .95):.0f} ms")


if __name__ == "__main__":
    main()
