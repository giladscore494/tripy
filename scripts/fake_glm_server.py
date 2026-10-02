"""Offline fake of the Z.ai GLM API for local smoke tests of the full app (no credentials, no internet).

    python scripts/fake_glm_server.py --port 8765 [--delay 1.5] [--fail finalizer|research|none]
    GLM_BASE_URL=http://127.0.0.1:8765/api/paas/v4 GLM_API_KEY=fake GLM_MODEL=glm-5.3-flash \
        NO_PROXY=127.0.0.1,localhost streamlit run app.py

Behaviour (deterministic):
* primary research: turn 1 searches, turn 2 fetches the local spec page, turn 3 answers without tool calls;
* every other tool-enabled call (document sweep, tail recovery) answers without tool calls;
* the finalizer (no tools) returns a small structured result;
* `--fail finalizer` answers every finalizer request with HTTP 500 (a clean finalization failure);
  `--fail research` answers research turns with HTTP 500.
It never validates the API key and never logs it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent import research_system_prompt  # noqa: E402

SPEC_PAGE = """<html><head><title>XPeng G6 2026 specifications (Israel)</title></head><body>
<h1>XPeng G6 2026 MAX — technical specifications</h1>
<table>
<tr><th>Maximum torque</th><td>660 Nm</td></tr>
<tr><th>Wheelbase</th><td>2,890 mm</td></tr>
<tr><th>Length</th><td>4,753 mm</td></tr>
<tr><th>Width</th><td>1,920 mm</td></tr>
<tr><th>Height</th><td>1,650 mm</td></tr>
<tr><th>Curb weight</th><td>2,095 kg</td></tr>
<tr><th>Battery capacity</th><td>87.5 kWh</td></tr>
<tr><th>Cargo volume</th><td>571 l</td></tr>
</table></body></html>"""


def reply(content: str | None = None, tool_calls: list | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content or ""}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 150, "total_tokens": 1350}}


def tool_call(n: int, name: str, args: dict) -> dict:
    return {"id": f"call_{n}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class Handler(BaseHTTPRequestHandler):
    server_version = "FakeGLM/1"

    def log_message(self, fmt, *args):  # quiet, and never echoes headers
        sys.stderr.write("fake-glm %s\n" % (fmt % args))

    def _send(self, status: int, body: dict | str, ctype: str = "application/json") -> None:
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):
        self._send(404, {})

    def do_GET(self):
        if self.path.startswith("/spec"):
            return self._send(200, SPEC_PAGE, "text/html; charset=utf-8")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        port = self.server.server_address[1]
        if self.path.endswith("/web_search"):
            time.sleep(self.server.delay / 2)
            return self._send(200, {"search_result": [
                {"title": "XPeng G6 2026 specifications (Israel)", "link": f"http://127.0.0.1:{port}/spec",
                 "content": "Torque 660 Nm, wheelbase 2,890 mm", "media": "example importer"}]})
        if not self.path.endswith("/chat/completions"):
            return self._send(404, {"error": "unknown endpoint"})
        time.sleep(self.server.delay)
        messages = payload.get("messages") or []
        tools = payload.get("tools") or []
        system = next((m.get("content") for m in messages if m.get("role") == "system"), "")
        is_research = bool(tools) and system == research_system_prompt()
        if not tools:
            if self.server.fail == "finalizer":
                return self._send(500, {"error": {"code": "500", "message": "fake provider failure"}})
            return self._send(200, reply(json.dumps({
                "summary": "XPeng G6 2026 MAX: specifications from the importer's spec page (fake provider).",
                "fields": {"torque_nm": {"value": 660, "unit": "Nm", "market": "IL", "provenance": "israel_direct",
                                         "evidence_ids": []},
                           "wheelbase_mm": {"value": 2890, "unit": "mm", "market": "IL",
                                            "provenance": "israel_direct", "evidence_ids": []}},
                "conflicts": [], "additional_findings": []})))
        if is_research:
            if self.server.fail == "research":
                return self._send(500, {"error": {"code": "500", "message": "fake provider failure"}})
            turns = sum(1 for m in messages if m.get("role") == "assistant")
            if turns == 0:
                return self._send(200, reply(tool_calls=[tool_call(1, "search_web", {"query": "XPeng G6 2026 specs"})]))
            if turns == 1:
                return self._send(200, reply(tool_calls=[tool_call(2, "fetch_url",
                                                                   {"url": f"http://127.0.0.1:{port}/spec"})]))
            return self._send(200, reply("Research complete; the spec page is acquired."))
        return self._send(200, reply("{}"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--delay", type=float, default=1.0, help="seconds per chat call")
    parser.add_argument("--fail", choices=["none", "finalizer", "research"], default="none")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.delay, server.fail = args.delay, args.fail
    print(f"fake GLM on http://127.0.0.1:{args.port}/api/paas/v4 (fail={args.fail}, delay={args.delay}s)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
