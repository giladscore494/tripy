"""The HTTP API the React workspace adds on top of PR #45: the typed per-run settings contract (run_settings is the one
source of truth, held to the former dashboard sidebar's defaults), A/B series over the RunManager, documents, Binding Replay and
run diagnostics over the read-only MCP implementations. No network: runs execute through the real RunManager with the
scripted research function of tests/test_api.py."""

from __future__ import annotations

import dataclasses
import json
import re
import os
import time

import pytest

from src.api.app import create_app
from src.api.deps import env_secret
from src.jobs import manager as JM
from src.run_profiles import ARMS, BASELINE, TREATMENT, TREATMENT_CARD, build_agent_config, code_default
from src.run_settings import (BOUNDS, RUN_OVERRIDES, pinned_by_named_profiles, run_setting_defaults,
                              build_research_request, settings_for_run, settings_from_env, validate_overrides)
from test_api import (ACCESS, LEAKS, OTHER, RECORD, RUN, SECRETS, VEHICLE, client, ctx, data_root, gate,  # noqa: F401
                      make_context, scripted_research, wait_terminal)

DOC = "d_c21f8ddbe51c5f2b"


def wait_series(manager, series_id, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        series = manager.get_series(series_id)
        if series and series["status"] != JM.SERIES_RUNNING and not manager.series_executing(series_id):
            return series
        time.sleep(0.02)
    raise AssertionError(f"series {series_id} did not finish")


@pytest.fixture
def capture(ctx, monkeypatch):
    """The ResearchRequests the API hands to RunManager.start (the real start still runs)."""
    seen = []
    real = ctx.manager.start

    def start(request):
        seen.append(request)
        return real(request)

    monkeypatch.setattr(ctx.manager, "start", start)
    return seen


# --- the per-run settings contract -----------------------------------------------------------------------------------

def test_run_settings_contract_lists_the_allowlist_with_the_sidebars_defaults(client):
    contract = client.get("/api/run-settings").json()
    by_name = {s["name"]: s for s in contract["settings"]}
    assert list(by_name) == [o.name for o in RUN_OVERRIDES]
    defaults = run_setting_defaults(env_secret)
    assert {n: s["default"] for n, s in by_name.items()} == defaults
    assert by_name["workers"]["min"] == BOUNDS["workers"][0] and by_name["workers"]["max"] == BOUNDS["workers"][1]
    assert by_name["acquisition_mode"]["options"] == ["contract", "legacy"]
    assert by_name["effort_research"]["options"][0] == "provider default"
    assert by_name["temperature"]["nullable"] and by_name["finalizer_model_id"]["allow_empty"]
    assert {n for n, s in by_name.items() if s["pinned_by_named_profile"]} == pinned_by_named_profiles()
    assert {"max_steps", "acquisition_mode", "effort_finalizer", "sweep_fields"} <= pinned_by_named_profiles()
    assert "workers" not in pinned_by_named_profiles() and "model_id" not in pinned_by_named_profiles()
    # never settable from a client
    controlled = {s["name"] for s in contract["server_controlled"]}
    assert controlled == {"base_url", "chat_path", "search_path", "api_key", "dsn", "extra_raw", "chat_limits",
                          "search_limit"}
    assert not controlled & set(by_name)
    assert contract["concurrency"]["research_model"]["model"] == "glm-5.3-flash"
    assert contract["concurrency"]["search"]["provider_limit"] == BOUNDS["search_limit"][1]
    text = json.dumps(contract)
    assert not any(leak in text for leak in LEAKS)


def test_settings_for_run_without_overrides_is_the_untouched_sidebar(data_root):
    from src.concurrency import ConcurrencyController

    controller = ConcurrencyController.from_env(env_secret)
    assert dataclasses.asdict(settings_for_run(env_secret, controller, None)) == \
        dataclasses.asdict(settings_from_env(env_secret, controller))
    assert dataclasses.asdict(settings_for_run(env_secret, controller, {})) == \
        dataclasses.asdict(settings_from_env(env_secret, controller))


def test_api_overrides_equal_the_same_edits_in_the_sidebar(data_root, monkeypatch):
    """The same per-run edits applied to the former sidebar's defaults (test_api.sidebar_settings) and passed to
    settings_for_run produce the identical settings object (including the model-dependent price defaults and
    in-flight limits)."""
    from src.concurrency import ConcurrencyController
    from test_api import sidebar_settings

    monkeypatch.setenv("GLM_FINALIZER_MODEL", "glm-5.3")
    overrides = {"model_id": "glm-5.1", "workers": 7, "max_steps": 12, "recovery_on": False, "include_level3": True,
                 "max_tokens": 4096, "effort_recovery": "medium", "acquisition_mode": "legacy", "card_choice": "on",
                 "recovery_mode": "cluster", "sweep_fields": 30, "data_source": "snapshot",
                 "search_backend": "duckduckgo", "chat_timeout": 600, "price_search": 0.02, "temperature": 0.3}
    controller = ConcurrencyController.from_env(env_secret)
    api = dataclasses.asdict(settings_for_run(env_secret, controller, overrides))
    assert api == dataclasses.asdict(sidebar_settings(env_secret, controller, **overrides))
    assert api["model_id"] == "glm-5.1" and set(api["chat_limits"]) == {"glm-5.1", "glm-5.3"}
    assert api["pricing"]["source"] == "edited in UI" and api["agent_overrides"]["temperature"] == 0.3


@pytest.mark.parametrize("overrides,message", [
    ({"api_key": "sk-x"}, "not a per-run setting"), ({"base_url": "http://evil"}, "not a per-run setting"),
    ({"workers": 51}, "between"), ({"workers": True}, "number"), ({"workers": 2.5}, "integer"),
    ({"acquisition_mode": "fast"}, "one of"), ({"model_id": "x y"}, "valid model id"),
    ({"recovery_on": "yes"}, "true or false"), ({"temperature": 2.0}, "between")])
def test_validate_overrides_is_a_strict_allowlist(overrides, message):
    with pytest.raises(ValueError, match=message):
        validate_overrides(overrides)
    assert validate_overrides({"workers": 3.0, "temperature": None}) == {"workers": 3}


def test_a_start_with_settings_builds_the_request_through_the_shared_path(client, ctx, gate, capture):
    gate.set()
    environment, defaults = dict(os.environ), run_setting_defaults(env_secret)
    body = {"scope": "manufacturer", "manufacturer": "טויוטה", "profile": "custom", "idempotency_key": "s1",
            "settings": {"workers": 3, "max_steps": 11, "chat_attempts": 4, "include_level3": True,
                         "effort_research": "medium", "data_source": "snapshot", "price_in": 1.25}}
    response = client.post("/api/runs", json=body)
    assert response.status_code == 201, response.text
    started = response.json()
    assert started["settings_overridden"] == sorted(body["settings"])
    request = capture[-1]
    assert request.workers == 3 and request.settings.chat_max_attempts == 4 and request.data_source == "snapshot"
    assert request.agent_cfg.max_steps == 11 and request.agent_cfg.include_level3 is True
    assert request.agent_cfg.phase_settings["research"]["reasoning_effort"] == "medium"
    assert request.pricing["input_per_mtok"] == 1.25 and request.pricing["source"] == "edited in UI"
    assert ctx.manager.get(started["run_id"]).request["workers"] == 3
    wait_terminal(ctx.manager, started["run_id"])
    # per-run only: the server's environment and defaults are untouched
    assert dict(os.environ) == environment and run_setting_defaults(env_secret) == defaults
    assert client.get("/api/run-settings").json()["settings"][0]["default"] == "glm-5.3-flash"


def test_a_named_profile_still_pins_its_settings_over_overrides(client, ctx, gate, capture):
    gate.set()
    response = client.post("/api/runs", json={"scope": "one", "record_id": VEHICLE, "profile": "production",
                                              "settings": {"max_steps": 11, "acquisition_mode": "legacy",
                                                           "workers": 2}})
    assert response.status_code == 201, response.text
    cfg = capture[-1].agent_cfg
    assert cfg.max_steps == code_default("max_steps") and cfg.acquisition_mode == "contract"
    wait_terminal(ctx.manager, response.json()["run_id"])


@pytest.mark.parametrize("settings", [{"api_key": "sk-1"}, {"base_url": "http://x"}, {"dsn": "postgres://x"},
                                      {"extra_raw": "{}"}, {"chat_limits": {"m": 1}}, {"search_limit": 2},
                                      {"workers": 0}, {"workers": "5"}, {"acquisition_mode": "x"},
                                      {"temperature": 9}, {"recovery_on": 1}])
def test_unsafe_or_invalid_settings_are_rejected(client, ctx, settings):
    response = client.post("/api/runs", json={"scope": "one", "record_id": VEHICLE, "settings": settings})
    assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_request"
    assert not ctx.manager.active_runs()
    for value in settings.values():
        assert str(value) not in response.json()["error"]["message"]


def test_settings_errors_from_run_settings_are_mapped(client, ctx, monkeypatch):
    from src.api.routes import runs as runs_route

    def refuse(*_a, **_k):
        raise ValueError("model_id is not a valid model id")

    monkeypatch.setattr(runs_route, "settings_for_run", refuse)
    response = client.post("/api/runs", json={"scope": "one", "record_id": VEHICLE, "settings": {"workers": 2}})
    assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_settings"


def test_overridden_model_settings_gate_the_start(client, ctx, monkeypatch):
    """The configuration checks use the run's settings (search backend override over the environment)."""
    monkeypatch.delenv("GLM_API_KEY")
    response = client.post("/api/runs", json={"scope": "one", "record_id": VEHICLE,
                                              "settings": {"search_backend": "duckduckgo"}})
    assert response.status_code == 503 and response.json()["error"]["code"] == "configuration_incomplete"


# --- A/B series ------------------------------------------------------------------------------------------------------

def test_a_series_runs_the_arms_interleaved_one_after_another(client, ctx, gate):
    gate.set()
    ctx.manager.series_poll_s = 0.02
    response = client.post("/api/series", json={"record_ids": [VEHICLE], "repeats": 2,
                                                "arms": [TREATMENT, BASELINE], "idempotency_key": "ab-1"})
    assert response.status_code == 201, response.text
    started = response.json()
    sid = started["series_id"]
    assert started["created"] and started["series"]["arms"] == [BASELINE, TREATMENT]       # ARMS order
    assert started["series"]["label"] == "A/B · 1 vehicle(s)"
    assert [(i["repeat"], i["arm"]) for i in started["series"]["planned"]] == [
        (1, BASELINE), (1, TREATMENT), (2, BASELINE), (2, TREATMENT)]
    final = wait_series(ctx.manager, sid)
    assert final["status"] == JM.SERIES_COMPLETED
    state = client.get(f"/api/series/{sid}").json()
    assert state["status"] == "COMPLETED" and len(state["run_ids"]) == 4 and not state["executing"]
    assert state["series_progress"]["total"] == {"planned": 4, "done": 4, "running": 0, "not_run": 0}
    assert state["arm_labels"][BASELINE] == "Benchmark: Baseline"
    assert state["vehicles"] == [{"record_id": VEHICLE, "label": ctx.catalog.title(VEHICLE)}]
    assert "dir" not in state["benchmark"] and "benchmark.json" in state["benchmark"]["files"]
    assert "owner" not in state
    records = [ctx.manager.get(r) for r in state["run_ids"]]
    assert [r.request["run_profile"] for r in records] == [BASELINE, TREATMENT] * 2
    assert all(r.target["scope"] == "Benchmark A/B" for r in records)
    export = client.get(f"/api/series/{sid}/export/benchmark.json")
    assert export.status_code == 200 and {r["run_id"] for r in export.json()["per_vehicle"]} == set(state["run_ids"])
    assert client.get(f"/api/series/{sid}/export/series.json").status_code == 404
    listing = client.get("/api/series").json()
    assert listing["total"] == 1 and listing["executing"] is None and listing["series"][0]["series_id"] == sid
    assert [a["id"] for a in listing["arms"]] == list(ARMS) and listing["default_arms"] == [BASELINE, TREATMENT]


def test_series_idempotency_one_at_a_time_and_cancellation(client, ctx, gate):
    ctx.manager.series_poll_s = 0.02
    body = {"record_ids": [VEHICLE, OTHER], "repeats": 3, "arms": [BASELINE, TREATMENT_CARD],
            "idempotency_key": "ab-2"}
    first = client.post("/api/series", json=body)
    assert first.status_code == 201
    sid = first.json()["series_id"]
    again = client.post("/api/series", json=body)
    assert again.status_code == 200 and again.json()["series_id"] == sid and not again.json()["created"]
    other = client.post("/api/series", json={**body, "idempotency_key": "ab-3"})
    assert other.status_code == 409 and other.json()["error"]["existing_series_id"] == sid
    assert client.get("/api/series").json()["executing"] == sid
    deadline = time.monotonic() + 10
    while not (client.get(f"/api/series/{sid}").json()["series_progress"]["total"]["running"]) and \
            time.monotonic() < deadline:
        time.sleep(0.02)
    cancelled = client.post(f"/api/series/{sid}/cancel")
    assert cancelled.status_code == 202
    final = wait_series(ctx.manager, sid)
    assert final["status"] == JM.SERIES_CANCELLED
    state = client.get(f"/api/series/{sid}").json()
    assert state["series_progress"]["total"]["not_run"] == 5 and state["planned"][0]["status"] == "CANCELLED"
    assert client.post(f"/api/series/{sid}/cancel").json()["error"]["code"] == "not_executing"


@pytest.mark.parametrize("body", [
    {"record_ids": [], "repeats": 1, "arms": [BASELINE]}, {"record_ids": [VEHICLE], "repeats": 0, "arms": [BASELINE]},
    {"record_ids": [VEHICLE], "repeats": 6, "arms": [BASELINE]}, {"record_ids": [VEHICLE], "repeats": 1, "arms": []},
    {"record_ids": [VEHICLE], "repeats": 1, "arms": ["production"]},
    {"record_ids": [VEHICLE], "repeats": "2", "arms": [BASELINE]},
    {"record_ids": [VEHICLE], "repeats": 1, "arms": [BASELINE], "settings": {"api_key": "sk-x"}},
    {"record_ids": [VEHICLE], "repeats": 1, "arms": [BASELINE], "settings": {"base_url": "http://evil"}},
    {"record_ids": [VEHICLE], "repeats": 1, "arms": [BASELINE], "arm_configs": {BASELINE: {}}}])
def test_invalid_series_requests_are_422(client, ctx, body):
    response = client.post("/api/series", json=body)
    assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_request"
    assert not ctx.manager.list_series()


def test_unknown_series_vehicles_and_ids(client, ctx):
    response = client.post("/api/series", json={"record_ids": ["nope"], "repeats": 1, "arms": [BASELINE]})
    assert response.status_code == 422 and response.json()["error"]["code"] == "unknown_vehicle"
    for bad in ("series-nope", ".hidden", "..", "a%2Fb"):
        assert client.get(f"/api/series/{bad}").status_code == 404, bad
        assert client.post(f"/api/series/{bad}/cancel").status_code in (404, 405), bad
    assert set(ARMS) == {BASELINE, TREATMENT, TREATMENT_CARD}


# --- documents, binding replay, diagnostics --------------------------------------------------------------------------

def test_documents_of_a_vehicle_run(client):
    page = client.get(f"/api/runs/{RUN}/documents").json()
    assert page["record_id"] == RECORD and page["paging"]["total"] == len(page["documents"]) >= 3
    doc = next(d for d in page["documents"] if d["doc_id"] == DOC)
    assert doc["url"] == "https://www.heyxpeng.co.il/g6" and doc["market"] == "IL" and doc["status"] == 200
    assert doc["doc_type"] == "html" and doc["size_bytes"] and doc["text_chars"] and doc["fetched_at"]
    assert "wheelbase_mm" in doc["used_for_fields"] and "source_authority" in doc
    assert client.get(f"/api/runs/{RUN}/documents?limit=1").json()["paging"]["next_offset"] == 1
    assert client.get(f"/api/runs/{RUN}/documents?record_id=nope").status_code == 404
    assert client.get("/api/runs/nope/documents").status_code == 404


def test_document_text_pages_and_structure(client):
    first = client.get(f"/api/documents/{DOC}/text?limit=50").json()
    rest = client.get(f"/api/documents/{DOC}/text?offset=50&limit=50000").json()
    assert first["next_offset"] == 50 and rest["next_offset"] is None
    assert first["text"] + rest["text"] == client.get(f"/api/documents/{DOC}/text").json()["text"]
    assert "אקספנג" in first["text"]
    plain = client.get(f"/api/documents/{DOC}/structure").json()
    assert plain["tables"] and plain["tables"][0]["rows"][1][0] == "טווח נסיעה (WLTP)"
    assert "cached" in plain["variant_map"]
    targeted = client.get(f"/api/documents/{DOC}/structure?run_id={RUN}").json()
    assert targeted["variant_map"]["computed_for"]["record_id"] == RECORD
    assert client.get(f"/api/documents/{DOC}/structure?record_id={RECORD}").status_code == 422


@pytest.mark.parametrize("bad,status", [("d_0000000000000000", 404), ("nope", 422), ("..", 404),
                                        ("d_c21f8ddbe51c5f2b%2F..", 404), ("D_C21F8DDBE51C5F2B", 422)])
def test_unknown_and_invalid_documents(client, bad, status):
    for suffix in ("text", "structure"):
        response = client.get(f"/api/documents/{bad}/{suffix}")
        assert response.status_code == status, (bad, suffix, response.text)
        assert "Traceback" not in response.text


def test_documents_stay_inside_the_data_tree(client, data_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "meta.json").write_text('{"url": "https://x"}', "utf-8")
    (outside / "text.txt").write_text("outside the tree", "utf-8")
    (data_root / "cache" / "documents" / "d_0123456789abcdef").symlink_to(outside, target_is_directory=True)
    response = client.get("/api/documents/d_0123456789abcdef/text")
    assert response.status_code == 404 and "outside the tree" not in response.text


def test_binding_replay_is_read_only(client, data_root):
    before = sorted(str(p.relative_to(data_root)) for p in data_root.rglob("*"))
    replay = client.get(f"/api/runs/{RUN}/binding-replay").json()
    assert replay["record_id"] == RECORD and replay["summary"]["vehicle"]["fields"] == 11
    assert replay["summary"]["vehicle"]["fields_ok_now"] >= replay["summary"]["vehicle"]["fields_ok_recorded"]
    item = replay["items"][0]
    assert {"field", "kind", "binding_level_now", "would_be_ok"} <= set(item)
    one = client.get(f"/api/runs/{RUN}/binding-replay?field=wheelbase_mm").json()
    assert one["items"] and {i["field"] for i in one["items"]} == {"wheelbase_mm"}
    assert set(one["summary"]["fields"]) == {"wheelbase_mm"}
    assert client.get(f"/api/runs/{RUN}/binding-replay?limit=2").json()["paging"]["returned"] == 2
    after = sorted(str(p.relative_to(data_root)) for p in data_root.rglob("*"))
    assert before == after and not list(data_root.rglob("binding_replay*"))
    assert client.get(f"/api/runs/{RUN}/binding-replay?record_id=nope").status_code == 404


def test_binding_replay_failures_never_leak_a_traceback(client, monkeypatch):
    from src.mcp_server import tools

    def boom(*_a, **_k):
        raise RuntimeError("Traceback (most recent call last): secret path /data/x")

    monkeypatch.setattr(tools.Observer, "binding_replay", boom)
    response = client.get(f"/api/runs/{RUN}/binding-replay")
    assert response.status_code == 500 and response.json()["error"]["code"] == "replay_failed"
    assert "Traceback" not in response.text and "/data/x" not in response.text


def test_run_diagnostics_are_the_benchmark_exports_rows(client, data_root):
    diag = client.get(f"/api/runs/{RUN}/diagnostics").json()
    export = client.get(f"/api/runs/{RUN}/export/benchmark.json").json()
    assert diag["vehicles"] == 1 and diag["per_vehicle"][0]["record_id"] == RECORD
    assert diag["per_vehicle"][0]["run_id"] == export["per_vehicle"][0]["run_id"]
    assert set(diag["aggregate"]) >= {"acquisition", "document_sweep"} and "per_vehicle" not in diag["aggregate"]
    legacy = client.get("/api/runs/20261001T185509Z-glm-5.3-one/diagnostics")
    assert legacy.status_code == 200


# --- auth and redaction for every new route --------------------------------------------------------------------------

NEW_GETS = ["/api/run-settings", "/api/series", "/api/series/series-x", f"/api/runs/{RUN}/documents",
            f"/api/documents/{DOC}/text", f"/api/documents/{DOC}/structure", f"/api/runs/{RUN}/binding-replay",
            f"/api/runs/{RUN}/diagnostics", "/api/series/series-x/export/benchmark.json"]
NEW_POSTS = [("/api/series", {"record_ids": [VEHICLE], "repeats": 1, "arms": [BASELINE]}),
             ("/api/series/series-x/cancel", None)]


def test_every_new_route_requires_the_bearer_token_in_production(client, ctx, monkeypatch):
    monkeypatch.setenv("TRIPY_ENV", "production")
    monkeypatch.setenv("TRIPY_ACCESS_TOKEN", ACCESS)
    for path in NEW_GETS:
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers={"Authorization": "Bearer wrong-0123456789"}).status_code == 401, path
    for path, body in NEW_POSTS:
        assert client.post(path, json=body).status_code == 401, path
    assert not ctx.manager.list_series()
    ok = {"Authorization": f"Bearer {ACCESS}"}
    assert client.get("/api/run-settings", headers=ok).status_code == 200
    assert client.get(f"/api/runs/{RUN}/documents", headers=ok).status_code == 200
    monkeypatch.delenv("TRIPY_ACCESS_TOKEN")
    response = client.get("/api/series", headers=ok)
    assert response.status_code == 503 and response.json()["error"]["code"] == "access_control_not_configured"


def test_no_secret_reaches_any_new_response(client, data_root):
    text = data_root / "cache" / "documents" / DOC / "text.txt"
    text.write_text(text.read_text("utf-8") + f"\nleaked key {SECRETS['GLM_API_KEY']} here\n", "utf-8")
    bodies = [client.get(path).text for path in NEW_GETS]
    bodies.append(client.get(f"/api/documents/{DOC}/text?limit=50000").text)
    assert "[redacted]" in bodies[-1] or "redacted" in bodies[-1]
    for body in bodies:
        assert not any(leak in body for leak in LEAKS)


def test_the_api_with_new_routes_never_imports_streamlit_or_src_ui(data_root):
    import subprocess
    import sys

    from test_api import ROOT

    code = f"""
import sys
from fastapi.testclient import TestClient
from src.api.app import create_app
with TestClient(create_app(mount_mcp=False)) as client:
    for path in {NEW_GETS[:2] + NEW_GETS[3:8]!r}:
        assert client.get(path).status_code == 200, path
assert "streamlit" not in sys.modules and "pandas" not in sys.modules, "framework imported"
assert not [m for m in sys.modules if m.startswith("src.ui")], "src.ui imported"
print("ok")
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=180,
                            env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert result.returncode == 0 and result.stdout.strip().endswith("ok"), result.stderr[-3000:]


def test_the_openapi_schema_types_the_new_contracts(data_root):
    schema = create_app(mount_mcp=False).openapi()
    overrides = schema["components"]["schemas"]["RunSettingsOverrides"]
    assert overrides["additionalProperties"] is False
    assert set(overrides["properties"]) == {o.name for o in RUN_OVERRIDES}
    series = schema["components"]["schemas"]["StartSeries"]
    assert set(series["properties"]) == {"record_ids", "repeats", "arms", "idempotency_key", "settings"}
    assert series["additionalProperties"] is False
    assert "RunSettingsOverrides" in json.dumps(series["properties"]["settings"])     # the same typed contract
    for path in ("/api/run-settings", "/api/series", "/api/series/{series_id}", "/api/series/{series_id}/cancel",
                 "/api/runs/{run_id}/documents", "/api/documents/{doc_id}/text", "/api/documents/{doc_id}/structure",
                 "/api/runs/{run_id}/binding-replay", "/api/runs/{run_id}/diagnostics"):
        assert path in schema["paths"], path


# --- A/B series: the same typed per-run settings as a run (the former A/B launcher used the Advanced settings) -------

@pytest.fixture
def capture_series(ctx, monkeypatch):
    seen = []
    real = ctx.manager.start_series

    def start_series(request):
        seen.append(request)
        return real(request)

    monkeypatch.setattr(ctx.manager, "start_series", start_series)
    return seen


def test_series_settings_become_the_template_and_each_arm_keeps_its_profile(client, ctx, gate, capture_series):
    gate.set()
    ctx.manager.series_poll_s = 0.02
    overrides = {"model_id": "glm-5.1", "workers": 3, "search_backend": "duckduckgo", "data_source": "snapshot",
                 "max_steps": 12, "price_in": 0.9}
    response = client.post("/api/series", json={"record_ids": [VEHICLE], "repeats": 1, "arms": [TREATMENT, BASELINE],
                                                "idempotency_key": "ab-settings", "settings": overrides})
    assert response.status_code == 201, response.text
    assert response.json()["settings_overridden"] == sorted(overrides)
    request = capture_series[0]
    settings = settings_for_run(env_secret, ctx.manager.controller, overrides)
    expected = JM.SeriesRequest(
        template=build_research_request(settings, env_secret, [ctx.catalog.by_id[VEHICLE]], "A/B · 1 vehicle(s)",
                                        "Benchmark A/B", None, "production"),
        arm_configs={arm: settings.agent_config(env_secret, arm) for arm in (BASELINE, TREATMENT)}, repeats=1,
        label="A/B · 1 vehicle(s)", idempotency_key="series:ab-settings")
    assert dataclasses.asdict(request) == dataclasses.asdict(expected)
    assert request.template.settings.model == "glm-5.1" and request.template.workers == 3
    assert request.template.tool_cfg.search_backend == "duckduckgo" and request.template.data_source == "snapshot"
    assert request.template.pricing["input_per_mtok"] == 0.9
    # a named arm profile still pins its experiment settings: max_steps is pinned, so the arm keeps its own value
    assert request.arm_configs[BASELINE].run_profile == BASELINE
    assert request.arm_configs[BASELINE] == build_agent_config(env_secret, settings.agent_overrides, BASELINE)
    final = wait_series(ctx.manager, response.json()["series_id"])
    assert final["status"] == JM.SERIES_COMPLETED
    records = [ctx.manager.get(r) for r in final["run_ids"]]
    assert all(r.request.get("research_model") == "glm-5.1" for r in records)


def test_series_without_settings_use_the_server_configuration(client, ctx, gate, capture_series):
    gate.set()
    ctx.manager.series_poll_s = 0.02
    response = client.post("/api/series", json={"record_ids": [VEHICLE], "repeats": 1, "arms": [BASELINE]})
    assert response.status_code == 201 and response.json()["settings_overridden"] == []
    default = settings_from_env(env_secret, ctx.manager.controller)
    assert dataclasses.asdict(capture_series[0].template.settings) == dataclasses.asdict(default.glm_settings())
    wait_series(ctx.manager, response.json()["series_id"])


@pytest.mark.parametrize("settings,message", [({"workers": 51}, "between"), ({"acquisition_mode": "turbo"}, "one of"),
                                              ({"model_id": "bad id!"}, "model id")])
def test_invalid_series_settings_are_422_and_start_nothing(client, ctx, settings, message):
    response = client.post("/api/series", json={"record_ids": [VEHICLE], "repeats": 1, "arms": [BASELINE],
                                                "settings": settings})
    assert response.status_code == 422
    assert response.json()["error"]["code"] in ("invalid_settings", "invalid_request")
    assert not ctx.manager.list_series()


# --- technical views, benchmark metrics, multi-run exports, cache, reachability -------------------------------------

def test_a_vehicles_technical_sections(client):
    tech = client.get(f"/api/runs/{RUN}/vehicles/{RECORD}/technical")
    assert tech.status_code == 200, tech.text
    body = tech.json()
    assert body["available"] and body["record_id"] == RECORD
    for key in ("notices", "raw", "partial_research", "consistency_checks", "tool_calls", "model_responses",
                "level15_input", "config", "api_attempts", "field_recovery", "candidates", "brief", "diagnostics"):
        assert key in body, key
    assert body["diagnostics"]["turns"] is not None and isinstance(body["brief"]["acquisition"], list)
    assert body["partial_research"]["target_status"].keys() == {"no_evidence", "unresolved", "level3"}
    text = json.dumps(body)
    assert not any(leak in text for leak in LEAKS)
    assert client.get(f"/api/runs/{RUN}/vehicles/nope/technical").status_code == 404
    assert client.get(f"/api/runs/nope/vehicles/{RECORD}/technical").status_code == 404


def test_live_view_and_technical_view_of_an_active_run(client, ctx, gate):
    run_id = client.post("/api/runs", json={"scope": "one", "record_id": VEHICLE}).json()["run_id"]
    tech = client.get(f"/api/runs/{run_id}/vehicles/{VEHICLE}/technical").json()
    assert tech["available"] is False and "active" in tech["reason"]
    live = client.get(f"/api/runs/{run_id}/live").json()
    assert live["active"] and live["columns"][0] == "קבוצה" and isinstance(live["lines"], list)
    assert all(len(row) == len(live["columns"]) for row in live["fields"])
    assert client.get(f"/api/runs/{run_id}/benchmark").json()["available"] is False
    gate.set()
    wait_terminal(ctx.manager, run_id)


def test_benchmark_tab_metrics(client):
    bench = client.get(f"/api/runs/{RUN}/benchmark").json()
    assert bench["available"] and bench["vehicles"] == 1 and bench["aggregate"]["vehicles"] == 1
    assert bench["columns"][0] == "vehicle" and set(bench["rows"][0]) == set(bench["columns"])
    from src.presentation.run_views import PER_VEHICLE_COLS
    assert all(c in PER_VEHICLE_COLS for c in bench["columns"])
    batches = client.get("/api/benchmark/batches").json()
    assert isinstance(batches["batches"], list)


def test_multi_run_benchmark_exports_are_the_canonical_bytes(client, ctx):
    from src import diagnostics as D
    from src import exports

    summary = client.get("/api/benchmark/summary", params=[("run_id", RUN), ("run_id", RUN)]).json()
    assert summary["run_ids"] == [RUN] and summary["vehicles"] == 1 and "benchmark.json" in summary["files"]
    diags = exports.run_diagnostics(ctx.runs_dir, [RUN])
    expected = {"benchmark.json": exports.benchmark_json(D.aggregate(diags)),
                "per_vehicle.csv": exports.per_vehicle_csv(D.aggregate(diags)),
                "binding_replay_items.jsonl": "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n"
                                                      for r in D.binding_replay_rows(diags))}
    for name, text in expected.items():
        response = client.get(f"/api/benchmark/export/{name}", params={"run_id": RUN})
        assert response.status_code == 200, name
        # benchmark.json carries generated_at (whole seconds): the response and `expected` may straddle a second
        stamp = re.compile(rb'"generated_at": "[^"]*"')
        assert stamp.sub(b"", response.content) == stamp.sub(b"", text.encode("utf-8")), name
        assert "attachment" in response.headers["content-disposition"]
    gaps = client.get("/api/benchmark/export/parser_gaps.jsonl", params={"run_id": RUN})
    assert gaps.status_code in (200, 404)
    assert client.get("/api/benchmark/export/benchmark.json").json()["error"]["code"] == "run_id_required"
    assert client.get("/api/benchmark/export/benchmark.json", params={"run_id": "nope"}).status_code == 404
    assert client.get("/api/benchmark/export/series.json", params={"run_id": RUN}).status_code == 404


def test_cache_listing_and_reachability(client, monkeypatch):
    docs = client.get("/api/cache/documents", params={"limit": 2}).json()
    assert docs["total"] >= 1 and docs["returned"] <= 2 and "document_hits" in docs["stats"]
    assert {"document_id", "url", "status"} <= set(docs["documents"][0])
    from src.api.routes import technical
    calls = []
    monkeypatch.setattr(technical, "endpoint_reachable", lambda url: calls.append(url) or (True, "HTTP 404"))
    reach = client.get("/api/config/reachability").json()
    assert reach == {"checked": True, "reachable": True, "detail": "HTTP 404"} and len(calls) == 1
    assert SECRETS["GLM_API_KEY"] not in json.dumps(reach)


def test_detail_carries_status_totals_and_history_carries_resolved_fields(client):
    detail = client.get(f"/api/runs/{RUN}").json()
    labels = [row[0] for row in detail["status_panel"]]
    assert labels[:2] == ["Run", "Status"] and "Resolved fields" in labels and "Model calls" in labels
    assert "notices" in detail["vehicles"][0]
    run = next(r for r in client.get("/api/runs").json()["runs"] if r["run_id"] == RUN)
    assert "resolved_fields_text" in run
    results = client.get(f"/api/runs/{RUN}/results").json()
    assert "output_source_caption" in results and "no_output_message" in results["vehicles"][0]
