"""Automatic replay safety and benchmark exports; offline, no model/network."""
import json
import subprocess
from pathlib import Path

import pytest

from src import binding_replay as R, diagnostics as D
from src.storage.run_log import RunLog, read_events
from src.ui import diagnostics_view
from test_binding_v3 import replay_fixture


def finished(tmp_path):
    folder, cache, _ = replay_fixture(tmp_path)
    with (folder / 'events.jsonl').open('a') as out:
        out.write(json.dumps({'kind': 'run_finished', 'status': 'completed', 'seq': 999}) + '\n')
    (folder / 'result.json').write_text(json.dumps({'status': 'completed', 'output': {}}))
    return folder, cache


def test_run_end_hook_writes_and_logs_without_touching_result(tmp_path):
    folder, cache = finished(tmp_path)
    result = (folder / 'result.json').read_bytes()
    log = RunLog(folder.parent.parent, folder.parent.name, folder.name)
    R.replay_after_result(log, cache)
    assert R.load_replay(folder)
    event = next(e for e in read_events(log.events_path) if e['kind'] == 'binding_replay_written')
    assert event['fields_ok_recorded'] == 0 and event['fields_ok_now'] == 1
    assert event['gap_counts']
    assert (folder / 'result.json').read_bytes() == result


def test_run_end_failure_is_logged_without_touching_result(tmp_path, monkeypatch):
    folder, cache = finished(tmp_path)
    result = (folder / 'result.json').read_bytes()
    def fail(*args, **kwargs):
        raise RuntimeError('forced replay failure')
    monkeypatch.setattr(R, 'replay_run', fail)
    log = RunLog(folder.parent.parent, folder.parent.name, folder.name)
    R.replay_after_result(log, cache)
    event = next(e for e in read_events(log.events_path) if e['kind'] == 'binding_replay_failed')
    assert 'forced replay failure' in event['error']
    assert (folder / 'result.json').read_bytes() == result


def test_old_finished_run_is_replayed_lazily_with_histograms_and_items(tmp_path):
    folder, _ = finished(tmp_path)
    result = D.write_benchmark(tmp_path / 'runs', ['R1'], tmp_path / 'benchmark')
    assert R.load_replay(folder)
    row = result['per_vehicle'][0]
    assert row['replay_fields_ok_now'] == 1
    assert row['replay_evidence_exact_now'] >= row['replay_evidence_exact_recorded']
    assert row['replay_missing_documents'] == 1 and row['replay_error'] is None
    field = next(f for f in row['replay_fields'] if f['field'] == 'curb_weight_kg')
    assert field['state_recorded'] == 'variant_not_exact' and field['state_now'] == 'ok'
    assert field['binding_level_histogram'] and field['best_binding_level_now']
    assert 'most_common_blocking_dimension' in field
    items = [json.loads(line) for line in (tmp_path / 'benchmark' / 'binding_replay_items.jsonl').read_text().splitlines()]
    assert items and all(r['run_id'] == 'R1' and r['record_id'] == '101122' for r in items)


def test_lazy_deadline_error_is_in_benchmark_and_child_does_not_write(tmp_path, monkeypatch):
    folder, _ = finished(tmp_path)
    def timed_out(*args, **kwargs):
        assert kwargs['timeout'] == 30
        raise subprocess.TimeoutExpired(args[0], 30)
    monkeypatch.setattr(R.subprocess, 'run', timed_out)
    row = D.write_benchmark(tmp_path / 'runs')['per_vehicle'][0]
    assert 'exceeded 30 s' in row['replay_error']
    assert not (folder / R.SUMMARY_FILE).exists()


def test_lazy_stale_summary_is_recomputed(tmp_path):
    folder, cache = finished(tmp_path)
    R.replay_run(folder, cache)
    old = json.loads((folder / R.SUMMARY_FILE).read_text())
    old['code_version'] = 'stale'
    (folder / R.SUMMARY_FILE).write_text(json.dumps(old))
    assert D.load_vehicle_diagnostics(folder)['binding_replay']['code_version'] == R.code_version()


def test_binding_replay_ui_exception_is_never_silent(monkeypatch, tmp_path):
    errors = []
    monkeypatch.setattr(diagnostics_view, '_render_binding_replay',
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('synthetic UI failure')))
    monkeypatch.setattr(diagnostics_view.st, 'error', errors.append)
    diagnostics_view.render_binding_replay(tmp_path, tmp_path, key='forced')
    assert len(errors) == 1 and 'RuntimeError: synthetic UI failure' in errors[0]
