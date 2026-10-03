"""Run profiles, per-run settings, env-override reporting and the A/B series launcher (Parts D-G). No network: the
series runs on the RunManager with scripted research functions."""

import copy
import dataclasses
import json
import threading
import time
from types import SimpleNamespace

import pytest

from fixtures import corolla_tail as tail
from test_phase_contracts import EU, GATE_OFF, PhaseClient, fetch, run, say
from test_railway_runstate import fake_factory, fake_loader, scripted_research, wait_terminal

from src import diagnostics as D
from src import run_profiles as R
from src.agent import AgentConfig
from src.jobs import manager as JM
from src.jobs.manager import (ResearchRequest, RunManager, SeriesRequest, agent_config_from_saved, plan_series,
                              series_progress)
from src.runstate import model as M
from src.storage.paths import resolve_paths
from src.storage.run_log import RunLog
from src.tools import ToolConfig

STALE = {"ACQUISITION_MODE": "contract", "DOCUMENT_SWEEP_MAX_FIELDS": "30", "DOCUMENT_SWEEP_MAX_CANDIDATES": "48",
         "GLM_DOCUMENT_SWEEP_THINKING": "enabled", "GLM_DOCUMENT_SWEEP_MAX_ATTEMPTS": "3",
         "PRIMARY_RESEARCH_MAX_TURNS": "9", "GLM_RECOVERY_THINKING": "enabled"}


# --- Part E: profiles ------------------------------------------------------------------------------------------

def test_a_named_profile_ignores_stale_env_values():
    cfg = R.build_agent_config(STALE.get, {"max_steps": 20, "document_sweep_max_fields": 40}, R.BASELINE)
    assert (cfg.acquisition_mode, cfg.document_sweep_max_fields, cfg.document_sweep_max_candidates) == ("legacy", 12, 16)
    assert cfg.acquisition_document_card is False and cfg.run_profile == R.BASELINE
    assert cfg.max_steps == AgentConfig().max_steps                     # pinned to the code default as well
    # pinned to the code defaults: GLM_DOCUMENT_SWEEP_THINKING / _MAX_ATTEMPTS ignored, never thinking "disabled"
    assert cfg.phase_settings["document_sweep"] == {"reasoning_effort": "low", "max_attempts": 1}
    # recovery thinking is not a profile setting (env still applies); its effort and max attempts are pinned
    assert cfg.phase_settings["recovery"] == {"thinking": "enabled", "reasoning_effort": "low", "max_attempts": 1}
    for name, (mode, card) in {R.PRODUCTION: ("contract", False), R.TREATMENT: ("contract", False),
                               R.TREATMENT_CARD: ("contract", True)}.items():
        cfg = R.build_agent_config(STALE.get, {}, name)
        assert (cfg.acquisition_mode, cfg.acquisition_document_card, cfg.document_sweep_max_fields) == (mode, card, 12)


def test_custom_uses_env_and_advanced_settings():
    cfg = R.build_agent_config(STALE.get, {}, R.CUSTOM)
    assert (cfg.acquisition_mode, cfg.document_sweep_max_fields, cfg.document_sweep_max_candidates, cfg.max_steps) == \
        ("contract", 30, 48, 9)
    assert cfg.phase_settings["document_sweep"] == {"thinking": "enabled", "max_attempts": 3}
    assert cfg.run_profile == R.CUSTOM
    edited = R.build_agent_config(STALE.get, {"acquisition_mode": "legacy", "document_sweep_max_fields": 7}, R.CUSTOM)
    assert (edited.acquisition_mode, edited.document_sweep_max_fields) == ("legacy", 7)


def test_ui_phase_settings_merge_per_phase_and_key_over_env():
    env = {**STALE, "GLM_DOCUMENT_SWEEP_TIMEOUT_S": "100"}.get
    cfg = R.build_agent_config(env, {"phase_settings": {"document_sweep": {"thinking": None, "max_attempts": 2}}},
                               R.CUSTOM)
    assert cfg.phase_settings["recovery"] == {"thinking": "enabled"}            # another phase's env setting survives
    assert cfg.phase_settings["document_sweep"] == {"timeout_s": 100.0, "max_attempts": 2}   # "provider default"
    assert R.merge_phase_settings({"a": {"x": 1}}, {"a": {"x": None}, "b": {"y": 2}}) == {"b": {"y": 2}}


def test_the_ui_settings_object_builds_the_run_config(monkeypatch):
    from src.ui.settings_panel import UISettings

    ui = UISettings(model_id="m", finalizer_model_id="", base_url="", chat_path="", api_key="", api_key_from_ui=False,
                    search_backend="glm", search_path="", search_engine="", chat_attempts=1, search_attempts=1,
                    chat_timeout=60, workers=1, chat_limits={}, search_limit=1,
                    agent_overrides={"acquisition_mode": "contract", "acquisition_document_card": True,
                                     "phase_settings": {"document_sweep": {"thinking": None, "max_attempts": 2}}},
                    pricing={}, data_source="auto", dsn="")
    custom = ui.agent_config(STALE.get)
    assert custom.acquisition_document_card and custom.phase_settings["document_sweep"] == {"max_attempts": 2}
    named = ui.agent_config(STALE.get, R.TREATMENT)
    assert named.acquisition_document_card is False and named.phase_settings["document_sweep"]["max_attempts"] == 1


def test_production_without_env_is_exactly_the_code_defaults():
    cfg = R.build_agent_config({}.get, {}, R.PRODUCTION)
    today = dataclasses.asdict(AgentConfig(acquisition_mode="contract", sweep_mode="adjudication",
                                           final_assembly="deterministic"))   # the code defaults
    got = dataclasses.asdict(cfg)
    diff = {k for k in today if today[k] != got[k]}
    assert diff == {"phase_settings", "run_profile"}
    # the pins spell out the code defaults (PHASE_DEFAULTS): no thinking object anywhere
    assert got["phase_settings"] == {"research": {"reasoning_effort": "high"},
                                     "document_sweep": {"reasoning_effort": "low", "max_attempts": 1},
                                     "recovery": {"reasoning_effort": "low", "max_attempts": 1},
                                     "finalizer": {"reasoning_effort": "low"}}
    from src.phase_settings import for_phase
    for phase in ("research", "document_sweep", "field_recovery", "finalization"):
        pinned, default = for_phase(cfg, phase), for_phase(AgentConfig(), phase)
        assert {k: pinned[k] for k in ("reasoning_effort", "thinking", "max_attempts")} == \
            {k: default[k] for k in ("reasoning_effort", "thinking", "max_attempts")}


# --- Part G: env overrides --------------------------------------------------------------------------------------

def test_the_env_override_list_shows_only_differing_variables():
    env = {"DOCUMENT_SWEEP_MAX_FIELDS": "12", "DOCUMENT_SWEEP_MAX_CANDIDATES": "48", "ACQUISITION_DOCUMENT_CARD": "off",
           "ACQUISITION_MODE": "legacy", "GLM_DOCUMENT_SWEEP_MAX_ATTEMPTS": "1", "GLM_DOCUMENT_SWEEP_THINKING": "disabled",
           "PRIMARY_RESEARCH_MIN_BASE_SCOPED_COVERAGE": "50.0", "PRIMARY_RESEARCH_HARD_MAX_TURNS": "",
           "GLM_RECOVERY_THINKING": "enabled", "BATCH_MAX_WORKERS": "9"}
    assert [o["text"] for o in R.env_overrides(env.get)] == [
        "ACQUISITION_MODE = legacy (code default contract)",
        "DOCUMENT_SWEEP_MAX_CANDIDATES = 48 (code default 16)",
        "GLM_DOCUMENT_SWEEP_THINKING = disabled (code default provider default)"]
    assert R.env_overrides({}.get) == []
    assert [o["var"] for o in R.env_overrides({"ACQUISITION_DOCUMENT_CARD": "on"}.get)] == ["ACQUISITION_DOCUMENT_CARD"]


# --- recording: run_started, result.json, run_configuration, config_key, by_config --------------------------------

def test_profile_env_overrides_and_new_fields_are_recorded(tmp_path, monkeypatch):
    for var, _, _ in R.ENV_OVERRIDE_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DOCUMENT_SWEEP_MAX_FIELDS", "30")
    cfg = R.build_agent_config(lambda name: None, {}, R.TREATMENT)
    client = PhaseClient([fetch("a", EU), say({"done": True, "reason": "x"})])
    result, events = run(tmp_path, client, **{**dataclasses.asdict(cfg), "research_memory_enabled": False,
                                             "requested_fields": tail.FIELDS, **GATE_OFF})
    started = next(e for e in events if e["kind"] == "run_started")
    expected = [{"var": "DOCUMENT_SWEEP_MAX_FIELDS", "value": "30", "code_default": "12",
                 "text": "DOCUMENT_SWEEP_MAX_FIELDS = 30 (code default 12)"}]
    for record in (started, result):
        assert record["run_profile"] == R.TREATMENT and record["acquisition_document_card"] is False
        assert record["env_overrides"] == expected and record["research_prompt_hash"]
    assert started["agent_config"]["run_profile"] == R.TREATMENT
    assert result["effective_config"]["agent"]["acquisition_document_card"] is False
    config = D.run_configuration(events)
    assert config["run_profile"] == R.TREATMENT and config["document_card"] is False
    assert config["env_overrides"] == [expected[0]["text"]] and config["sweep_max_fields"] == 12
    assert "run_profile=benchmark_treatment" in D.config_key(config) and "document_card=False" in D.config_key(config)
    diag = D.vehicle_diagnostics(events, run_id="b", result=result)
    card = copy.deepcopy(diag)
    card["configuration"].update(run_profile=R.TREATMENT_CARD, document_card=True)
    groups = D.aggregate([diag, card, copy.deepcopy(diag)])["by_config"]
    assert sorted((g["configuration"]["run_profile"], g["vehicles"]) for g in groups.values()) == [
        (R.TREATMENT, 2), (R.TREATMENT_CARD, 1)]
    old = D.run_configuration([{"kind": "run_started", "agent_config": {}}])
    assert (old["run_profile"], old["document_card"], old["env_overrides"]) == (None, False, [])


def test_a_saved_request_round_trips_the_run_configuration(tmp_path):
    from src.benchmark import start_batch
    from src.storage.run_log import load_batch

    cfg = R.build_agent_config(STALE.get, {"phase_settings": {"finalizer": {"max_tokens": 900}}}, R.TREATMENT_CARD)
    client = SimpleNamespace(model="glm-5.3-flash", finalizer_model="", settings=SimpleNamespace(
        model="glm-5.3-flash", finalizer_model="", public=lambda: {"model": "glm-5.3-flash"}))
    start_batch(tmp_path, "b1", client=client, agent_cfg=cfg, tool_cfg=ToolConfig(), pricing={}, vehicles=[],
                level15_source="snapshot", level15_note="", selection="x", prompt_version="p")
    restored = agent_config_from_saved(json.loads(json.dumps(load_batch(tmp_path, "b1")["agent_config"])))
    assert dataclasses.asdict(restored) == dataclasses.asdict(cfg)
    assert (restored.acquisition_mode, restored.acquisition_document_card, restored.document_sweep_max_fields,
            restored.document_sweep_max_candidates, restored.run_profile) == ("contract", True, 12, 16, R.TREATMENT_CARD)
    assert restored.phase_settings["document_sweep"] == {"reasoning_effort": "low", "max_attempts": 1}
    assert restored.phase_settings["finalizer"] == {"max_tokens": 900, "reasoning_effort": "low"}


# --- Part F: the A/B series -------------------------------------------------------------------------------------

def test_series_planning_interleaves_arms():
    plan = plan_series([R.BASELINE, R.TREATMENT], 3)
    assert [p["arm"] for p in plan] == [R.BASELINE, R.TREATMENT] * 3
    assert [p["repeat"] for p in plan] == [1, 1, 2, 2, 3, 3] and [p["index"] for p in plan] == list(range(6))
    assert all(p["status"] == JM.PLANNED and p["run_id"] is None for p in plan)


def profiled_research(log_runs: list, gate: threading.Event | None = None):
    """A research function that records its run (start / end, profile) and logs a run_started with the run's config."""
    def research(vehicle, row, *, client, cache, runs_dir, batch_id, listener, cancel_event, agent_cfg, **kw):
        rid = vehicle["upstream_record_id"]
        entry = {"run_id": batch_id, "record_id": rid, "profile": agent_cfg.run_profile, "start": time.monotonic()}
        log = RunLog(runs_dir, batch_id, rid, listener=listener)
        log.event("run_started", record_id=rid, research_model="glm-5.3-flash", agent_config=dataclasses.asdict(agent_cfg),
                  acquisition_mode=agent_cfg.acquisition_mode, run_profile=agent_cfg.run_profile,
                  acquisition_document_card=agent_cfg.acquisition_document_card)
        while gate is not None and not gate.wait(0.02):
            if cancel_event.is_set():
                from src.concurrency import BatchCancelled
                log.write_result({"record_id": rid, "status": "interrupted", "output": None})
                log.event("run_finished", status="interrupted")
                entry["end"] = time.monotonic()
                log_runs.append(entry)
                raise BatchCancelled("stop")
        log.event("research_stopped", reason="model_finished", steps=1)
        result = {"record_id": rid, "status": "completed", "output": {"summary": "ok", "fields": {}}}
        log.write_result(result)
        log.event("run_finished", status="completed")
        entry["end"] = time.monotonic()
        log_runs.append(entry)
        return result
    return research


def series_manager(tmp_path, research, **kw):
    paths = resolve_paths(lambda name: str(tmp_path / "data") if name == "TRIPY_DATA_DIR" else None)
    manager = RunManager(paths, client_factory=fake_factory, level15_loader=fake_loader, research_fn=research,
                         register_atexit=False, **kw)
    manager.series_poll_s = 0.02
    return manager


def base_request(ids=("1", "2"), key=None):
    return ResearchRequest(vehicles=[{"upstream_record_id": i, "manufacturer": "M", "model": f"V{i}"} for i in ids],
                           label="M V", scope="One vehicle", settings=SimpleNamespace(model="glm-5.3-flash",
                                                                                      finalizer_model=""),
                           agent_cfg=AgentConfig(), tool_cfg=ToolConfig(), pricing={}, prompt_version="p",
                           idempotency_key=key)


def series_request(arms=(R.BASELINE, R.TREATMENT), repeats=3, ids=("1", "2")):
    return SeriesRequest(template=base_request(ids), repeats=repeats, label="A/B test",
                         arm_configs={arm: R.build_agent_config({}.get, {}, arm) for arm in arms})


def wait_series(manager, series_id, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        series = manager.get_series(series_id)
        if series and series["status"] != JM.SERIES_RUNNING and not manager.series_executing(series_id):
            return series
        time.sleep(0.02)
    raise AssertionError(f"series {series_id} did not finish")


def test_a_series_runs_strictly_one_after_another_and_writes_its_benchmark(tmp_path):
    runs: list = []
    manager = series_manager(tmp_path, profiled_research(runs))
    # an unrelated run before the series: never part of the series benchmark
    other = manager.start(base_request(ids=("9",), key="other"))
    wait_terminal(manager, other.run_id)
    started = manager.start_series(series_request())
    series = wait_series(manager, started.run_id)
    assert series["status"] == JM.SERIES_COMPLETED and len(series["run_ids"]) == 6
    assert [i["status"] for i in series["planned"]] == [M.COMPLETED] * 6
    by_run = {}
    for entry in runs:
        by_run.setdefault(entry["run_id"], []).append(entry)
    ordered = [by_run[r] for r in series["run_ids"]]
    assert [{e["profile"] for e in group} for group in ordered] == [{R.BASELINE}, {R.TREATMENT}] * 3
    for previous, current in zip(ordered, ordered[1:]):          # the next run starts only after the previous ended
        assert min(e["start"] for e in current) >= max(e["end"] for e in previous)
    records = [manager.get(r) for r in series["run_ids"]]
    assert [r.request["run_profile"] for r in records] == [R.BASELINE, R.TREATMENT] * 3
    assert [r.request["series"]["index"] for r in records] == list(range(6))
    assert all(r.target["scope"] == "Benchmark A/B" for r in records)
    # the benchmark over exactly the series' run_ids, both arms in by_config
    bench = json.loads((manager.series_dir(started.run_id) / "benchmark.json").read_text("utf-8"))
    assert {row["run_id"] for row in bench["per_vehicle"]} == set(series["run_ids"]) and len(bench["per_vehicle"]) == 12
    assert sorted(g["configuration"]["run_profile"] for g in bench["by_config"].values()) == [R.BASELINE, R.TREATMENT]
    assert (manager.series_dir(started.run_id) / D.PARSER_GAPS_FILE).is_file()
    assert series["benchmark"]["complete"] and series["benchmark"]["run_ids"] == series["run_ids"]
    progress = series_progress(series)
    assert progress["total"] == {"planned": 6, "done": 6, "running": 0, "not_run": 0}
    assert progress["arms"][R.BASELINE]["done"] == 3
    # series state lives next to the run records, never listed as a run
    assert not any(r.run_id.startswith("_") for r in manager.list_runs())


def test_ab_runs_have_cold_private_caches_and_no_cross_arm_memory(tmp_path):
    observed = []
    scripted = profiled_research([])

    def research(*args, cache, agent_cfg, **kwargs):
        marker = cache.root / "memory" / "previous-arm.txt"
        observed.append((agent_cfg.run_profile, cache.root, marker.exists(), cache.stats_snapshot()))
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(agent_cfg.run_profile, encoding="utf-8")
        return scripted(*args, cache=cache, agent_cfg=agent_cfg, **kwargs)

    manager = series_manager(tmp_path, research)
    series_id = manager.start_series(series_request(repeats=2, ids=("1",))).run_id
    series = wait_series(manager, series_id)
    assert series["status"] == JM.SERIES_COMPLETED
    assert [row[0] for row in observed] == [R.BASELINE, R.TREATMENT] * 2
    assert len({row[1] for row in observed}) == 4
    assert all(not row[2] and row[3]["document_hits"] == 0 for row in observed)
    assert not (manager.cache.root / "memory" / "previous-arm.txt").exists()
    for index, run_id in enumerate(series["run_ids"]):
        record = manager.get(run_id)
        assert manager._cache_for_series_run(record.request["series"]).root == observed[index][1]


def test_a_series_waits_for_a_vehicle_that_is_in_another_active_run(tmp_path):
    gate = threading.Event()
    runs: list = []
    manager = series_manager(tmp_path, profiled_research(runs, gate))
    blocker = manager.start(base_request(ids=("1",), key="manual"))
    started = manager.start_series(series_request(repeats=1))
    time.sleep(0.3)
    assert manager.get_series(started.run_id)["run_ids"] == []           # waiting: vehicle 1 is busy
    gate.set()
    series = wait_series(manager, started.run_id)
    assert series["status"] == JM.SERIES_COMPLETED and len(series["run_ids"]) == 2
    first_series_start = min(e["start"] for e in runs if e["run_id"] in series["run_ids"])
    assert first_series_start >= next(e["end"] for e in runs if e["run_id"] == blocker.run_id)


def test_cancel_series_cancels_the_running_run_and_every_planned_one(tmp_path):
    gate = threading.Event()
    manager = series_manager(tmp_path, profiled_research([], gate))
    started = manager.start_series(series_request())
    deadline = time.monotonic() + 10
    while not manager.get_series(started.run_id)["run_ids"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert manager.cancel_series(started.run_id)
    series = wait_series(manager, started.run_id)
    assert series["status"] == JM.SERIES_CANCELLED and len(series["run_ids"]) == 1
    assert manager.get(series["run_ids"][0]).status == M.CANCELLED
    assert [i["status"] for i in series["planned"]] == [M.CANCELLED] * 6
    assert series_progress(series)["total"]["not_run"] == 5
    assert not manager.cancel_series(started.run_id)                       # nothing left to cancel
    gate.set()


def test_a_series_survives_a_new_manager_and_is_marked_interrupted_not_resumed(tmp_path):
    gate = threading.Event()
    runs: list = []
    first = series_manager(tmp_path, profiled_research(runs, gate))
    started = first.start_series(series_request(repeats=2))
    deadline = time.monotonic() + 10
    while not first.get_series(started.run_id)["run_ids"] and time.monotonic() < deadline:
        time.sleep(0.02)
    running_run = first.get_series(started.run_id)["run_ids"][0]
    # the process dies: a new manager (new boot id) starts on the same volume
    second = series_manager(tmp_path, profiled_research([]))
    series = second.get_series(started.run_id)
    assert series is not None and series["status"] == JM.SERIES_INTERRUPTED            # survives, not resumed
    assert series["planned"][0]["run_id"] == running_run and series["planned"][0]["status"] == M.INTERRUPTED
    assert [i["status"] for i in series["planned"][1:]] == [JM.NOT_STARTED] * 3
    assert "not resumed" in series["error"] and series["benchmark"]["complete"] is False
    assert second.get(running_run).status == M.INTERRUPTED
    assert second.list_runs() and len([r for r in second.list_runs() if not r.legacy]) == 1   # nothing new started
    assert [s["series_id"] for s in second.list_series()] == [started.run_id]
    first._shutting_down = True                                                      # stop the old "process"
    gate.set()


def test_a_series_needs_vehicles_and_arms_and_runs_one_at_a_time(tmp_path):
    gate = threading.Event()
    manager = series_manager(tmp_path, profiled_research([], gate))
    with pytest.raises(JM.RunRejected):
        manager.start_series(SeriesRequest(template=base_request(ids=()), arm_configs={R.BASELINE: AgentConfig()}))
    with pytest.raises(JM.RunRejected):
        manager.start_series(SeriesRequest(template=base_request(), arm_configs={}))
    started = manager.start_series(series_request(repeats=1))
    with pytest.raises(JM.RunRejected):
        manager.start_series(series_request(repeats=1))
    manager.cancel_series(started.run_id)
    gate.set()
    wait_series(manager, started.run_id)
