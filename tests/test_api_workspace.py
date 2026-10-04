"""The HTTP API the React workspace adds on top of PR #45: the typed per-run settings contract (run_settings is the one
source of truth, aligned with the Streamlit sidebar), A/B series over the RunManager, documents, Binding Replay and
run diagnostics over the read-only MCP implementations. No network: runs execute through the real RunManager with the
scripted research function of tests/test_api.py."""

from __future__ import annotations

import dataclasses
import json
import os
import time

import pytest

from src.api.app import create_app
from src.api.deps import env_secret
from src.jobs import manager as JM
from src.run_profiles import ARMS, BASELINE, TREATMENT, TREATMENT_CARD, code_default
from src.run_settings import (BOUNDS, RUN_OVERRIDES, pinned_by_named_profiles, run_setting_defaults,
                              settings_for_run, settings_from_env, validate_overrides)
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


def test_api_overrides_equal_the_same_edits_in_the_streamlit_sidebar(data_root, monkeypatch):
    """The same per-run edits made in the dashboard's Advanced settings and passed to settings_for_run produce the
    identical settings object (including the model-dependent price defaults and in-flight limits)."""
    from streamlit.testing.v1 import AppTest

    from src.concurrency import ConcurrencyController

    monkeypatch.setenv("GLM_FINALIZER_MODEL", "glm-5.3")

    def script():
        import dataclasses
        import os

        import streamlit as st

        from src.concurrency import ConcurrencyController
        from src.ui.settings_panel import render_settings

        def secret(name):
            return os.environ.get(name) or ""

        settings = render_settings(secret, ConcurrencyController.from_env(secret), allow_ui_key=False)
        st.session_state["settings"] = dataclasses.asdict(settings)

    app = AppTest.from_function(script, default_timeout=60)
    app.run()
    edits = {"cfg_model": ("text_input", "glm-5.1"), "cfg_workers": ("number_input", 7),
             "cfg_turns": ("slider", 12), "cfg_recovery": ("checkbox", False), "cfg_l3": ("checkbox", True),
             "cfg_maxtok": ("number_input", 4096), "cfg_effort_recovery": ("selectbox", "medium"),
             "cfg_acq_mode": ("selectbox", "legacy"), "cfg_card": ("selectbox", "on"),
             "cfg_recovery_mode": ("selectbox", "cluster"), "cfg_sweep_fields": ("number_input", 30),
             "cfg_source": ("selectbox", "snapshot"), "cfg_backend": ("selectbox", "duckduckgo"),
             "cfg_timeout": ("number_input", 600), "price_search": ("number_input", 0.02)}
    for key, (kind, value) in edits.items():
        getattr(app, kind)(key=key).set_value(value)
    app.run()
    app.checkbox(key="cfg_usetemp").check()
    app.run()
    app.slider(key="cfg_temp").set_value(0.3)
    app.run()
    assert not app.exception, app.exception
    overrides = {"model_id": "glm-5.1", "workers": 7, "max_steps": 12, "recovery_on": False, "include_level3": True,
                 "max_tokens": 4096, "effort_recovery": "medium", "acquisition_mode": "legacy", "card_choice": "on",
                 "recovery_mode": "cluster", "sweep_fields": 30, "data_source": "snapshot",
                 "search_backend": "duckduckgo", "chat_timeout": 600, "price_search": 0.02, "temperature": 0.3}
    api = dataclasses.asdict(settings_for_run(env_secret, ConcurrencyController.from_env(env_secret), overrides))
    assert app.session_state["settings"] == api
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
    {"record_ids": [VEHICLE], "repeats": 1, "arms": [BASELINE], "settings": {"workers": 2}},
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
    assert set(series["properties"]) == {"record_ids", "repeats", "arms", "idempotency_key"}
    for path in ("/api/run-settings", "/api/series", "/api/series/{series_id}", "/api/series/{series_id}/cancel",
                 "/api/runs/{run_id}/documents", "/api/documents/{doc_id}/text", "/api/documents/{doc_id}/structure",
                 "/api/runs/{run_id}/binding-replay", "/api/runs/{run_id}/diagnostics"):
        assert path in schema["paths"], path
