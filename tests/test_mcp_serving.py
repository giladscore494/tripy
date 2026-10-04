"""PR #41: the real server (scripts/start.sh -> streamlit run) with and without TRIPY_MCP_TOKEN, on localhost only.

The dashboard must behave identically in both states (health check, host config, the page, media route, the
/_stcore/stream WebSocket and the access-token lock screen rendered over it); only the MCP state adds /mcp/<token>.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "srv-0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e"
ACCESS = "dashboard-access-token-0123456789"

pytestmark = pytest.mark.skipif(shutil.which("streamlit") is None, reason="streamlit CLI not installed")


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _request(port: int, path: str, *, method: str = "GET", body: bytes | None = None,
             headers: dict | None = None) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


class Server:
    def __init__(self, tmp: Path, mcp: bool):
        self.port = _port()
        self.log = tmp / f"server-{'mcp' if mcp else 'plain'}.log"
        self.data = tmp / f"data-{'mcp' if mcp else 'plain'}"
        env = {k: v for k, v in os.environ.items() if k not in ("TRIPY_MCP_TOKEN", "MILO_RUNS_DIR", "MILO_CACHE_DIR")}
        env.update(PORT=str(self.port), TRIPY_DATA_DIR=str(self.data), TRIPY_ENV="production",
                   TRIPY_ACCESS_TOKEN=ACCESS, GLM_API_KEY="k-test-not-used-000", GLM_MODEL="glm-test")
        if mcp:
            env["TRIPY_MCP_TOKEN"] = TOKEN
        self.proc = subprocess.Popen(["sh", str(ROOT / "scripts" / "start.sh")], cwd=ROOT, env=env,
                                     stdout=self.log.open("wb"), stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                if _request(self.port, "/_stcore/health")[0] == 200:
                    return
            except OSError:
                pass
            if self.proc.poll() is not None:
                break
            time.sleep(0.3)
        self.stop()
        raise AssertionError(f"server did not start:\n{self.log.read_text(errors='replace')[-3000:]}")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def first_render(self) -> str:
        """Open the dashboard's WebSocket like a browser, request a script run, return the rendered elements."""
        from streamlit.proto.BackMsg_pb2 import BackMsg
        from streamlit.proto.ForwardMsg_pb2 import ForwardMsg
        from websockets.sync.client import connect

        rendered = []
        with connect(f"ws://127.0.0.1:{self.port}/_stcore/stream", subprotocols=["streamlit"],
                     open_timeout=20) as ws:
            assert ws.subprotocol == "streamlit"
            msg = BackMsg()
            msg.rerun_script.query_string = ""
            msg.rerun_script.page_script_hash = ""
            ws.send(msg.SerializeToString())
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                fm = ForwardMsg()
                fm.ParseFromString(ws.recv(timeout=30))
                rendered.append(str(fm.delta) if fm.HasField("delta") else "")
                if fm.HasField("script_finished"):
                    break
        return "\n".join(rendered)


@pytest.fixture(scope="module")
def servers(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("serving")
    plain = Server(tmp, mcp=False)
    try:
        with_mcp = Server(tmp, mcp=True)
    except Exception:
        plain.stop()
        raise
    yield plain, with_mcp
    plain.stop()
    with_mcp.stop()


def test_the_dashboard_is_served_identically_in_both_states(servers):
    plain, with_mcp = servers
    for path in ["/_stcore/health", "/_stcore/host-config", "/", "/media/0123456789abcdef.txt",
                 "/_stcore/script-health-check", "/healthz", "/favicon.png"]:
        a, b = _request(plain.port, path), _request(with_mcp.port, path)
        assert a[0] == b[0], path
        assert a[1] == b[1] or path == "/_stcore/script-health-check", path
    assert _request(plain.port, "/_stcore/health")[1] == b"ok"
    index = _request(with_mcp.port, "/")[1].decode()
    assert "<div id=\"root\">" in index or "id=\"root\"" in index


def test_the_websocket_runs_the_same_gated_app_in_both_states(servers):
    renders = [server.first_render() for server in servers]
    for render in renders:
        assert "TRIPY" in render and "Private research dashboard" in render and "Access token" in render
        assert "Start research" not in render            # the access-token gate still locks everything else
    assert renders[0].count("Access token") == renders[1].count("Access token")


def test_mcp_exists_only_with_the_variable(servers):
    plain, with_mcp = servers
    ping = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    status, body, _ = _request(with_mcp.port, f"/mcp/{TOKEN}", method="POST", body=ping, headers=headers)
    assert status == 200 and json.loads(body)["result"] == {}
    for path in ["/mcp", f"/mcp/{TOKEN[:-2]}", f"/mcp/{TOKEN.upper()}", f"/MCP/{TOKEN}", "/mcp/x"]:
        assert _request(with_mcp.port, path, method="POST", body=ping, headers=headers)[:2] == (404, b""), path
    # without the variable nothing answers as the MCP: the path is Streamlit's (no JSON-RPC, no empty 404)
    status, body, _ = _request(plain.port, f"/mcp/{TOKEN}", method="POST", body=ping, headers=headers)
    assert (status, body) != (404, b"") and b"jsonrpc" not in body


def test_mcp_tool_call_over_the_real_server_and_no_token_in_logs(servers):
    import asyncio

    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    _, with_mcp = servers

    async def scenario():
        async with streamable_http_client(f"http://127.0.0.1:{with_mcp.port}/mcp/{TOKEN}") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("server_log_tail", {"lines": 50})
                return json.loads(result.content[0].text)

    log = asyncio.run(scenario())
    assert log["exists"] and any("MCP enabled" in line for line in log["lines"])
    time.sleep(0.5)
    stdout = with_mcp.log.read_text(errors="replace")
    assert "mcp call tool=server_log_tail" in stdout
    assert TOKEN not in stdout
    assert TOKEN not in (with_mcp.data / "logs" / "tripy.log").read_text(errors="replace")
