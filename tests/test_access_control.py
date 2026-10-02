"""Production access control (TRIPY_ACCESS_TOKEN): fail closed, minimal lock screen, constant-time check,
no secret in session state, logs, diagnostics or the page, no URL authentication."""

import io
import json
import logging
import re
from pathlib import Path

import pytest

from src import access_control as AC
from src.app_config import blocking_errors, diagnostics, redact, redact_obj, validate_config

ROOT = Path(__file__).resolve().parent.parent
APP = str(ROOT / "app.py")
TOKEN = "op-Kx9!QzT4-wV2pLm8sR3uY7aN5bE1cD6"


def env_of(mapping):
    return lambda name: mapping.get(name)


PROD = {"TRIPY_ENV": "production", "TRIPY_ACCESS_TOKEN": TOKEN}


# --- core logic ---------------------------------------------------------------------------------------------------

def test_access_is_required_in_production_only():
    assert AC.access_required(env_of({"TRIPY_ENV": "production"}))
    assert AC.access_required(env_of({"RAILWAY_ENVIRONMENT": "production"}))
    assert not AC.access_required(env_of({}))
    assert not AC.access_required(env_of({"TRIPY_ACCESS_TOKEN": TOKEN}))        # production-only enforcement
    assert not AC.token_configured(env_of({"TRIPY_ACCESS_TOKEN": "   "}))


def test_verification_is_constant_time_and_exact(monkeypatch):
    calls = []
    real = AC.hmac.compare_digest
    monkeypatch.setattr(AC.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    assert AC.verify_token(TOKEN, TOKEN) and calls
    assert not AC.verify_token(TOKEN + " ", TOKEN)                            # opaque: no trimming
    assert not AC.verify_token(TOKEN.upper(), TOKEN)
    assert not AC.verify_token("", TOKEN) and not AC.verify_token(None, TOKEN)
    assert not AC.verify_token(TOKEN, "")                                     # nothing configured: never open
    source = Path(AC.__file__).read_text()
    assert "compare_digest" in source and not re.search(r"submitted\s*==\s*expected|expected\s*==\s*submitted", source)


def test_attempt_stores_only_the_flag_and_a_generic_message():
    state = {}
    assert not AC.attempt("wrong-token", env_of(PROD), state)
    assert state[AC.MESSAGE_KEY] == "Invalid access token." and not AC.is_authenticated(state)
    assert TOKEN not in json.dumps(state) and "wrong-token" not in json.dumps(state)
    assert AC.attempt(TOKEN, env_of(PROD), state)
    assert state == {AC.SESSION_FLAG: True}                                   # never the secret itself


def test_backoff_after_repeated_failures_without_sleeping():
    clock = {"t": 1000.0}
    state = {}
    for _ in range(AC.MAX_FAILURES):
        assert not AC.attempt("nope", env_of(PROD), state, now=lambda: clock["t"])
    assert not AC.attempt(TOKEN, env_of(PROD), state, now=lambda: clock["t"])  # locked: even the right token waits
    assert state[AC.MESSAGE_KEY].startswith("Too many attempts") and not AC.is_authenticated(state)
    clock["t"] += AC.LOCKOUT_S + 1
    assert AC.attempt(TOKEN, env_of(PROD), state, now=lambda: clock["t"])


def test_logout_only_clears_the_session_flag():
    state = {AC.SESSION_FLAG: True, "submit_nonce": "n", "scope": "One vehicle"}
    AC.logout(state)
    assert state == {"submit_nonce": "n", "scope": "One vehicle"}


# --- configuration validation, diagnostics, redaction ----------------------------------------------------------------

def test_validation_blocks_production_without_a_token(tmp_path):
    base = {"TRIPY_DATA_DIR": str(tmp_path), "GLM_API_KEY": "k" * 20, "GLM_MODEL": "glm-5.3-flash"}
    prod = validate_config(env_of({**base, "TRIPY_ENV": "production"}))
    access = next(c for c in prod if c.name == "Access control")
    assert access.level == "error" and access in blocking_errors(prod)
    assert "TRIPY_ACCESS_TOKEN" in access.detail
    dev = validate_config(env_of(base))
    assert not [c for c in dev if c.name == "Access control"] and not blocking_errors(dev)
    ok = validate_config(env_of({**base, **PROD}))
    assert next(c for c in ok if c.name == "Access control").level == "ok" and not blocking_errors(ok)
    weak = validate_config(env_of({**base, "TRIPY_ENV": "production", "TRIPY_ACCESS_TOKEN": "short"}))
    assert next(c for c in weak if c.name == "Access control").level == "warning"
    assert TOKEN not in json.dumps([c.as_dict() for c in ok])


def test_startup_diagnostics_report_presence_only(tmp_path):
    info = diagnostics(env_of({**PROD, "TRIPY_DATA_DIR": str(tmp_path)}))
    assert info["tripy_access_token_present"] is True and TOKEN not in json.dumps(info)


def test_the_token_is_redacted_everywhere(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIPY_ACCESS_TOKEN", TOKEN)
    # exception text and plain error messages
    try:
        raise RuntimeError(f"boom while handling {TOKEN}")
    except RuntimeError as exc:
        assert TOKEN not in redact(str(exc)) and TOKEN not in redact(repr(exc))
    # nested payloads (technical details, run state errors, benchmark exports)
    payload = {"error": f"x {TOKEN}", "nested": [{"value": TOKEN}], "n": 1}
    assert TOKEN not in json.dumps(redact_obj(payload))
    # server logs (the RedactingFilter on the tripy logger), including a traceback
    from src.server_logging import RedactingFilter
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger("tripy.test_access_control")
    logger.addHandler(handler)
    try:
        logger.warning("token %s in a log line", TOKEN)
        try:
            raise ValueError(TOKEN)
        except ValueError:
            logger.exception("failure")
    finally:
        logger.removeHandler(handler)
    assert TOKEN not in stream.getvalue() and "redacted" in stream.getvalue()
    # durable run state: a research failure whose message carries the token (RunManager -> run_state.json)
    import time
    from test_railway_runstate import make_manager, request, scripted_research
    manager = make_manager(tmp_path, scripted_research(fail_with=RuntimeError(f"provider echoed {TOKEN}")))
    run_id = manager.start(request()).run_id
    deadline = time.monotonic() + 10
    while (manager.is_executing(run_id) or not manager.get(run_id).terminal) and time.monotonic() < deadline:
        time.sleep(0.02)
    for path in (manager.runs_dir / run_id).rglob("*.json*"):
        assert TOKEN not in path.read_text(), path
    # failure cards' technical details
    from src.runstate.failures import explain
    info = explain({"status": "finalization_failed", "error": f"bad {TOKEN}", "documents": ["d"], "output": None})
    assert TOKEN not in info["technical"]


def test_no_authentication_through_the_url():
    """Only `?run=` (a run id) is ever read from the URL; nothing authenticates through query params or cookies."""
    sources = [ROOT / "app.py", *ROOT.glob("src/**/*.py")]
    for path in sources:
        text = path.read_text("utf-8")
        for match in re.finditer(r"query_params(?:\.get\(|\[)\s*[\"']([^\"']+)[\"']", text):
            assert match.group(1) == "run", f"{path}: query param {match.group(1)!r}"
        assert "cookies" not in text, f"{path}: cookies are never used for authentication"
        # only the gate and the config / redaction layer ever read the secret
        if re.search(r"(?:environ(?:\.get)?|getenv|secret|lookup|_get)\([^)]*[\"']TRIPY_ACCESS_TOKEN", text):
            assert path.name in ("access_control.py", "app_config.py"), path


# --- the Streamlit app ---------------------------------------------------------------------------------------------

@pytest.fixture
def prod_env(tmp_path, monkeypatch):
    for name in ("GLM_API_KEY", "GLM_MODEL", "MILO_RUNS_DIR", "MILO_CACHE_DIR", "RAILWAY_ENVIRONMENT",
                 "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID", "TRIPY_ACCESS_TOKEN", "TRIPY_ALLOW_UI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TRIPY_ENV", "production")
    monkeypatch.setenv("TRIPY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GLM_API_KEY", "sk-test-glm-key-000000000")
    monkeypatch.setenv("GLM_MODEL", "glm-5.3-flash")
    return tmp_path / "data" / "runs"


def app_test():
    from streamlit.testing.v1 import AppTest
    return AppTest.from_file(APP, default_timeout=60)


def page_text(app) -> str:
    parts = [m.value for m in app.markdown] + [e.value for e in app.error] + [c.value for c in app.caption]
    parts += [w.value for w in app.warning] + [i.value for i in app.info] + [b.label for b in app.button]
    parts += [t.label for t in app.text_input]
    return "\n".join(str(p) for p in parts)


OPERATIONAL = ("Start research", "Stop run", "Retry finalization", "Finalize from preserved research",
               "Restart research", "Logout")


def assert_locked(app):
    labels = [b.label for b in app.button]
    assert not any(label in OPERATIONAL for label in labels)
    text = page_text(app)
    for absent in ("Run history", "Research target", "Advanced settings", "Technical details", "System"):
        assert absent not in text
    assert not any("API key" in t.label for t in app.text_input)
    assert not app.expander and not app.selectbox and not app.radio


def make_history(runs_dir: Path):
    """A failed run on disk, so an unlocked dashboard would show history, a failure card and retry buttons."""
    from src.runstate import model as M
    from src.runstate.model import RunRecord
    from src.runstate.repository import FileRunRepository
    from src.storage.run_log import RunLog, write_batch
    run_id = "20261001T100000Z-history"
    write_batch(runs_dir, run_id, {"batch_id": run_id, "record_ids": ["101122"], "agent_config": {}})
    log = RunLog(runs_dir, run_id, "101122")
    log.event("run_started", agent_config={})
    log.event("tool_result", result={"document_id": "d1", "url": "https://x"})
    log.event("finalization_failed", error="HTTP 500", api_error={"status": 500})
    log.event("run_finished", status="finalization_failed")
    log.write_result({"record_id": "101122", "status": "finalization_failed", "output": None, "documents": ["d1"],
                      "error": "HTTP 500", "api_error": {"status": 500}})
    FileRunRepository(runs_dir).create(RunRecord(
        run_id=run_id, status=M.FAILED, target={"label": "XPeng G6", "record_ids": ["101122"],
                                                 "vehicles": [{"record_id": "101122", "label": "XPeng G6"}]},
        vehicles={"101122": {"status": M.FAILED, "engine_status": "finalization_failed"}},
        owner={"boot_id": "x"}, jobs=[{"kind": "research", "started_at": "2026-10-01T10:00:00+00:00",
                                        "finished_at": "2026-10-01T10:05:00+00:00"}]))


def test_production_without_a_token_fails_closed(prod_env):
    make_history(prod_env)
    app = app_test()
    app.run()
    assert not app.exception, app.exception
    assert "Production access control is not configured." in page_text(app)
    assert "Set TRIPY_ACCESS_TOKEN in the deployment environment." in page_text(app)
    assert_locked(app)
    assert not any(t.label == "Access token" for t in app.text_input)         # nothing to unlock with


def test_unauthenticated_production_session_sees_only_the_lock_screen(prod_env, monkeypatch):
    monkeypatch.setenv("TRIPY_ACCESS_TOKEN", TOKEN)
    make_history(prod_env)
    app = app_test()
    app.run()
    assert not app.exception, app.exception
    assert [t.label for t in app.text_input] == ["Access token"] and [b.label for b in app.button] == ["Unlock"]
    assert "Private research dashboard" in page_text(app) and "XPeng" not in page_text(app)
    assert_locked(app)


def test_a_wrong_token_keeps_the_dashboard_locked(prod_env, monkeypatch):
    monkeypatch.setenv("TRIPY_ACCESS_TOKEN", TOKEN)
    app = app_test()
    app.run()
    app.text_input(key=AC.INPUT_KEY).input(TOKEN[:-1])                         # a near miss
    app.button[0].click().run()
    assert not app.exception, app.exception
    assert [e.value for e in app.error] == ["Invalid access token."]
    assert_locked(app)
    state = app.session_state._state.filtered_state
    assert state.get(AC.SESSION_FLAG) is not True and state[AC.INPUT_KEY] == ""
    assert TOKEN not in json.dumps(state, default=str) and TOKEN[:-1] not in json.dumps(state, default=str)
    rendered = json.dumps([*page_text(app).splitlines()])
    assert TOKEN not in rendered and str(len(TOKEN)) not in [e.value for e in app.error][0]


def test_the_correct_token_unlocks_the_dashboard_without_storing_it(prod_env, monkeypatch):
    monkeypatch.setenv("TRIPY_ACCESS_TOKEN", TOKEN)
    make_history(prod_env)
    app = app_test()
    app.run()
    app.text_input(key=AC.INPUT_KEY).input(TOKEN)
    app.button[0].click().run()
    assert not app.exception, app.exception
    labels = [b.label for b in app.button]
    assert "Start research" in labels and "Logout" in labels and "Retry finalization" in labels
    assert "Run history" in page_text(app)
    state = app.session_state._state.filtered_state
    assert state[AC.SESSION_FLAG] is True and state[AC.INPUT_KEY] == ""
    assert TOKEN not in json.dumps(state, default=str)
    assert not any("API key" in t.label for t in app.text_input)               # production: no key field


def test_logout_returns_to_the_lock_screen(prod_env, monkeypatch):
    monkeypatch.setenv("TRIPY_ACCESS_TOKEN", TOKEN)
    app = app_test()
    app.session_state[AC.SESSION_FLAG] = True
    app.run()
    assert "Logout" in [b.label for b in app.button]
    next(b for b in app.button if b.label == "Logout").click().run()
    assert not app.exception, app.exception
    assert [b.label for b in app.button] == ["Unlock"]
    assert_locked(app)


def test_development_is_unchanged(tmp_path, monkeypatch):
    for name in ("TRIPY_ENV", "RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID", "TRIPY_ACCESS_TOKEN",
                 "GLM_API_KEY", "TRIPY_ALLOW_UI_API_KEY", "MILO_RUNS_DIR", "MILO_CACHE_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TRIPY_DATA_DIR", str(tmp_path / "data"))
    app = app_test()
    app.run()
    assert not app.exception, app.exception
    labels = [b.label for b in app.button]
    assert "Start research" in labels and "Logout" not in labels and "Unlock" not in labels
    assert any("API key (development only)" in t.label for t in app.text_input)


def test_an_unauthenticated_browser_cannot_start_research(prod_env, monkeypatch):
    """Security regression: with GLM fully configured, a locked session never reaches the run manager."""
    import src.jobs.manager as jobs
    monkeypatch.setenv("TRIPY_ACCESS_TOKEN", TOKEN)
    make_history(prod_env)
    starts, managers = [], []
    monkeypatch.setattr(jobs.RunManager, "start", lambda self, request: starts.append(request))
    monkeypatch.setattr(jobs.RunManager, "retry_finalization", lambda self, *a, **k: starts.append(a))
    real_get = jobs.get_manager
    monkeypatch.setattr(jobs, "get_manager", lambda *a, **k: managers.append(1) or real_get(*a, **k))
    app = app_test()
    app.run()
    for value in ("", "guess-1", TOKEN.lower()):                              # several failed unlocks
        app.text_input(key=AC.INPUT_KEY).input(value)
        app.button[0].click().run()
    assert not app.exception, app.exception
    assert_locked(app)
    assert starts == [] and managers == []        # no run, no retry, not even a RunManager / run-history read
