"""Downloadable exports, framework-neutral (no Streamlit, no pandas): the dashboard's download buttons and the HTTP
API serve the same bytes from the same functions.

    candidates.csv     one vehicle run's candidate table (runstate.live_state.candidate_table_rows, Hebrew headers)
    per_vehicle.csv    diagnostics.per_vehicle_csv over the runs' vehicle diagnostics (the benchmark export)
    benchmark.json     diagnostics.aggregate over the same diagnostics

`rows_to_csv` writes exactly what `pandas.DataFrame(rows).to_csv(index=False)` wrote before (csv.writer, minimal
quoting, the platform line separator, None as an empty cell), so the candidates.csv the dashboard offers is
byte-identical to the previous one.
"""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
from typing import Iterable

from .storage import trace
from .storage.run_log import read_events


def rows_to_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    columns = list(dict.fromkeys(key for row in rows for key in row))
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator=os.linesep)
    writer.writerow(columns)
    for row in rows:
        writer.writerow(["" if row.get(c) is None else row.get(c) for c in columns])
    return buf.getvalue()


def candidate_rows(events: list[dict]) -> list[dict]:
    """The candidate table of one vehicle run, from its events (empty when the run recorded no field specs)."""
    from .runstate.live_state import candidate_table_rows

    started = trace.first_event(events, "run_started") or {}
    specs = started.get("requested_field_specs") or []
    return candidate_table_rows(events, specs, started.get("vehicle_label")) if specs else []


def candidates_csv(rows: list[dict]) -> str:
    return rows_to_csv(rows)


def vehicle_events(runs_dir: Path | str, run_id: str, record_id: str) -> list[dict]:
    return read_events(Path(runs_dir) / run_id / str(record_id) / "events.jsonl")


def run_diagnostics(runs_dir: Path | str, run_ids: Iterable[str]) -> list[dict]:
    """Every vehicle diagnostics of the runs (diagnostics.json when complete, else built from events), as the
    dashboard's Benchmark diagnostics export reads them."""
    from . import diagnostics as diag_mod

    return [d for d in (diag_mod.diagnostics_from_dir(p) for p in diag_mod.vehicle_dirs(runs_dir, list(run_ids))) if d]


def benchmark_export(diags: list[dict]) -> dict:
    from . import diagnostics as diag_mod

    return diag_mod.aggregate(diags)


def benchmark_json(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, indent=1, default=str)


def per_vehicle_csv(result: dict) -> str:
    from . import diagnostics as diag_mod

    return diag_mod.per_vehicle_csv(result["per_vehicle"])
