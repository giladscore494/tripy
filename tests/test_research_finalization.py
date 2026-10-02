"""Research / finalization split, retry semantics, durable partial results, incomplete-run
reconstruction and recovery. Fake HTTP and scripted GLM only: no network, no paid calls.
"""

import json
import shutil
from pathlib import Path

import pytest
import requests

from conftest import FakeResponse, FakeSession, cache_source
from test_run_config import API_KEY, PostResponse, ScriptedPostSession, chat_reply, tool_call
from test_tools_smoke import ScriptedGLM, _call

from src import cli
from src.agent import (FINALIZER_SYSTEM_PROMPT, SYSTEM_PROMPT, AgentConfig, agent_config_from_env, run_vehicle,
                       tool_config_from_env)
from src.benchmark import (HANDSHAKE_RECORD_ID, aggregate, benchmark_vehicles, compute_metrics, research_one,
                           start_batch)
from src.glm_client import GLMClient, GLMError, GLMSettings
from src.pricing import UNKNOWN_USAGE_NOTE, default_pricing
from src.recovery import RecoveryError, finalize_existing_run, plan_recovery
from src.storage.cache import DocumentCache
from src.storage.run_loader import INCOMPLETE_BANNER, load_runs, run_document_metas
from src.storage.run_log import RunLog, read_events
from src.tools import ToolConfig
from src.ui import run_view

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "baseline_runs"
BASELINE_BATCH = "20261001T185509Z-glm-5.3-one"
FINAL_JSON = {"vehicle_id": "101122", "summary": "compiled", "fields": {
    "torque_nm": {"value": 660, "unit": "Nm", "evidence_ids": ["e1"]}}}


def vehicle44():
    return next(v for v in benchmark_vehicles() if v["upstream_record_id"] == HANDSHAKE_RECORD_ID)


def env_client(monkeypatch, session, **env):
    monkeypatch.setenv("GLM_API_KEY", API_KEY)
    for name in ("GLM_MODEL", "GLM_FINALIZER_MODEL", "GLM_CHAT_MAX_ATTEMPTS", "GLM_SEARCH_MAX_ATTEMPTS",
                 "GLM_CHAT_TIMEOUT_S", "GLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return GLMClient(GLMSettings.from_env(), session=session, sleeper=lambda s: None)


def chat_payloads(session):
    return [r["payload"] for r in session.requests if r["url"].endswith("chat/completions")]


def run_one(client, tmp_path, agent_cfg, fetch_routes=None, batch="b"):
    runs = tmp_path / "runs"
    cache = DocumentCache(runs / "_cache")
    result = research_one(vehicle44(), {"upstream_record_id": HANDSHAKE_RECORD_ID, "tozar": "אקספנג"},
                          client=client, cache=cache, runs_dir=runs, batch_id=batch, agent_cfg=agent_cfg,
                          tool_cfg=ToolConfig(search_backend="glm"), pricing=default_pricing(client.model),
                          level15_source="snapshot", session=FakeSession(fetch_routes or {}))
    return result, runs, cache


BIG_PAGE = "https://www.xpeng.com/g6/specs"
DEEP_MARKER = "DEEP-UNIQUE-MARKER-QQ"


def big_page_body() -> bytes:
    filler = "".join(f"<p>spec row {i}: value {i * 7} units</p>" for i in range(6000))
    return f"<html><body><h1>G6</h1><p>torque 660 Nm</p>{filler}<p>{DEEP_MARKER}</p></body></html>".encode()


# 1 ----------------------------------------------------------------------------------------

def test_glm_model_flash_is_sent_unchanged(monkeypatch, tmp_path):
    session = ScriptedPostSession({"chat/completions": [
        chat_reply(tool_call("c1", "fetch_url", {"url": BIG_PAGE})),
        chat_reply({"role": "assistant", "content": json.dumps(FINAL_JSON)}),
    ]})
    client = env_client(monkeypatch, session, GLM_MODEL="glm-5.3-flash")
    assert client.model == "glm-5.3-flash" and client.finalizer_model == "glm-5.3-flash"
    result, _, _ = run_one(client, tmp_path, AgentConfig(field_recovery_enabled=False, max_steps=4),
                           {BIG_PAGE: FakeResponse(b"<html><body>660 Nm</body></html>")})
    assert [p["model"] for p in chat_payloads(session)] == ["glm-5.3-flash", "glm-5.3-flash"]
    assert result["status"] == "completed" and result["research_model"] == "glm-5.3-flash"
    assert result["glm_config"]["model"] == "glm-5.3-flash"
    assert result["pricing"]["input_per_mtok"] == 0.15


# 2, 3, 6 -----------------------------------------------------------------------------------

def test_finalizer_model_only_changes_compact_finalization_call(monkeypatch, tmp_path):
    session = ScriptedPostSession({"chat/completions": [
        chat_reply(tool_call("c1", "fetch_url", {"url": BIG_PAGE})),
        chat_reply(tool_call("c2", "find_in_document", {"document_id": "PLACEHOLDER", "query": "torque"})),
        chat_reply(tool_call("c3", "store_evidence", {"field": "torque_nm", "value": 660, "source_url": BIG_PAGE,
                                                      "quote": "torque 660 Nm"})),
        chat_reply({"role": "assistant", "content": json.dumps(FINAL_JSON)}),
    ]})
    client = env_client(monkeypatch, session, GLM_MODEL="glm-5.3-flash", GLM_FINALIZER_MODEL="glm-5.3")
    result, runs, cache = run_one(client, tmp_path, AgentConfig(field_recovery_enabled=False, max_steps=3, no_new_research_turns=0),
                                  {BIG_PAGE: FakeResponse(big_page_body())})
    payloads = chat_payloads(session)
    research, final = payloads[:3], payloads[3]
    assert all(p["model"] == "glm-5.3-flash" and p.get("tools") for p in research)
    assert final["model"] == "glm-5.3" and "tools" not in final

    # max_steps -> compact finalization, not the historical conversation.
    assert result["stop_reason"] == "max_steps" and result["status"] == "max_steps_finalized"
    assert result["output"]["summary"] == "compiled"
    roles = [m["role"] for m in final["messages"]]
    assert roles == ["system", "user"]
    assert final["messages"][0]["content"] == FINALIZER_SYSTEM_PROMPT
    sent = json.dumps(final["messages"], ensure_ascii=False)
    assert SYSTEM_PROMPT not in sent and "tool_call_id" not in sent
    assert DEEP_MARKER not in sent  # the 200 KB page never reaches the finalizer
    user_content = final["messages"][1]["content"]
    assert '"evidence_id": "e1"' in user_content and "torque 660 Nm" in user_content
    assert len(sent) < 70000

    # Metrics and provenance
    fin = result["finalization"]
    assert fin["model"] == "glm-5.3" and fin["finalizer_input_chars"] == sum(len(m["content"]) for m in final["messages"])
    assert fin["prompt_tokens"] == 1000 and fin["completion_tokens"] == 100 and fin["model_calls"] == 1
    assert result["research_model"] == "glm-5.3-flash" and result["finalizer_model"] == "glm-5.3"
    assert result["usage_research"]["model_calls"] == 3 and result["usage_finalizer"]["model_calls"] == 1
    assert result["pricing_finalizer"]["input_per_mtok"] == 1.40
    assert result["cost"]["tokens_usd"] == round((3000 * 0.15 + 300 * 0.5 + 1000 * 1.40 + 100 * 4.40) / 1e6, 6)
    m = result["metrics"]
    assert (m["research_model_calls"], m["finalizer_model_calls"], m["finalizer_model"]) == (3, 1, "glm-5.3")
    assert m["finalizer_input_chars"] == fin["finalizer_input_chars"] and m["finalizer_prompt_tokens"] == 1000
    run_dir = runs / "b" / HANDSHAKE_RECORD_ID
    request = json.loads((run_dir / "finalizer_request.json").read_text("utf-8"))
    assert request["model"] == "glm-5.3" and request["messages"] == final["messages"]
    kinds = [e["kind"] for e in read_events(run_dir / "events.jsonl")]
    assert kinds.index("research_stopped") < kinds.index("finalization_started") < kinds.index("finalization_finished")
    # the full document stays on disk
    assert DEEP_MARKER in cache.read_text(result["documents"][0])
    for path in runs.rglob("*"):
        if path.is_file():
            assert API_KEY not in path.read_text("utf-8", errors="ignore"), path


# 4, 5 ---------------------------------------------------------------------------------------

def test_large_document_text_is_compacted_and_ids_survive(make_ctx, tmp_path):
    ctx = make_ctx({BIG_PAGE: FakeResponse(big_page_body())})
    doc_id = "d_" + __import__("hashlib").sha256(f"fetch:{BIG_PAGE}".encode()).hexdigest()[:16]
    turns = [
        {"role": "assistant", "content": "", "tool_calls": [_call("c1", "fetch_url", {"url": BIG_PAGE})]},
        {"role": "assistant", "content": "", "tool_calls": [_call("c2", "extract_html", {"document_id": doc_id,
                                                                                          "max_chars": 50000})]},
    ]
    turns += [{"role": "assistant", "content": "", "tool_calls": [
        _call(f"c{i}", "store_evidence", {"field": f"f{i}", "value": i * 7, "document_id": doc_id,
                                          "quote": f"spec row {i}: value {i * 7} units"})]} for i in range(3, 9)]
    turns.append({"role": "assistant", "content": json.dumps(FINAL_JSON)})
    client = ScriptedGLM(turns)
    cfg = AgentConfig(field_recovery_enabled=False, max_steps=12, keep_recent_tool_results=2, max_tool_output_chars=5000, compact_tool_output_chars=600)
    result = run_vehicle({"upstream_record_id": "1"}, {}, client=client, cache=ctx.cache,
                         run_log=RunLog(tmp_path, "b", "1"), config=cfg, tool_config=ctx.config, session=ctx.session)
    assert result["status"] == "completed"
    # The fetch returned only a short preview, extract_html was capped by the tool config.
    fetch_msg = client.requests[1]["messages"][-1]
    assert fetch_msg["role"] == "tool" and len(fetch_msg["content"]) <= cfg.max_tool_output_chars + 200
    assert len(json.loads(fetch_msg["content"])["text_preview"]) <= ctx.config.preview_chars
    extract_msg = client.requests[2]["messages"][-1]["content"]
    assert len(json.loads(extract_msg)["text"]) <= ctx.config.max_text_chars
    # By the last turn both large results are compacted stubs that still carry the document handle.
    last = client.requests[-1]["messages"]
    tools = [m for m in last if m["role"] == "tool"]
    assert all(len(m["content"]) <= cfg.compact_tool_output_chars for m in tools[:-2])
    stub = json.loads(tools[0]["content"])
    assert stub["compacted"] is True and stub["document_id"] == doc_id and stub["url"] == BIG_PAGE
    assert json.loads(tools[1]["content"])["document_id"] == doc_id
    assert "spec row 10:" not in json.dumps(last, ensure_ascii=False)
    assert all(not k.startswith("_") for m in last for k in m)
    # The full text is still in the cache.
    assert DEEP_MARKER in ctx.cache.read_text(doc_id)
    assert result["usage_finalizer"]["model_calls"] == 0  # model finished with JSON: no finalizer call


def test_no_new_research_trigger_and_duplicate_warnings(make_ctx, tmp_path):
    ctx = make_ctx({BIG_PAGE: FakeResponse(b"<html><body>660 Nm</body></html>")})
    fetch = {"role": "assistant", "content": "", "tool_calls": [_call("c", "fetch_url", {"url": BIG_PAGE})]}
    client = ScriptedGLM([fetch, fetch, fetch, {"role": "assistant", "content": json.dumps(FINAL_JSON)}])
    result = run_vehicle({"upstream_record_id": "1"}, {}, client=client, cache=ctx.cache,
                         run_log=RunLog(tmp_path, "b", "1"), config=AgentConfig(field_recovery_enabled=False, max_steps=10, no_new_research_turns=2),
                         tool_config=ctx.config, session=ctx.session)
    assert result["stop_reason"] == "no_new_research" and result["status"] == "no_new_research_finalized"
    assert result["research_steps"] == 3 and result["output"]["summary"] == "compiled"
    # Exact repeats are now answered before dispatch (not detected after a second execution).
    tracking = result["research_tracking"]
    assert (tracking["duplicate_fetches_suppressed"], tracking["duplicate_fetches"]) == (2, 0)
    assert [c["duplicate"] for c in result["tool_calls"]] == [False, True, True]
    assert [c.get("reused_from_step") for c in result["tool_calls"]] == [None, 1, 1]
    second = json.loads(client.requests[2]["messages"][-1]["content"].split("\n[operational note]")[0])
    assert "Exact operation already completed in this run at step 1" in second["operational_note"]
    assert second["reused_from_step"] == 1 and "text_preview" not in second
    assert "[operational note]" in client.requests[2]["messages"][-1]["content"]
    assert client.requests[-1]["tools"] is None and len(client.requests[-1]["messages"]) == 2
    assert len(ctx.session.calls) == 1  # duplicate fetches were served from the cache


# 7 ------------------------------------------------------------------------------------------

class TimeoutSession:
    def __init__(self, exc=requests.ReadTimeout):
        self.exc, self.requests = exc, []

    def post(self, url, headers=None, data=None, timeout=None):
        self.requests.append(url)
        raise self.exc("Read timed out. (read timeout=240)")


def test_explicit_attempt_semantics_for_timeouts(monkeypatch):
    events = []
    session = TimeoutSession()
    client = env_client(monkeypatch, session, GLM_MODEL="glm-5.3-flash")
    assert (client.settings.chat_max_attempts, client.settings.search_max_attempts) == (2, 3)
    client.hook = lambda kind, **data: events.append((kind, data))
    with pytest.raises(GLMError) as info:
        client.chat([{"role": "user", "content": "hi"}])
    assert len(session.requests) == 2  # two TOTAL attempts, not "1 + 2 retries"
    assert [(d["attempt"], d["max_attempts"]) for _, d in events] == [(1, 2), (2, 2)]
    assert all(k == "api_error" and d["timeout"] and d["usage_unknown"] and d["request_kind"] == "chat"
               and d["model"] == "glm-5.3-flash" for k, d in events)
    assert [d["will_retry"] for _, d in events] == [True, False]
    assert info.value.attempts == 2 and info.value.as_dict()["usage_unknown"] is True

    session.requests.clear()
    events.clear()
    with pytest.raises(GLMError):
        client.web_search("g6")
    assert len(session.requests) == 3 and {d["request_kind"] for _, d in events} == {"search"}

    single = env_client(monkeypatch, TimeoutSession(), GLM_MODEL="m", GLM_CHAT_MAX_ATTEMPTS="1",
                        GLM_CHAT_TIMEOUT_S="90")
    assert single.settings.timeout_s == 90.0
    with pytest.raises(GLMError):
        single.chat([{"role": "user", "content": "hi"}])
    assert len(single.session.requests) == 1

    connect = env_client(monkeypatch, TimeoutSession(requests.ConnectTimeout), GLM_MODEL="m")
    seen = []
    connect.hook = lambda kind, **data: seen.append(data)
    with pytest.raises(GLMError):
        connect.chat([{"role": "user", "content": "hi"}])
    assert all(d["timeout"] and not d["usage_unknown"] for d in seen)  # never reached the provider


# 8 ------------------------------------------------------------------------------------------

class MixedSession(ScriptedPostSession):
    """Scripted chat replies; an Exception instance in the queue is raised instead of answered."""

    def post(self, url, headers=None, data=None, timeout=None):
        self.requests.append({"url": url, "headers": headers, "payload": json.loads(data)})
        for suffix, queue in self.routes.items():
            if url.endswith(suffix):
                item = queue.pop(0)
                if isinstance(item, BaseException):
                    raise item
                return item
        return PostResponse(404, {"error": "no route"})


def test_finalizer_timeout_still_writes_durable_partial_result(monkeypatch, tmp_path):
    timeout = requests.ReadTimeout("Read timed out. (read timeout=240)")
    session = MixedSession({
        "chat/completions": [
            chat_reply(tool_call("c1", "search_web", {"query": "XPeng G6 NSGHA"})),
            chat_reply(tool_call("c2", "fetch_url", {"url": BIG_PAGE})),
            timeout, timeout,
        ],
        "web_search": [PostResponse(200, {"search_result": [{"title": "G6", "link": BIG_PAGE, "content": "s"}]})],
    })
    client = env_client(monkeypatch, session, GLM_MODEL="glm-5.3-flash")
    result, runs, _ = run_one(client, tmp_path, AgentConfig(field_recovery_enabled=False, max_steps=2),
                              {BIG_PAGE: FakeResponse(b"<html><body>660 Nm</body></html>")})
    saved = json.loads((runs / "b" / HANDSHAKE_RECORD_ID / "result.json").read_text("utf-8"))
    for r in (result, saved):
        assert r["status"] == "finalization_failed" and r["stop_reason"] == "max_steps"
        assert r["output"] is None and "ReadTimeout" in r["finalizer_error"]
        assert [c["name"] for c in r["tool_calls"]] == ["search_web", "fetch_url"]
        assert len(r["documents"]) == 1 and r["search_api_calls"] == 1
        assert r["usage_research"]["model_calls"] == 2 and r["usage_finalizer"]["model_calls"] == 0
        assert r["api_stats"]["chat_attempts"] == 4 and r["api_stats"]["timeout_count"] == 2
        assert r["api_stats"]["unknown_usage_attempts"] == 2
        assert r["cost_note"] == UNKNOWN_USAGE_NOTE and r["cost_details"]["complete"] is False
        assert r["research_bundle"]["documents"][0]["url"] == BIG_PAGE
        assert r["finalization"]["status"] == "failed" and r["finalization"]["finalizer_input_chars"] > 0
        assert r["metrics"]["timeout_count"] == 2 and r["metrics"]["final_output"] is False
    assert len(chat_payloads(session)) == 4  # 2 research turns + 2 finalizer attempts, nothing more


# 9 ------------------------------------------------------------------------------------------

def test_research_exception_still_writes_durable_partial_result(monkeypatch, tmp_path):
    session = MixedSession({"chat/completions": [
        chat_reply(tool_call("c1", "fetch_url", {"url": BIG_PAGE})),
        PostResponse(500, text="upstream"), PostResponse(503, text="busy"),
    ]})
    client = env_client(monkeypatch, session, GLM_MODEL="glm-5.3-flash")
    result, runs, _ = run_one(client, tmp_path, AgentConfig(field_recovery_enabled=False, max_steps=5),
                              {BIG_PAGE: FakeResponse(b"<html><body>660 Nm</body></html>")})
    saved = json.loads((runs / "b" / HANDSHAKE_RECORD_ID / "result.json").read_text("utf-8"))
    assert saved["status"] == "research_failed" and saved["stop_reason"] == "api_failure"
    assert saved["api_error"]["status"] == 503 and len(saved["api_errors"]) == 2
    assert saved["tool_calls"][0]["name"] == "fetch_url" and saved["documents"]
    assert saved["research_bundle"]["research_summary"]["documents_touched"] == 1
    assert saved["api_stats"]["unknown_usage_attempts"] == 0 and saved["cost_note"] is None
    assert (runs / "b" / HANDSHAKE_RECORD_ID / "documents" / saved["documents"][0] / "body.bin").is_file()

    class Boom(ScriptedGLM):
        def chat(self, messages, tools=None, **kwargs):
            raise ValueError("unexpected")

    other = run_vehicle({"upstream_record_id": "2"}, {}, client=Boom([]), cache=DocumentCache(tmp_path / "c2"),
                        run_log=RunLog(tmp_path / "r2", "b", "2"), config=AgentConfig(field_recovery_enabled=False), tool_config=ToolConfig())
    assert other["status"] == "research_failed" and other["stop_reason"] == "research_exception"
    assert json.loads((tmp_path / "r2" / "b" / "2" / "result.json").read_text("utf-8"))["status"] == "research_failed"


# 10 -----------------------------------------------------------------------------------------

class InterruptingGLM(ScriptedGLM):
    def chat(self, messages, tools=None, **kwargs):
        if not self.messages:
            self.requests.append({"messages": messages, "tools": tools, **kwargs})
            raise KeyboardInterrupt
        return super().chat(messages, tools=tools, **kwargs)


def test_keyboard_interrupt_preserves_partial_artifacts(tmp_path):
    client = InterruptingGLM([{"role": "assistant", "content": "", "tool_calls": [
        _call("c1", "fetch_url", {"url": BIG_PAGE}),
        _call("c2", "store_evidence", {"field": "torque_nm", "value": 660, "source_url": BIG_PAGE, "quote": "Torque 660 Nm"})]}])
    runs = tmp_path / "runs"
    with pytest.raises(KeyboardInterrupt):
        research_one(vehicle44(), {"upstream_record_id": HANDSHAKE_RECORD_ID}, client=client,
                     cache=DocumentCache(runs / "_cache"), runs_dir=runs, batch_id="b", agent_cfg=AgentConfig(field_recovery_enabled=False),
                     tool_cfg=ToolConfig(), pricing=default_pricing("glm-5.3"), level15_source="snapshot",
                     session=FakeSession({BIG_PAGE: FakeResponse(b"<html><body>Torque 660 Nm</body></html>")}))
    run_dir = runs / "b" / HANDSHAKE_RECORD_ID
    saved = json.loads((run_dir / "result.json").read_text("utf-8"))
    assert saved["status"] == "interrupted" and saved["stop_reason"] == "user_cancelled"
    assert saved["evidence"][0]["value"] == 660 and saved["documents"]
    assert saved["metrics"]["tool_calls"] == 2
    assert (run_dir / "documents" / saved["documents"][0] / "body.bin").is_file()
    kinds = [e["kind"] for e in read_events(run_dir / "events.jsonl")]
    assert "interrupted" in kinds and "finalization_started" not in kinds
    assert len(client.requests) == 2  # no model call after the interrupt
    assert client.hook is None


def test_cli_ctrl_c_exits_130_after_persisting(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.setenv("MILO_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(cli, "GLMClient", lambda settings: InterruptingGLM([]))
    code = cli.main(["--model", "glm-5.3-flash", "--runs-dir", str(tmp_path / "runs")])
    assert code == cli.EXIT_INTERRUPTED == 130
    results = list((tmp_path / "runs").glob("*/101122/result.json"))
    assert len(results) == 1 and json.loads(results[0].read_text("utf-8"))["status"] == "interrupted"
    assert "--finalize-existing" in capsys.readouterr().err


# 11-14: the GLM-5.3 baseline failure pattern ----------------------------------------------------

@pytest.fixture
def baseline(tmp_path):
    root = tmp_path / "runs"
    shutil.copytree(FIXTURE, root)
    return root


def test_incomplete_baseline_run_is_reconstructed(baseline):
    cache = DocumentCache(baseline / "_cache")
    before = {p: p.read_bytes() for p in baseline.rglob("*") if p.is_file()}
    runs = load_runs(baseline, BASELINE_BATCH, cache=cache)
    assert len(runs) == 1
    run = runs[0]
    assert run["synthesized"] and run["result_source"] == "events.jsonl" and run["banner"] == INCOMPLETE_BANNER
    assert run["status"] == "incomplete" and run["output"] is None and run["stop_reason"] == "max_steps"
    assert run["research_steps"] == 30 and run["last_successful_step"] == 30 and len(run["tool_calls"]) == 30
    assert run["tool_calls"][-1]["name"] == "fetch_pdf"
    assert run["research_model"] == "glm-5.3" and run["started_at"] and run["latest_event_at"]
    assert run["usage"]["model_calls"] == 30 and run["usage"]["prompt_tokens"] == 1_312_500
    assert run["api_stats"]["timeout_count"] == 3 and run["api_stats"]["unknown_usage_attempts"] == 3
    assert run["finalization"]["status"] == "failed_no_response" and run["finalization"]["timeouts"] == 3
    assert "3 timed out" in run["end_state"]
    assert len(run["evidence"]) == 6 and run["search_api_calls"] == 11
    assert run["cost_note"] == UNKNOWN_USAGE_NOTE and run["cost"]["total_usd"] == pytest.approx(2.0003)
    assert len(run["model_responses"]) == 30 and run["model_responses"][0]["reasoning_content"]
    assert run["effective_config"]["agent"]["max_steps"] == 30 and run["glm_config"]["chat_path"] == "chat/completions"
    assert run["research_bundle"]["research_summary"]["research_model_turns"] == 30
    assert after_unchanged(baseline, before)


def after_unchanged(root, before) -> bool:
    after = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    return after == before


def test_incomplete_run_documents_are_listed(baseline):
    cache = DocumentCache(baseline / "_cache")
    run = load_runs(baseline, BASELINE_BATCH, cache=cache)[0]
    metas = run_document_metas(run, baseline, cache)
    assert len(metas) == 7 and all(m.get("url") for m in metas)
    assert {m["doc_type"] for m in metas} == {"html", "pdf"}
    paired = run_view.batch_document_metas([run], baseline, cache)
    assert [m["document_id"] for m, _ in paired] == run["documents"]
    # Even without the shared cache the documents still show, from the logged `document` events.
    bare = run_document_metas(run, baseline, None)
    assert len(bare) == 7 and all(m.get("url") for m in bare)


def test_benchmark_counts_failed_and_incomplete_runs(baseline):
    cache = DocumentCache(baseline / "_cache")
    run = load_runs(baseline, BASELINE_BATCH, cache=cache)[0]
    m = compute_metrics(run, vehicle44(), cache)
    assert (m["status"], m["final_output"], m["coverage_pct"]) == ("incomplete", False, 0.0)
    assert (m["research_steps"], m["model_calls"], m["tool_calls"], m["documents_opened"]) == (30, 30, 30, 7)
    assert m["tool_calls_by_name"]["search_web"] == 11 and m["search_api_calls"] == 11
    assert (m["timeout_count"], m["unknown_usage_attempts"], m["api_errors"]) == (3, 3, 3)
    assert m["evidence_items"] == 6 and m["prompt_tokens"] == 1_312_500 and m["duration_s"] > 1000
    assert m["finalization_status"] == "failed_no_response" and m["result_source"] == "events.jsonl"
    agg = aggregate([m])
    assert agg["runs_without_final_output"] == 1 and agg["timeout_count_total"] == 3 and not agg["cost_complete"]


def test_results_distinguish_missing_final_json_from_missing_research(baseline):
    run = load_runs(baseline, BASELINE_BATCH)[0]
    assert run_view.has_research(run)
    assert run_view.no_output_message(run).startswith("Not produced —")
    assert not run_view.has_research({"tool_calls": [], "evidence": [], "documents": [], "usage": {}})
    assert "finalization failed" in run_view.no_output_message({"status": "finalization_failed"})
    assert "interrupted" in run_view.no_output_message({"status": "interrupted"})


# 15 -----------------------------------------------------------------------------------------

def test_finalize_existing_run_makes_one_call_and_no_research(baseline, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("recovery must not search or fetch")

    monkeypatch.setattr(requests.Session, "get", no_network)
    run_dir = baseline / BASELINE_BATCH / HANDSHAKE_RECORD_ID
    original_events = (run_dir / "events.jsonl").read_bytes()
    original_input = (run_dir / "input.json").read_bytes()
    plan = plan_recovery(baseline, BASELINE_BATCH, HANDSHAKE_RECORD_ID, cache=DocumentCache(baseline / "_cache"))
    assert plan["prior_result_json"] is False and plan["research_events_sent_to_finalizer"] == 0
    assert (run_dir / "events.jsonl").read_bytes() == original_events

    session = ScriptedPostSession({"chat/completions": [chat_reply({"role": "assistant",
                                                                   "content": json.dumps(FINAL_JSON)})]})
    client = env_client(monkeypatch, session, GLM_MODEL="glm-5.3-flash", GLM_FINALIZER_MODEL="glm-5.3")
    cache = DocumentCache(baseline / "_cache")
    result = finalize_existing_run(baseline, BASELINE_BATCH, HANDSHAKE_RECORD_ID, client=client, cache=cache,
                                   config=AgentConfig(field_recovery_enabled=False),
                                   metrics_fn=lambda r: compute_metrics(r, vehicle44(), cache))
    assert [r["url"].rsplit("/", 1)[-1] for r in session.requests] == ["completions"]  # one call, no search
    sent = session.requests[0]["payload"]
    assert sent["model"] == "glm-5.3" and "tools" not in sent and len(sent["messages"]) == 2
    assert "Step 7: plan the next research action" not in json.dumps(sent, ensure_ascii=False)
    assert result["status"] == "recovered_finalized" and result["recovered"] is True
    assert result["output"]["summary"] == "compiled" and result["recovery"]["finalizer_model"] == "glm-5.3"
    assert result["recovery"]["prior_status"] == "incomplete" and result["recovery"]["searches_performed"] == 0
    assert result["usage_research"]["model_calls"] == 30 and result["usage_finalizer"]["model_calls"] == 1
    assert result["api_stats"]["timeout_count"] == 3 and result["cost_note"] == UNKNOWN_USAGE_NOTE
    assert result["metrics"]["fields_with_value"] == 1

    # History preserved: original events are an unchanged prefix, input untouched, recovery events appended.
    events_now = (run_dir / "events.jsonl").read_bytes()
    assert events_now.startswith(original_events) and (run_dir / "input.json").read_bytes() == original_input
    appended = [json.loads(line) for line in events_now[len(original_events):].decode().splitlines()]
    assert [e["kind"] for e in appended][0] == "recovery_started" and appended[-1]["kind"] == "recovery_finished"
    assert appended[0]["seq"] == 160  # sequence continues after the 159 original events
    stamp = result["recovery"]["stamp"]
    assert (run_dir / "recovery" / stamp / "finalizer_request.json").is_file()
    assert len(list((run_dir / "documents").iterdir())) == 7  # touched documents now exported into the run

    # The UI now loads the recovered result.json.
    loaded = load_runs(baseline, BASELINE_BATCH, cache=cache)[0]
    assert loaded["recovered"] and not loaded.get("synthesized") and loaded["output"]["summary"] == "compiled"

    # A second recovery is refused unless forced; when forced, the previous result.json is preserved.
    with pytest.raises(RecoveryError):
        finalize_existing_run(baseline, BASELINE_BATCH, HANDSHAKE_RECORD_ID, client=client, cache=cache,
                              config=AgentConfig(field_recovery_enabled=False))
    session.routes["chat/completions"].append(chat_reply({"role": "assistant", "content": "not json"}))
    session.routes["chat/completions"].append(chat_reply({"role": "assistant", "content": "still not json"}))
    again = finalize_existing_run(baseline, BASELINE_BATCH, HANDSHAKE_RECORD_ID, client=client, cache=cache,
                                  config=AgentConfig(field_recovery_enabled=False), force=True)
    assert again["status"] == "completed_unparsed"
    preserved = Path(again["recovery"]["prior_result_preserved_as"])
    assert json.loads(preserved.read_text("utf-8"))["status"] == "recovered_finalized"


def test_cli_finalize_existing(baseline, monkeypatch, capsys):
    monkeypatch.setenv("MILO_CACHE_DIR", str(baseline / "_cache"))
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.setenv("GLM_FINALIZER_MODEL", "glm-5.3")
    args = ["--finalize-existing", "--batch-id", BASELINE_BATCH, "--record-id", HANDSHAKE_RECORD_ID,
            "--runs-dir", str(baseline)]
    assert cli.main(args + ["--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["will_search"] is False and plan["finalizer_model"] == "glm-5.3" and plan["evidence_items"] == 6
    assert not (baseline / BASELINE_BATCH / HANDSHAKE_RECORD_ID / "result.json").exists()

    session = ScriptedPostSession({"chat/completions": [chat_reply({"role": "assistant",
                                                                   "content": json.dumps(FINAL_JSON)})]})
    monkeypatch.setenv("GLM_API_KEY", API_KEY)
    monkeypatch.setattr(cli, "GLMClient", lambda settings: GLMClient(settings, session=session, sleeper=lambda s: None))
    assert cli.main(args + ["--model", "glm-5.3-flash"]) == 0
    out = capsys.readouterr().out
    summary = json.loads(out[out.rindex("\n{") + 1:])  # progress lines first, JSON summary last
    assert summary["status"] == "recovered_finalized" and summary["finalizer_model"] == "glm-5.3"
    assert len(session.requests) == 1 and session.requests[0]["payload"]["model"] == "glm-5.3"
    assert cli.main(["--finalize-existing", "--record-id", HANDSHAKE_RECORD_ID]) == 2  # needs --batch-id


# 16 + configuration -----------------------------------------------------------------------------

def test_env_configuration_and_defaults(monkeypatch):
    for name in ("AGENT_MAX_STEPS", "AGENT_NO_NEW_RESEARCH_TURNS", "TOOL_PREVIEW_CHARS"):
        monkeypatch.delenv(name, raising=False)
    cfg = agent_config_from_env()
    assert (cfg.max_steps, cfg.no_new_research_turns) == (12, 2)
    monkeypatch.setenv("AGENT_MAX_STEPS", "30")
    monkeypatch.setenv("AGENT_NO_NEW_RESEARCH_TURNS", "0")
    monkeypatch.setenv("TOOL_PREVIEW_CHARS", "500")
    cfg = agent_config_from_env(thinking="enabled")
    assert (cfg.max_steps, cfg.no_new_research_turns, cfg.thinking) == (30, 0, "enabled")
    assert tool_config_from_env(search_backend="duckduckgo").preview_chars == 500
    settings = GLMSettings(api_key="k", model="glm-5.3-flash")
    public = settings.public()
    assert public["finalizer_model"] == "glm-5.3-flash" and public["finalizer_model_source"] == "GLM_MODEL"
    assert public["chat_max_attempts"] == 2 and "api_key" not in public


def test_cli_dry_run_shows_flash_and_finalizer(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("AGENT_MAX_STEPS", raising=False)
    monkeypatch.setenv("GLM_MODEL", "glm-5.3-flash")
    monkeypatch.setenv("GLM_FINALIZER_MODEL", "glm-5.3")
    assert cli.main(["--dry-run", "--runs-dir", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["research_model"] == "glm-5.3-flash" and out["finalizer_model"] == "glm-5.3"
    assert out["glm_config"]["model"] == "glm-5.3-flash" and out["agent_config"]["max_steps"] == 12
    assert out["pricing"]["input_per_mtok"] == 0.15 and out["pricing_finalizer"]["input_per_mtok"] == 1.40
    assert not any(tmp_path.iterdir())


def test_stub_batch_e2e_successful_run_unchanged(monkeypatch, tmp_path):
    """The pre-refactor happy path: the research model returns JSON itself; no finalizer call is made."""
    session = ScriptedPostSession({
        "chat/completions": [
            chat_reply(tool_call("c1", "search_web", {"query": "XPeng G6 2026 MAX specs"})),
            chat_reply(tool_call("c2", "fetch_url", {"url": BIG_PAGE})),
            chat_reply(tool_call("c3", "store_evidence", {"field": "torque_nm", "value": 660, "source_url": BIG_PAGE})),
            chat_reply({"role": "assistant", "content": json.dumps(FINAL_JSON)}),
        ],
        "web_search": [PostResponse(200, {"search_result": [{"title": "G6", "link": BIG_PAGE, "content": "s"}]})],
    })
    client = env_client(monkeypatch, session, GLM_MODEL="glm-5.3-flash")
    runs = tmp_path / "runs"
    start_batch(runs, "b", client=client, agent_cfg=AgentConfig(field_recovery_enabled=False), tool_cfg=ToolConfig(), pricing={},
                vehicles=[vehicle44()], level15_source="snapshot", level15_note="", selection="one",
                prompt_version="pv")
    result, runs, cache = run_one(client, tmp_path, AgentConfig(field_recovery_enabled=False),
                                  {BIG_PAGE: FakeResponse(b"<html><body>660 Nm</body></html>")})
    assert result["status"] == "completed" and result["stop_reason"] == "model_finished"
    assert result["finalization"] is None and result["usage_finalizer"]["model_calls"] == 0
    assert result["research_steps"] == 4 and result["parse_note"] == "ok"
    assert result["api_stats"] == {**result["api_stats"], "api_attempts": 5, "timeout_count": 0,
                                   "unknown_usage_attempts": 0}
    assert result["cost_note"] is None and result["metrics"]["fields_with_value"] == 1
    loaded = load_runs(runs, "b", cache=cache)
    assert len(loaded) == 1 and loaded[0]["result_source"] == "result.json" and not loaded[0].get("synthesized")


# Provenance / variant discipline (prompt + evidence fields + observational metrics only) ------------

def test_market_provenance_flows_from_evidence_to_finalizer_and_metrics(make_ctx, tmp_path):
    from src.agent import build_user_message
    from src.bundle import build_research_bundle
    from src.schemas import LEVEL2_TARGET_FIELDS
    from src.tools import dispatch

    assert "energy_consumption_kwh_100km" in LEVEL2_TARGET_FIELDS["electric_hybrid"]
    assert "never put kWh/100km" in LEVEL2_TARGET_FIELDS["performance"]["fuel_consumption_combined_l_100km"]
    assert {"curb_weight_kg", "registration_licence_fee", "local_trim_name"} <= {
        k for group in LEVEL2_TARGET_FIELDS.values() for k in group}
    for prompt in (SYSTEM_PROMPT, FINALIZER_SYSTEM_PROMPT):
        assert "prefer the Israeli-market value" in prompt and "A document_id is not an evidence id" in prompt
        assert "provenance_summary" in prompt and "inferred_variant_mappings" in prompt

    payload44 = {"identity": {"government_record_id": "101122", "trim": "MAX", "model_code": "NSGHA"}}
    message = build_user_message(payload44, True, 12, notes={"inferred_local_name": "Core Performance AWD"})
    assert "Operator notes for this variant (context and inferences, not verified facts)" in message
    assert "Operator notes" not in build_user_message({"identity": {"government_record_id": "38626"}}, True)

    ctx = make_ctx()
    ctx.log = lambda kind, **data: events.append({"kind": kind, **data})
    events: list[dict] = []
    # a foreign value is stored, never blocked; the market is the SOURCE's (domain), not the model's word
    for market, value, url in (("IL", 2295, "https://www.example.co.il/g6"), ("MY", 2220, "https://www.example.com.my/g6")):
        cache_source(ctx.cache, url, f"XPeng G6 AWD: kerb weight {value} kg")
        out = dispatch(ctx, "store_evidence", {"field": "curb_weight_kg", "value": value, "market": market,
                                               "variant": "AWD", "source_url": url, "quote": f"kerb weight {value} kg"})
        assert out["stored"] and out["market"] == market
    bundle = build_research_bundle(events, payload44)
    assert bundle["evidence_by_market"] == {"IL": 1, "MY": 1}
    assert [f["market"] for f in bundle["candidate_facts"]["curb_weight_kg"]] == ["IL", "MY"]
    assert bundle["fields_with_multiple_stored_values"] == ["curb_weight_kg"]  # listed, no winner picked
    assert "Core Performance AWD" in bundle["vehicle"]["operator_variant_notes"]["inferred_local_name"]

    output = {"fields": {
        "curb_weight_kg": {"value": 2295, "market": "IL", "provenance": "israel_direct", "evidence_ids": ["e1"],
                           "alternatives": [{"value": 2220, "market": "MY", "evidence_ids": ["e2"]}]},
        "torque_nm": {"value": 660, "provenance": "foreign_direct", "evidence_ids": ["d_0123456789abcdef"]},
        "local_trim_name": {"value": "Core Performance AWD", "provenance": "inferred"},
        "tire_size_front": {"value": None, "provenance": "unresolved"}}}
    m = compute_metrics({"output": output, "evidence": ctx.evidence.items}, {"propulsion": "battery_electric"})
    assert (m["fields_israel_direct"], m["fields_foreign_direct"], m["fields_inferred"], m["fields_unresolved"]) == \
        (1, 1, 1, 1)
    assert m["evidence_with_market"] == 2 and m["cited_evidence_ids"] == 2 and m["cited_ids_not_in_evidence"] == 1


def test_streamlit_app_renders_incomplete_baseline(baseline, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("MILO_RUNS_DIR", str(baseline))
    monkeypatch.setenv("MILO_CACHE_DIR", str(baseline / "_cache"))
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    app = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"), default_timeout=60)
    app.run()
    assert not app.exception, app.exception
    texts = " ".join([*(c.value for c in app.caption), *(w.value for w in app.warning), *(m.value for m in app.markdown)])
    assert "No results to show yet" not in texts
    assert INCOMPLETE_BANNER in texts
    assert "Not produced — the run ended without a result.json" in texts
    assert "did not produce a final result.json" in texts


def test_batch_ids_never_collide_or_overwrite(tmp_path, monkeypatch):
    from datetime import datetime as real_datetime, timezone

    from src.storage import run_log
    from src.storage.run_log import new_batch_id, write_batch

    class FrozenClock(real_datetime):        # both ids in the SAME second: the collision this test is about
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(run_log, "datetime", FrozenClock)
    first = new_batch_id("glm-5.3-flash-one-101122", tmp_path)
    write_batch(tmp_path, first, {"batch_id": first})
    second = new_batch_id("glm-5.3-flash-one-101122", tmp_path)
    assert second != first and second.startswith(first)
    with pytest.raises(FileExistsError):
        write_batch(tmp_path, first, {"batch_id": "overwrite"})
    assert json.loads((tmp_path / first / "batch.json").read_text("utf-8"))["batch_id"] == first
