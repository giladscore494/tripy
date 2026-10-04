"""PR #41: the read-only MCP (src/mcp_server). No network: tools run over a fixture data root, the protocol runs
in-process over httpx's ASGI transport."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from src.runstate.model import COMPLETED, RunRecord
from src.runstate.repository import FileRunRepository
from src.storage.paths import resolve_paths

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
TOKEN = "tok-5f0c2b7d9a1e4c3b8f6a2d1e0c9b8a7f"
RUN, RECORD, LEGACY = "SYNTH-pr40", "101122", "20261001T185509Z-glm-5.3-one"
DOC = "d_647a84be06077771"
SECRETS = {"GLM_API_KEY": "sk-seeded-GLMKEY-0123456789", "TRIPY_ACCESS_TOKEN": "seeded-access-token-abcdef0123",
           "DATABASE_URL": "postgresql://tripy:pw-seeded-db-7788@db.internal:5432/x",
           "PAYMENTS_SERVICE_PASSWORD": "seeded-other-password-55", "SENTRY_DSN": "https://seeded-dsn-9911@sentry.io/1"}
LEAKS = ["sk-seeded-GLMKEY-0123456789", "seeded-access-token-abcdef0123", "pw-seeded-db-7788",
         "seeded-other-password-55", "seeded-dsn-9911", "bearer-seeded-xyz-12345", "zz-seeded-json-key-998", TOKEN]


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    """A data root like Railway's /data: runs/ (a managed run with run_state.json, a legacy one), cache/, logs/."""
    for name in ("MILO_RUNS_DIR", "MILO_CACHE_DIR", "TRIPY_ENV"):
        monkeypatch.delenv(name, raising=False)
    data = tmp_path / "data"
    runs, cache = data / "runs", data / "cache"
    runs.mkdir(parents=True)
    shutil.copytree(FIXTURES / "pr40_runs" / RUN, runs / RUN)
    shutil.copytree(FIXTURES / "pr40_runs" / "_cache" / "documents", cache / "documents")
    shutil.copytree(FIXTURES / "baseline_runs" / LEGACY, runs / LEGACY)
    events = runs / RUN / RECORD / "events.jsonl"
    with events.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "api_error", "seq": 23, "error": "HTTP 401 for key sk-seeded-GLMKEY-0123456789",
                             "headers": {"Authorization": "Bearer bearer-seeded-xyz-12345"},
                             "raw": '{"api_key": "zz-seeded-json-key-998"}',
                             "dsn": SECRETS["DATABASE_URL"], "note": f"token {TOKEN}"}) + "\n")
        fh.write(json.dumps({"kind": "run_finished", "seq": 24, "status": "completed"}) + "\n")
    FileRunRepository(runs).create(RunRecord(
        run_id=RUN, status=COMPLETED, started_at="2026-10-03T23:49:00+00:00", finished_at="2026-10-03T23:59:00+00:00",
        target={"label": "XPeng G6 · 2026 · MAX", "record_ids": [RECORD]}, request={"run_profile": "production"},
        owner={"boot_id": "elsewhere"}, vehicles={RECORD: {"status": COMPLETED, "engine_status": "completed"}},
        report={"vehicles": {RECORD: {"cost_usd": 0.25}}},
        jobs=[{"started_at": "2026-10-03T23:49:00+00:00", "finished_at": "2026-10-03T23:59:00+00:00"}]))
    text = cache / "documents" / DOC / "text.txt"
    text.write_text(text.read_text("utf-8") + f"\nleaked key {SECRETS['GLM_API_KEY']} here\n", "utf-8")
    (data / "logs").mkdir()
    (data / "logs" / "tripy.log").write_text(f"2026-10-04 INFO tripy: old line with {SECRETS['GLM_API_KEY']}\n"
                                             "2026-10-04 INFO tripy: unrelated\n", "utf-8")
    monkeypatch.setenv("TRIPY_DATA_DIR", str(data))
    monkeypatch.setenv("TRIPY_MCP_TOKEN", TOKEN)
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    return data


def observer():
    from src.mcp_server.tools import Observer
    return Observer(resolve_paths())


def call(name: str, **args) -> dict:
    """One tool through the production wrapper (worker thread, redaction, size cap, audit line)."""
    from src.mcp_server.server import _Slots, call as run_call

    text = asyncio.run(run_call(name, args, lambda: getattr(observer(), name)(**args), _Slots()))
    assert len(text) <= 60_000
    return json.loads(text)


def snapshot(root: Path) -> dict[str, str]:
    out = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        out[rel] = "dir" if path.is_dir() else ("link" if path.is_symlink() else
                                                  hashlib.sha256(path.read_bytes()).hexdigest())
    return out


def sweep() -> list[dict]:
    return [call("list_runs"), call("run_status", run_id=RUN), call("run_events", run_id=RUN, record_id=RECORD),
            call("run_result", run_id=RUN, record_id=RECORD), call("run_diagnostics", run_ids=[RUN, LEGACY]),
            call("binding_replay", run_id=RUN, record_id=RECORD), call("candidates", run_id=RUN, record_id=RECORD),
            call("documents", run_id=RUN, record_id=RECORD), call("document_text", doc_id=DOC),
            call("document_structure", doc_id=DOC), call("document_structure", doc_id=DOC, run_id=RUN,
                                                         record_id=RECORD),
            call("target_identity", record_id=RECORD), call("server_log_tail")]


# --- tools: shape, paging, cursors ------------------------------------------------------------------------------------

def test_list_runs_and_run_status(data_root):
    runs = call("list_runs")
    by_id = {r["run_id"]: r for r in runs["runs"]}
    assert set(by_id) == {RUN, LEGACY}
    row = by_id[RUN]
    assert row["record_ids"] == [RECORD] and row["status"] == COMPLETED and row["profile"] == "production"
    assert row["cost_usd"] == 0.25 and row["wall_time_s"] == 600.0 and row["executing"] is False
    assert runs["paging"] == {"offset": 0, "limit": 20, "returned": 2, "total": 2, "remaining": 0, "next_offset": None}
    page = call("list_runs", limit=1)
    assert page["paging"]["returned"] == 1 and page["paging"]["next_offset"] == 1 and page["paging"]["remaining"] == 1
    status = call("run_status", run_id=RUN)
    assert status["active"] is False and "no RunManager" in status["active_basis"]
    vehicle = status["vehicles"][0]
    assert vehicle["record_id"] == RECORD and vehicle["status"] == COMPLETED and vehicle["engine_status"] == "completed"
    assert {s["key"] for s in vehicle["stages"]} == {"acquisition", "harvest", "sweep", "recovery", "finalization"}


def test_run_status_uses_the_live_manager_without_creating_one(data_root, monkeypatch):
    from src.jobs import manager as manager_mod

    assert manager_mod.existing_manager(resolve_paths()) is None

    class Live:
        def active_runs(self):
            return [RunRecord(run_id=RUN)]

    monkeypatch.setattr("src.mcp_server.tools.existing_manager", lambda paths: Live())
    assert call("run_status", run_id=RUN)["active"] is True
    assert {r["run_id"]: r["executing"] for r in call("list_runs")["runs"]}[RUN] is True
    assert manager_mod.existing_manager(resolve_paths()) is None       # the tools never create a RunManager


def test_run_events_cursor_returns_only_new_rows(data_root):
    first = call("run_events", run_id=RUN, record_id=RECORD, limit=10)
    assert [e["line"] for e in first["events"]] == list(range(1, 11)) and first["next"] == 10 and first["more"]
    rest = call("run_events", run_id=RUN, record_id=RECORD, since=first["next"])
    assert [e["line"] for e in rest["events"]] == list(range(11, 25)) and rest["next"] == 24 and not rest["more"]
    assert call("run_events", run_id=RUN, record_id=RECORD, since=24)["events"] == []
    events = data_root / "runs" / RUN / RECORD / "events.jsonl"
    with events.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "note", "seq": 25}) + "\n" + '{"kind": "torn", "seq"')   # a line being written
    tail = call("run_events", run_id=RUN, record_id=RECORD, since=24)
    assert [(e["line"], e["kind"]) for e in tail["events"]] == [(25, "note")] and tail["next"] == 25
    only = call("run_events", run_id=RUN, record_id=RECORD, kinds=["candidates_harvested"])
    assert {e["kind"] for e in only["events"]} == {"candidates_harvested"} and len(only["events"]) == 4
    assert only["next"] == 25


def test_result_diagnostics_replay_candidates(data_root):
    result = call("run_result", run_id=RUN, record_id=RECORD)
    assert result["synthesized"] is True and len(result["fields"]) == 11
    assert all({"field", "state", "value", "evidence_ids"} <= set(f) for f in result["fields"])
    diag = call("run_diagnostics", run_ids=[RUN])
    row = diag["per_vehicle"][0]
    assert row["run_id"] == RUN and row["record_id"] == RECORD
    assert any(k.startswith("replay_") or "replay" in k for k in row), sorted(row)
    replay = call("binding_replay", run_id=RUN, record_id=RECORD, field="wheelbase_mm")
    assert replay["items"] and {i["field"] for i in replay["items"]} == {"wheelbase_mm"}
    assert set(replay["summary"]["fields"]) == {"wheelbase_mm"}
    assert all("variant_map_region_now" in i and "binding_basis_now" in i for i in replay["items"])
    cands = call("candidates", run_id=RUN, record_id=RECORD)
    assert len(cands["rows"]) == 11 and {"field", "origin", "rejection"} <= set(cands["rows"][0])
    one = call("candidates", run_id=RUN, record_id=RECORD, field="height_mm")
    assert [r["field"] for r in one["rows"]] == ["height_mm"]


def test_documents_text_structure_identity_and_log(data_root):
    docs = call("documents", run_id=RUN, record_id=RECORD)["documents"]
    by_id = {d["doc_id"]: d for d in docs}
    assert DOC in by_id and len(docs) == 4
    meta = by_id[DOC]
    assert meta["url"] == "https://www.xpeng.com/de-de/g6/specs" and meta["market"] == "DE"
    assert {"source_authority", "content_type", "size_bytes", "fetched_at", "used_for_fields"} <= set(meta)
    assert any(d["used_for_fields"] for d in docs)
    text = call("document_text", doc_id=DOC, offset=0, limit=50)
    assert text["returned_chars"] == 50 and text["next_offset"] == 50 and text["remaining_chars"] > 0
    second = call("document_text", doc_id=DOC, offset=50, limit=100000)
    assert second["offset"] == 50 and second["next_offset"] is None and second["remaining_chars"] == 0
    assert text["text"] + second["text"] == call("document_text", doc_id=DOC, limit=50000)["text"]
    plain = call("document_structure", doc_id=DOC)
    assert {"tables", "dom_groups", "variant_map", "paging"} <= set(plain) and "cached" in plain["variant_map"]
    targeted = call("document_structure", doc_id=DOC, run_id=RUN, record_id=RECORD)
    vmap = targeted["variant_map"]["map"]
    assert targeted["variant_map"]["computed_for"]["record_id"] == RECORD
    assert {"regions", "inventory", "catalog"} <= set(vmap)
    ident = call("target_identity", record_id=RECORD)
    assert ident["run_id"] == RUN and ident["target_identity"]["family"] == "g6"
    assert ident["level15_payload"]["identity"]["model_code"] == "NSGHA"
    assert isinstance(ident["catalog_entries"], list)
    log = call("server_log_tail", lines=50, grep="UNRELATED")
    assert log["lines"] == ["2026-10-04 INFO tripy: unrelated"]


# --- validation -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["..", "../runs", "/etc", "a/b", "_cache", ".hidden", "", "x" * 200, "nope"])
def test_bad_run_ids_are_rejected(data_root, bad):
    from src.mcp_server.safety import ToolInputError

    with pytest.raises(ToolInputError):
        observer().run_status(bad)
    with pytest.raises(ToolInputError):
        observer().run_events(bad, RECORD)


def test_traversal_unknown_ids_and_symlinks_out_of_tree_are_rejected(data_root, tmp_path):
    from src.mcp_server.safety import ToolInputError

    outside = tmp_path / "outside"
    (outside / RECORD).mkdir(parents=True)
    (outside / RECORD / "events.jsonl").write_text('{"kind": "secret"}\n')
    (data_root / "runs" / "linked").symlink_to(outside, target_is_directory=True)
    (data_root / "runs" / RUN / "evil").symlink_to(outside / RECORD, target_is_directory=True)
    doc_out = tmp_path / "doc"
    doc_out.mkdir()
    (doc_out / "meta.json").write_text("{}")
    (data_root / "cache" / "documents" / "d_0123456789abcdef").symlink_to(doc_out, target_is_directory=True)
    o = observer()
    for args in [("linked", RECORD), (RUN, "evil"), (RUN, ".."), (RUN, "../../etc"), (RUN, "999999")]:
        with pytest.raises(ToolInputError):
            o.run_events(*args)
    for doc in ["d_0123456789abcdef", "../d_647a84be06077771", "d_647A84BE06077771", "/etc/passwd", "d_ffffffffffffffff"]:
        with pytest.raises(ToolInputError):
            o.document_text(doc)
    with pytest.raises(ToolInputError):
        o.target_identity("../101122")
    with pytest.raises(ToolInputError):
        o.run_diagnostics(["../x"])
    with pytest.raises(ValueError, match="run_id"):
        call("run_status", run_id="..")


# --- redaction, size caps, read-only ----------------------------------------------------------------------------------

def test_seeded_secrets_are_redacted_everywhere(data_root):
    responses = sweep() + [call("run_events", run_id=RUN, record_id=RECORD, since=22),
                           call("document_text", doc_id=DOC, limit=50000), call("server_log_tail", grep="old line")]
    blob = json.dumps(responses, ensure_ascii=False)
    for leak in LEAKS:
        assert leak not in blob, leak
    api_error = responses[-3]["events"][0]
    assert api_error["kind"] == "api_error" and "[redacted]" in json.dumps(api_error)
    assert "[redacted]" in responses[-2]["text"] and "[redacted]" in responses[-1]["lines"][0]
    # configuration that merely has KEY / TOKEN in its name but is not a secret stays readable
    from src.mcp_server.safety import Redactor
    assert Redactor({"GLM_MAX_TOKENS": "4096", "X_KEY_ENABLED": "true"}).text("4096 true") == "4096 true"


def test_every_response_is_capped_and_pages(data_root):
    events = data_root / "runs" / RUN / RECORD / "events.jsonl"
    with events.open("a", encoding="utf-8") as fh:
        for i in range(40):
            fh.write(json.dumps({"kind": "model_response", "seq": 100 + i, "content": "x" * 20_000}) + "\n")
    text = data_root / "cache" / "documents" / DOC / "text.txt"
    text.write_text("y" * 300_000, "utf-8")
    page = call("run_events", run_id=RUN, record_id=RECORD, since=24, limit=1000)
    assert page["more"] and 0 < page["returned"] < 40
    assert all(len(e["content"]) < 9_000 for e in page["events"])          # one long string is clipped
    seen = page["returned"]
    while page["more"]:
        page = call("run_events", run_id=RUN, record_id=RECORD, since=page["next"], limit=1000)
        seen += page["returned"]
    assert seen == 40
    doc = call("document_text", doc_id=DOC, limit=50_000)
    assert doc["returned_chars"] == 50_000 and doc["remaining_chars"] == 250_000
    from src.mcp_server.safety import MAX_RESPONSE_CHARS, Redactor, finish
    huge = finish({"blob": "z" * 200_000, "q": '"' * 10_000}, Redactor({}))
    assert len(huge) <= MAX_RESPONSE_CHARS and json.loads(huge)["truncated"] is True


def test_full_tool_sweep_leaves_the_data_tree_byte_identical(data_root):
    from src import evidence_admission

    log = data_root / "logs" / "tripy.log"
    before, log_before = snapshot(data_root), log.read_bytes()
    shared_texts = list(evidence_admission._TEXTS)
    sweep()
    after = snapshot(data_root)
    assert list(evidence_admission._TEXTS) == shared_texts       # the engine's in-process document LRU is untouched
    assert {k: v for k, v in after.items() if not k.startswith("logs")} == \
           {k: v for k, v in before.items() if not k.startswith("logs")}
    # the only change allowed under the data root: audit lines appended to the server log by this process
    assert log.read_bytes().startswith(log_before)
    assert not list((data_root / "runs" / RUN / RECORD).glob("binding_replay*"))
    assert not list((data_root / "runs" / RUN / RECORD).glob("diagnostics*"))
    assert not list((data_root / "cache" / "documents" / DOC).glob("derived_*"))


def test_each_call_logs_one_audit_line_without_the_token(data_root):
    records: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    handler = Grab()
    logging.getLogger("tripy").addHandler(handler)
    try:
        call("run_status", run_id=RUN)
        call("document_text", doc_id=DOC, limit=10)
        with pytest.raises(ValueError):
            asyncio.run(_raise_rejected())
    finally:
        logging.getLogger("tripy").removeHandler(handler)
    audit = [r for r in records if "mcp call" in r]
    assert len(audit) == 3
    assert "tool=run_status" in audit[0] and f"run_id={RUN}" in audit[0] and "outcome=ok" in audit[0]
    assert "chars=" in audit[1] and "ms=" in audit[1] and f"doc_id={DOC}" in audit[1]
    assert "outcome=rejected" in audit[2]
    assert not any(TOKEN in r for r in records)


async def _raise_rejected():
    from src.mcp_server.server import _Slots, call as run_call
    await run_call("run_status", {"run_id": "nope"}, lambda: observer().run_status("nope"), _Slots())


# --- the HTTP gate and the protocol -----------------------------------------------------------------------------------

async def _with_app(fn):
    from starlette.applications import Starlette

    from src.mcp_server.asgi import McpRoute
    from src.mcp_server.server import build

    server = build()
    server.streamable_http_app()
    manager = server.session_manager
    app = Starlette(routes=[McpRoute(TOKEN, manager.handle_request)])
    async with manager.run():
        return await fn(app)




def test_mcp_protocol_lists_the_twelve_read_only_tools_and_calls_them(data_root):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from src.mcp_server.server import TOOL_NAMES

    async def scenario(app):
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), timeout=30)
        async with client, streamable_http_client(f"http://testserver/mcp/{TOKEN}", http_client=client) \
                as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                ok = await session.call_tool("list_runs", {"limit": 5})
                bad = await session.call_tool("run_status", {"run_id": "../../etc"})
                return tools, ok, bad

    tools, ok, bad = asyncio.run(_with_app(scenario))
    assert tuple(t.name for t in tools) == TOOL_NAMES
    assert all(t.annotations.readOnlyHint and not t.annotations.destructiveHint for t in tools)
    assert not ok.isError and {r["run_id"] for r in json.loads(ok.content[0].text)["runs"]} == {RUN, LEGACY}
    assert bad.isError and "run_id: '../../etc'" in bad.content[0].text and TOKEN not in bad.content[0].text


def test_only_the_exact_secret_path_is_served(data_root):
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    seen: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record):
            if not record.name.startswith(("httpx", "httpcore")):     # the test's own client logs its request URL
                seen.append(record.getMessage())

    grab = Grab(level=logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(grab)
    old_level = root.level
    root.setLevel(logging.DEBUG)

    async def scenario(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            good = await client.post(f"/mcp/{TOKEN}", json=body, headers=headers)
            bad = {path: await client.post(path, json=body, headers=headers) for path in [
                "/mcp", "/mcp/", "/mcp/wrong", f"/mcp/{TOKEN[:-1]}", f"/mcp/{TOKEN[:8]}", f"/mcp/{TOKEN.upper()}",
                f"/MCP/{TOKEN}", f"/Mcp/{TOKEN}", f"/mcp/{TOKEN}/", f"/mcp/{TOKEN}/x", f"/mcp/{TOKEN}x",
                f"/mcp//{TOKEN}"]}
            get = await client.get("/mcp/anything")
            return good, bad, get

    try:
        good, bad, get = asyncio.run(_with_app(scenario))
    finally:
        root.removeHandler(grab)
        root.setLevel(old_level)
    assert good.status_code == 200 and good.json()["result"] == {}
    for path, response in bad.items():
        assert (response.status_code, response.content) == (404, b""), path
    assert (get.status_code, get.content) == (404, b"")
    for response in [good, *bad.values(), get]:
        assert TOKEN not in response.text and TOKEN not in json.dumps(dict(response.headers))
    assert seen and not any(TOKEN in line for line in seen)


def test_gate_compares_in_constant_time_and_an_unusable_token_serves_nothing(monkeypatch):
    import inspect

    from src.mcp_server import asgi, token_problem

    assert "hmac.compare_digest" in inspect.getsource(asgi.McpRoute.authorized)
    assert token_problem("short") and token_problem("has/slash-0123456789") and token_problem("")
    assert token_problem(TOKEN) is None
    route = asgi.mcp_routes("tooshort")
    assert len(route) == 1 and route[0].authorized("/mcp/tooshort") is False
    assert asgi.mcp_routes("") == [] and asgi.mcp_routes("   ") == []
    monkeypatch.delenv("TRIPY_MCP_TOKEN", raising=False)
    assert asgi.mcp_routes() == []


# --- variable unset: nothing exists ------------------------------------------------------------------------------------

def test_dashboard_never_imports_the_mcp_package(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "TRIPY_MCP_TOKEN"}
    env.update(TRIPY_DATA_DIR=str(tmp_path / "data"), PYTHONPATH=str(ROOT))
    code = ("import sys\n"
            "from streamlit.testing.v1 import AppTest\n"
            "at = AppTest.from_file('app.py', default_timeout=90)\n"
            "at.run()\n"
            "assert not at.exception, at.exception\n"
            "bad = sorted(m for m in sys.modules if m == 'mcp' or m.startswith(('mcp.', 'src.mcp_server')))\n"
            "print('MCP_MODULES', bad)\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    assert "MCP_MODULES []" in out.stdout


def test_start_sh_keeps_todays_command_without_the_variable(tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "streamlit").write_text('#!/bin/sh\necho "streamlit $*"\n')
    (fake / "python").write_text("#!/bin/sh\nexit 0\n")          # skip startup diagnostics in this test
    for item in fake.iterdir():
        item.chmod(0o755)
    base = {"PATH": f"{fake}:{os.environ['PATH']}", "PORT": "9123"}

    def start(**env) -> str:
        return subprocess.run(["sh", str(ROOT / "scripts" / "start.sh")], env={**base, **env}, capture_output=True,
                              text=True, timeout=30, check=True).stdout.strip()

    flags = ("--server.address=0.0.0.0 --server.port=9123 --server.headless=true --server.fileWatcherType=none "
             "--server.runOnSave=false --client.showErrorDetails=none --browser.gatherUsageStats=false")
    assert start() == f"streamlit run app.py {flags}"
    assert start(TRIPY_MCP_TOKEN="") == f"streamlit run app.py {flags}"
    assert start(TRIPY_MCP_TOKEN="  ") == f"streamlit run app.py {flags}"
    assert start(TRIPY_MCP_TOKEN=TOKEN) == f"streamlit run tripy_server.py {flags}"
    assert TOKEN not in start(TRIPY_MCP_TOKEN=TOKEN)


def test_launcher_is_a_streamlit_app_over_the_unchanged_dashboard():
    from streamlit.web.server.app_discovery import discover_asgi_app

    found = discover_asgi_app(ROOT / "tripy_server.py")
    assert found.is_asgi_app and found.import_string == "tripy_server:app"
    assert not discover_asgi_app(ROOT / "app.py").is_asgi_app
    source = (ROOT / "tripy_server.py").read_text()
    assert 'st.App("app.py", routes=mcp_routes(), lifespan=mcp_lifespan)' in source
    assert "mcp" not in (ROOT / "app.py").read_text().lower()
