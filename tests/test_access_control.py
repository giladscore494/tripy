"""Production access control (TRIPY_ACCESS_TOKEN): fail closed, constant-time check, no secret in logs, diagnostics,
run state or the frontend, no URL or cookie authentication. The HTTP behavior (401 / 503 / Bearer) is in
tests/test_api.py and tests/test_production_serving.py."""

import io
import json
import logging
import re
from pathlib import Path

from src import access_control as AC
from src.app_config import blocking_errors, diagnostics, redact, redact_obj, validate_config

ROOT = Path(__file__).resolve().parent.parent
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
    """Nothing authenticates through query params or cookies: the token is read from the Authorization header only,
    and only the gate and the config / redaction layer ever read the secret."""
    for path in ROOT.glob("src/**/*.py"):
        text = path.read_text("utf-8")
        assert "cookies" not in text, f"{path}: cookies are never used for authentication"
        assert not re.search(r"query_params\.get\(\s*[\"'](token|access|auth)", text), path
        # only the gate and the config / redaction layer ever read the secret
        if re.search(r"(?:environ(?:\.get)?|getenv|secret|lookup|_get)\([^)]*[\"']TRIPY_ACCESS_TOKEN", text):
            assert path.name in ("access_control.py", "app_config.py"), path
