"""The production web process: FastAPI serves the React bundle (frontend/dist) next to /api, /health and the optional
MCP, from ONE Uvicorn process started by scripts/start.sh. SPA fallback that never swallows /api, /health, /mcp or
/assets; cache headers; security headers; the bundle required in production; one RunManager per process; secrets
kept out of the access log; no Streamlit anywhere. No network: localhost only."""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import frontend as FE
from src.api.app import create_app
from test_api import ACCESS, RUN, SECRETS, client, ctx, data_root, gate  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
MCP_TOKEN = SECRETS["TRIPY_MCP_TOKEN"]
INDEX = '<!doctype html><html><head><script type="module" src="/assets/index-abc123.js"></script></head>' \
        '<body><div id="root"></div></body></html>'


@pytest.fixture
def dist(tmp_path) -> Path:
    """A minimal Vite-shaped bundle: index.html, a hashed asset, a public root file."""
    folder = tmp_path / "dist"
    (folder / "assets").mkdir(parents=True)
    (folder / "index.html").write_text(INDEX)
    (folder / "assets" / "index-abc123.js").write_text("console.log('tripy')")
    (folder / "assets" / "index-def456.css").write_text("body{}")
    (folder / "tripy.svg").write_text("<svg/>")
    return folder


@pytest.fixture
def spa(ctx, dist):
    with TestClient(create_app(context=ctx, mount_mcp=False, frontend=dist)) as test_client:
        yield test_client


# --- SPA serving and fallback ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/runs/foo", "/series/foo", "/research/new", "/diagnostics", "/settings",
                                  "/runs", "/does-not-exist", "/runs/foo?tab=technical&vehicle=1"])
def test_every_client_route_answers_the_react_index(spa, path):
    response = spa.get(path)
    assert response.status_code == 200 and response.text == INDEX
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-cache"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "script-src 'self'" in response.headers["content-security-policy"]


def test_hashed_assets_are_immutable_and_a_missing_asset_is_a_real_404(spa):
    js = spa.get("/assets/index-abc123.js")
    assert js.status_code == 200 and js.text == "console.log('tripy')"
    assert js.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "javascript" in js.headers["content-type"]
    assert spa.get("/assets/index-def456.css").headers["cache-control"] == "public, max-age=31536000, immutable"
    for path in ["/assets/nonexistent.js", "/assets", "/assets/", "/assets/%2e%2e/index.html", "/assets/sub/x.js"]:
        missing = spa.get(path)
        assert missing.status_code == 404, path
        assert missing.text != INDEX and "id=\"root\"" not in missing.text, path
    root_file = spa.get("/tripy.svg")
    assert root_file.status_code == 200 and root_file.text == "<svg/>" and "immutable" not in root_file.headers["cache-control"]


def test_api_health_and_mcp_are_never_swallowed_by_the_spa(spa):
    nope = spa.get("/api/nope")
    assert nope.status_code == 404 and nope.json()["error"]["code"] == "not_found"
    unknown_run = spa.get("/api/runs/nope")
    assert unknown_run.status_code == 404 and unknown_run.json()["error"]["code"] == "unknown_run"
    assert spa.get("/api").status_code == 404 and spa.get("/api").headers["content-type"] == "application/json"
    assert spa.get("/api/runs").status_code == 200
    health = spa.get("/health")
    assert health.status_code == 200 and health.json() == {"status": "ok", "service": "tripy"}
    for path in ["/mcp", f"/mcp/{MCP_TOKEN}", "/MCP/x", "/health/x", "/API/runs"]:      # MCP not mounted here
        response = spa.get(path)
        assert response.status_code == 404 and response.text != INDEX, path
    # only GET / HEAD are client routes; a POST to one is 405, never the index
    assert spa.post("/runs/foo").status_code == 405
    assert spa.head("/runs/foo").status_code == 200


def test_mcp_routes_win_over_the_spa_when_configured(ctx, dist, monkeypatch):
    from src.mcp_server import asgi

    monkeypatch.setattr(asgi, "_MCP", asgi._Mcp())        # the process's MCP runs once; this test builds its own
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    with TestClient(create_app(context=ctx, mount_mcp=True, frontend=dist)) as test_client:
        ping = test_client.post(f"/mcp/{MCP_TOKEN}", json=body, headers=headers)
        assert ping.status_code == 200 and ping.json()["result"] == {}
        for path in ["/mcp", f"/mcp/{MCP_TOKEN[:-1]}", f"/mcp/{MCP_TOKEN}/x", "/mcp/x"]:
            denied = test_client.post(path, json=body, headers=headers)
            assert (denied.status_code, denied.content) == (404, b""), path
            assert test_client.get(path).text != INDEX, path
        assert test_client.get("/runs/foo").text == INDEX


def test_security_headers_no_cors_and_no_caching_of_api_responses(spa):
    for path in ["/", "/api/runs", "/health", "/assets/index-abc123.js"]:
        headers = spa.get(path, headers={"Origin": "https://evil.example"}).headers
        assert headers["x-content-type-options"] == "nosniff", path
        assert headers["referrer-policy"] == "no-referrer" and headers["x-frame-options"] == "DENY", path
        assert "access-control-allow-origin" not in headers, path          # same origin: no CORS at all
    assert spa.get("/api/runs").headers["cache-control"] == "no-store"
    assert spa.get(f"/api/runs/{RUN}/export/benchmark.json").headers["cache-control"] == "no-store"
    preflight = spa.options("/api/runs", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in preflight.headers


def test_frontend_sources_are_never_served(spa):
    for path in ["/src/main.tsx", "/package.json", "/frontend/src/App.tsx", "/vite.config.ts"]:
        response = spa.get(path)
        assert response.text == INDEX, path            # the SPA shell, never a source file


# --- the bundle is required in production ----------------------------------------------------------------------------

def test_production_without_the_bundle_fails_startup_clearly(ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("TRIPY_ENV", "production")
    monkeypatch.setattr(FE, "FRONTEND_DIST", tmp_path / "missing")
    import src.api.app as app_module
    monkeypatch.setattr(app_module, "FRONTEND_DIST", tmp_path / "missing")
    with pytest.raises(FE.FrontendMissing, match="npm ci && npm run build"):
        create_app(context=ctx, mount_mcp=False)
    with pytest.raises(FE.FrontendMissing):
        create_app(context=ctx, mount_mcp=False, frontend=tmp_path / "missing")


def test_development_without_the_bundle_serves_the_api_only(ctx, tmp_path, monkeypatch):
    import src.api.app as app_module
    monkeypatch.setattr(app_module, "FRONTEND_DIST", tmp_path / "missing")
    with TestClient(create_app(context=ctx, mount_mcp=False)) as test_client:
        assert test_client.get("/health").status_code == 200 and test_client.get("/api/runs").status_code == 200
        assert test_client.get("/runs/foo").status_code == 404


def test_tripy_frontend_dist_selects_another_bundle(ctx, dist, monkeypatch):
    monkeypatch.setenv("TRIPY_FRONTEND_DIST", str(dist))
    with TestClient(create_app(context=ctx, mount_mcp=False)) as test_client:
        assert test_client.get("/series/x").text == INDEX


# --- authentication in production ------------------------------------------------------------------------------------

def test_production_auth_shell_loads_api_requires_the_bearer_token(ctx, dist, monkeypatch):
    monkeypatch.setenv("TRIPY_ENV", "production")
    monkeypatch.setenv("TRIPY_ACCESS_TOKEN", ACCESS)
    with TestClient(create_app(context=ctx, mount_mcp=False, frontend=dist)) as test_client:
        assert test_client.get("/").status_code == 200                       # the unlock screen can load
        assert test_client.get("/health").status_code == 200
        assert test_client.get("/api/config/status").status_code == 401
        bad = test_client.get("/api/config/status", headers={"Authorization": "Bearer wrong-token"})
        assert bad.status_code == 401 and ACCESS not in bad.text and "wrong-token" not in bad.text
        assert test_client.get(f"/api/config/status?token={ACCESS}").status_code == 401   # never through the URL
        ok = test_client.get("/api/config/status", headers={"Authorization": f"Bearer {ACCESS}"})
        assert ok.status_code == 200 and ACCESS not in ok.text
    monkeypatch.delenv("TRIPY_ACCESS_TOKEN")
    with TestClient(create_app(context=ctx, mount_mcp=False, frontend=dist)) as test_client:
        closed = test_client.get("/api/runs", headers={"Authorization": f"Bearer {ACCESS}"})
        assert closed.status_code == 503 and closed.json()["error"]["code"] == "access_control_not_configured"
        assert test_client.get("/health").status_code == 200


# --- one RunManager per process --------------------------------------------------------------------------------------

def test_one_run_manager_per_process_and_shutdown_on_lifespan_exit(data_root, monkeypatch):
    from src.jobs import manager as JM

    created = []
    real_init = JM.RunManager.__init__

    def counting(self, *args, **kwargs):
        created.append(1)
        kwargs["register_atexit"] = False
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(JM.RunManager, "__init__", counting)
    monkeypatch.setattr(JM, "_MANAGERS", {})
    shutdowns = []
    monkeypatch.setattr(JM.RunManager, "shutdown", lambda self, grace_s=None: shutdowns.append(self))
    app = create_app(mount_mcp=False, frontend=None)
    with TestClient(app) as test_client:
        for _ in range(5):                                     # polling clients never create a manager
            assert test_client.get("/api/runs").status_code == 200
            assert test_client.get(f"/api/runs/{RUN}/progress").status_code == 200
        manager = app.state.tripy.manager
    assert created == [1] and shutdowns == [manager]
    # a second app in the same process (tests, reloads) gets the same process-wide manager
    again = create_app(mount_mcp=False, frontend=None)
    with TestClient(again):
        assert again.state.tripy.manager is manager and created == [1]


# --- logs ------------------------------------------------------------------------------------------------------------

def test_the_access_log_never_carries_the_mcp_token_or_a_secret(monkeypatch):
    from src.server_logging import ServerLogRedactingFilter, redact_server_logs

    monkeypatch.setenv("TRIPY_MCP_TOKEN", MCP_TOKEN)
    monkeypatch.setenv("GLM_API_KEY", SECRETS["GLM_API_KEY"])
    redact_server_logs()
    redact_server_logs()                                        # idempotent
    access = logging.getLogger("uvicorn.access")
    assert sum(isinstance(f, ServerLogRedactingFilter) for f in access.filters) == 1
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:5", "POST", f"/mcp/{MCP_TOKEN}", "1.1", 200), None)
    for f in access.filters:
        f.filter(record)
    message = record.getMessage()
    assert MCP_TOKEN not in message and record.args[4] == 200
    assert message == '127.0.0.1:5 - "POST /mcp/[redacted] HTTP/1.1" 200'                # exact, scrubbed once
    wrong = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "%s", ("/mcp/guess-0123456789abcdef",), None)
    access.filters[0].filter(wrong)
    assert "guess" not in wrong.getMessage()
    error = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, "boom %s", (SECRETS["GLM_API_KEY"],), None)
    for f in logging.getLogger("uvicorn.error").filters:
        f.filter(error)
    assert SECRETS["GLM_API_KEY"] not in error.getMessage()


# --- no Streamlit ----------------------------------------------------------------------------------------------------

def test_the_production_application_never_imports_streamlit_pandas_or_a_ui_package(data_root):
    code = f"""
import sys
from fastapi.testclient import TestClient
from src.api.app import create_app
with TestClient(create_app(mount_mcp=False, frontend=None)) as client:
    for path in ["/health", "/api/runs", "/api/runs/{RUN}", "/api/runs/{RUN}/vehicles/101122/technical",
                 "/api/runs/{RUN}/live", "/api/runs/{RUN}/benchmark", "/api/runs/{RUN}/documents",
                 "/api/runs/{RUN}/binding-replay", "/api/runs/{RUN}/diagnostics", "/api/benchmark/summary?run_id={RUN}",
                 "/api/benchmark/export/benchmark.json?run_id={RUN}", "/api/cache/documents", "/api/series",
                 "/api/config/status", "/api/run-settings"]:
        assert client.get(path).status_code == 200, path
bad = sorted(m for m in sys.modules if m.split(".")[0] in ("streamlit", "pandas") or m.startswith("src.ui"))
assert not bad, bad
print("ok")
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=180,
                            env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert result.returncode == 0 and result.stdout.strip().endswith("ok"), result.stderr[-3000:]


def test_no_streamlit_remains_in_the_repository():
    assert not (ROOT / "app.py").exists() and not (ROOT / "tripy_server.py").exists()
    assert not (ROOT / ".streamlit").exists() and not (ROOT / "src" / "ui").exists()
    requirements = (ROOT / "requirements.txt").read_text().lower()
    assert "streamlit" not in requirements and "pandas" not in requirements
    imports = re.compile(r"^\s*(import streamlit|from streamlit|import pandas|from pandas)\b|streamlit\.testing", re.M)
    runtime = re.compile(r"streamlit|_stcore|\bst\.(session_state|query_params|cache_resource|fragment|rerun|secrets)\b",
                         re.I)
    for folder in ("src", "tests", "scripts"):
        for path in (ROOT / folder).rglob("*.py"):
            text = path.read_text("utf-8")
            assert not imports.search(text), path
            if folder != "tests":                     # tests may assert the absence of these words
                assert not runtime.search(text), path
    for path in [ROOT / "Dockerfile", ROOT / "railway.json", ROOT / "scripts" / "start.sh", ROOT / ".dockerignore"]:
        assert "streamlit" not in path.read_text().lower() and "_stcore" not in path.read_text(), path


# --- the real server: scripts/start.sh -> one Uvicorn process ---------------------------------------------------------

def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(port: int, path: str, *, method: str = "GET", body: bytes | None = None,
         headers: dict | None = None) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


class Server:
    def __init__(self, tmp: Path, dist: Path, mcp: bool):
        self.port = _port()
        self.log = tmp / f"server-{'mcp' if mcp else 'plain'}.log"
        self.data = tmp / f"data-{'mcp' if mcp else 'plain'}"
        env = {k: v for k, v in os.environ.items()
               if k not in ("TRIPY_MCP_TOKEN", "MILO_RUNS_DIR", "MILO_CACHE_DIR", "TRIPY_FRONTEND_DIST")}
        env.update(PORT=str(self.port), TRIPY_DATA_DIR=str(self.data), TRIPY_ENV="production",
                   TRIPY_ACCESS_TOKEN=ACCESS, GLM_API_KEY="k-test-not-used-000", GLM_MODEL="glm-test",
                   TRIPY_FRONTEND_DIST=str(dist))
        if mcp:
            env["TRIPY_MCP_TOKEN"] = MCP_TOKEN
        self.proc = subprocess.Popen(["sh", str(ROOT / "scripts" / "start.sh")], cwd=ROOT, env=env,
                                     stdout=self.log.open("wb"), stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if _get(self.port, "/health")[0] == 200:
                    return
            except OSError:
                pass
            if self.proc.poll() is not None:
                break
            time.sleep(0.2)
        self.stop()
        raise AssertionError(f"server did not start:\n{self.log.read_text(errors='replace')[-3000:]}")

    def stop(self) -> int | None:
        if self.proc.poll() is None:
            self.proc.terminate()                                   # SIGTERM, as the platform sends it
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        return self.proc.returncode


@pytest.fixture(scope="module")
def servers(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("serving")
    bundle = tmp / "dist"
    (bundle / "assets").mkdir(parents=True)
    (bundle / "index.html").write_text(INDEX)
    (bundle / "assets" / "index-abc123.js").write_text("console.log('tripy')")
    plain = Server(tmp, bundle, mcp=False)
    try:
        with_mcp = Server(tmp, bundle, mcp=True)
    except Exception:
        plain.stop()
        raise
    yield plain, with_mcp
    plain.stop()
    with_mcp.stop()


def test_start_sh_runs_exactly_one_uvicorn_process(servers):
    for server in servers:
        cmdline = Path(f"/proc/{server.proc.pid}/cmdline").read_bytes().split(b"\0")
        assert any(part.endswith(b"uvicorn") for part in cmdline[:2]), cmdline      # exec: the shell is gone
        children = Path(f"/proc/{server.proc.pid}/task/{server.proc.pid}/children").read_text().split()
        assert children == [], children                                             # no worker, no second server


def test_the_real_server_serves_spa_health_and_the_gated_api(servers):
    for server in servers:
        status, body, headers = _get(server.port, "/runs/some-run")
        assert status == 200 and b'id="root"' in body and headers["cache-control"] == "no-cache"
        status, _, headers = _get(server.port, "/assets/index-abc123.js")
        assert status == 200 and "immutable" in headers["cache-control"]
        assert _get(server.port, "/assets/missing.js")[0] == 404
        status, body, _ = _get(server.port, "/health")
        assert status == 200 and json.loads(body) == {"status": "ok", "service": "tripy"}
        assert _get(server.port, "/api/runs")[0] == 401
        assert _get(server.port, "/api/runs", headers={"Authorization": f"Bearer {ACCESS}"})[0] == 200
        assert _get(server.port, "/api/nope", headers={"Authorization": f"Bearer {ACCESS}"})[0] == 404
        assert "server" not in {k.lower() for k in headers}                     # --no-server-header


def test_mcp_exists_only_with_the_variable_and_its_token_never_reaches_a_log(servers):
    plain, with_mcp = servers
    ping = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    status, body, _ = _get(with_mcp.port, f"/mcp/{MCP_TOKEN}", method="POST", body=ping, headers=headers)
    assert status == 200 and json.loads(body)["result"] == {}
    for path in ["/mcp", f"/mcp/{MCP_TOKEN[:-2]}", f"/mcp/{MCP_TOKEN.upper()}", "/mcp/x"]:
        assert _get(with_mcp.port, path, method="POST", body=ping, headers=headers)[:2] == (404, b""), path
    status, body, _ = _get(plain.port, f"/mcp/{MCP_TOKEN}", method="POST", body=ping, headers=headers)
    assert status in (404, 405) and b"jsonrpc" not in body and b'id="root"' not in body
    time.sleep(0.3)
    for server in servers:
        stdout = server.log.read_text(errors="replace")
        assert MCP_TOKEN not in stdout and ACCESS not in stdout and "k-test-not-used-000" not in stdout
        log_file = server.data / "logs" / "tripy.log"
        if log_file.exists():
            assert MCP_TOKEN not in log_file.read_text(errors="replace")
    assert "/mcp/[redacted]" in with_mcp.log.read_text(errors="replace")        # the access log line, scrubbed


def test_sigterm_stops_the_server_cleanly(tmp_path):
    bundle = tmp_path / "dist"
    (bundle / "assets").mkdir(parents=True)
    (bundle / "index.html").write_text(INDEX)
    server = Server(tmp_path, bundle, mcp=False)
    started = time.monotonic()
    # Uvicorn shuts down gracefully (lifespan shutdown: RunManager.shutdown), then re-raises the signal it caught
    assert server.stop() in (0, -15) and time.monotonic() - started < 25
    log = server.log.read_text(errors="replace")
    assert "Waiting for application shutdown" in log and "Application shutdown complete" in log
