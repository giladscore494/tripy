"""Run provenance: effective GLM config, raw API errors, search-call count, pricing and CLI dry run.

A real GLMClient is driven through a fake HTTP session, so no network is used.
"""

import json

from conftest import FakeResponse, FakeSession

from src import cli
from src.agent import AgentConfig
from src.benchmark import HANDSHAKE_RECORD_ID, benchmark_vehicles, research_one, start_batch
from src.glm_client import GLMClient, GLMError, GLMSettings
from src.pricing import compute_cost, default_pricing
from src.storage.cache import DocumentCache
from src.storage.run_log import load_events
from src.tools import ToolConfig

API_KEY = "test-key-should-never-be-written-0123456789"  # fake canary  # pragma: allowlist secret


class PostResponse:
    def __init__(self, status: int, payload=None, text: str | None = None, headers=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)
        self.headers = headers or {"Content-Type": "application/json"}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class ScriptedPostSession:
    """Answers POSTs per endpoint from queues and records every request."""

    def __init__(self, routes: dict[str, list[PostResponse]]):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.requests = []

    def post(self, url, headers=None, data=None, timeout=None):
        self.requests.append({"url": url, "headers": headers, "payload": json.loads(data)})
        for suffix, queue in self.routes.items():
            if url.endswith(suffix):
                return queue.pop(0)
        return PostResponse(404, {"error": "no route"})


def chat_reply(message: dict, cached: int = 0) -> PostResponse:
    return PostResponse(200, {"id": "resp-1", "model": "glm-5.3", "choices": [{"message": message, "finish_reason": "stop"}],
                              "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
                                        "prompt_tokens_details": {"cached_tokens": cached}}})


def tool_call(call_id: str, name: str, args: dict) -> dict:
    return {"role": "assistant", "content": "", "reasoning_content": "thinking...",
            "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def make_client(session, **overrides) -> GLMClient:
    settings = GLMSettings(api_key=API_KEY, model="glm-5.3", base_url="https://glm.example/api/paas/v4", **overrides)
    return GLMClient(settings, session=session, sleeper=lambda s: None)


def test_pricing_defaults_and_cost(monkeypatch):
    for name in ("GLM_PRICE_INPUT_PER_MTOK", "GLM_PRICE_OUTPUT_PER_MTOK", "GLM_PRICE_WEB_SEARCH_PER_CALL"):
        monkeypatch.delenv(name, raising=False)
    assert (default_pricing("glm-5.3")["input_per_mtok"], default_pricing("glm-5.3")["output_per_mtok"]) == (1.40, 4.40)
    assert (default_pricing("glm-5.3-flash")["input_per_mtok"], default_pricing("glm-5.3-flash")["output_per_mtok"]) == (0.15, 0.50)
    assert (default_pricing("GLM-5.3-FlashX")["input_per_mtok"], default_pricing("glm-5.3-flashx")["output_per_mtok"]) == (0.37, 1.25)
    assert default_pricing("glm-5.3")["web_search_per_call"] == 0.01
    unknown = default_pricing("glm-unknown")
    assert unknown["input_per_mtok"] is None and unknown["web_search_per_call"] == 0.01
    monkeypatch.setenv("GLM_PRICE_INPUT_PER_MTOK", "2.5")
    assert default_pricing("glm-5.3")["input_per_mtok"] == 2.5
    cost = compute_cost({"prompt_tokens": 1_000_000, "completion_tokens": 500_000}, 3, default_pricing("glm-5.3-flash"))
    assert cost == {"tokens_usd": 2.5 + 0.25, "web_search_usd": 0.03, "total_usd": 2.78}
    assert compute_cost({"prompt_tokens": 10}, 2, unknown)["tokens_usd"] is None


def test_client_keeps_raw_errors_and_full_search_fields():
    events = []
    session = ScriptedPostSession({
        "/custom/search": [
            PostResponse(503, text="upstream busy", headers={"X-Request-Id": "r-1", "Authorization": "x"}),
            PostResponse(200, {"search_result": [{"title": "G6", "content": "c", "link": "https://xpeng.com/g6",
                                                  "media": "XPENG", "icon": "https://i", "refer": "1",
                                                  "publish_date": "2026-01-01"}]}),
        ],
        "chat/completions": [PostResponse(400, {"error": {"code": "1210", "message": "bad param"}})],
    })
    client = make_client(session, search_path="custom/search", search_engine="search-prime")
    client.hook = lambda kind, **data: events.append((kind, data))
    results = client.web_search("xpeng g6 torque", count=99, domain="xpeng.com", recency="oneYear")
    assert results == [{"title": "G6", "url": "https://xpeng.com/g6", "snippet": "c", "site": "XPENG",
                        "published": "2026-01-01", "icon": "https://i", "refer": "1"}]
    sent = session.requests[-1]["payload"]
    assert sent == {"search_engine": "search-prime", "search_query": "xpeng g6 torque", "count": 50,
                    "search_recency_filter": "oneYear", "search_domain_filter": "xpeng.com"}
    assert session.requests[-1]["url"] == "https://glm.example/api/paas/v4/custom/search"
    assert [k for k, _ in events] == ["api_error", "api_call"]
    assert events[0][1]["body"] == "upstream busy" and events[0][1]["headers"] == {"X-Request-Id": "r-1"}

    try:
        client.chat([{"role": "user", "content": "hi"}])
    except GLMError as exc:
        raw = exc.as_dict()
    assert raw["status"] == 400 and json.loads(raw["body"])["error"]["code"] == "1210"
    assert raw["endpoint"] == "chat/completions"


def test_run_records_effective_config_trace_and_cost(tmp_path):
    page = "https://www.xpeng.com/g6"
    glm_session = ScriptedPostSession({
        "chat/completions": [
            chat_reply(tool_call("c1", "search_web", {"query": "XPeng G6 2026 MAX specs"})),
            chat_reply(tool_call("c2", "fetch_url", {"url": page}), cached=400),
            chat_reply(tool_call("c3", "store_evidence", {"field": "torque_nm", "value": 660, "source_url": page})),
            chat_reply({"role": "assistant", "content": json.dumps({"summary": "ok", "fields": {
                "torque_nm": {"value": 660, "unit": "Nm", "evidence_ids": ["e1"]}}})}),
        ],
        "web_search": [PostResponse(200, {"search_result": [{"title": "G6", "link": page, "content": "specs"}]})],
    })
    client = make_client(glm_session)
    vehicle = next(v for v in benchmark_vehicles() if v["upstream_record_id"] == HANDSHAKE_RECORD_ID)
    row = {"upstream_record_id": HANDSHAKE_RECORD_ID, "tozar": vehicle["manufacturer"]}
    agent_cfg = AgentConfig(field_recovery_enabled=False, max_steps=6, max_tokens=2048, thinking="enabled", extra_body={"custom_flag": True})
    tool_cfg = ToolConfig(search_backend="glm")
    pricing = default_pricing("glm-5.3")
    runs = tmp_path / "runs"
    cache = DocumentCache(runs / "_cache")
    batch = start_batch(runs, "b1", client=client, agent_cfg=agent_cfg, tool_cfg=tool_cfg, pricing=pricing,
                        vehicles=[vehicle], level15_source="snapshot", level15_note="", selection="one",
                        prompt_version="pv")
    result = research_one(vehicle, row, client=client, cache=cache, runs_dir=runs, batch_id="b1",
                          agent_cfg=agent_cfg, tool_cfg=tool_cfg, pricing=pricing, level15_source="snapshot",
                          session=FakeSession({page: FakeResponse(b"<html><body><h1>G6</h1>660 Nm</body></html>")}))

    assert result["status"] == "completed" and result["output"]["summary"] == "ok"
    cfg = result["glm_config"]
    for key, value in {"model": "glm-5.3", "base_url": "https://glm.example/api/paas/v4",
                       "chat_path": "chat/completions", "search_path": "web_search", "search_engine": "search-prime",
                       "thinking": {"type": "enabled"}, "max_tokens": 2048, "search_backend": "glm"}.items():
        assert cfg[key] == value, key
    assert cfg["extra_request_body"] == {"custom_flag": True, "thinking": {"type": "enabled"}}
    assert cfg["recorded_at"] and "api_key" not in cfg
    assert batch["glm_config"]["thinking"] == {"type": "enabled"}

    chat_payloads = [r["payload"] for r in glm_session.requests if r["url"].endswith("chat/completions")]
    assert all(p["thinking"] == {"type": "enabled"} and p["max_tokens"] == 2048 and p["custom_flag"] for p in chat_payloads)
    assert chat_payloads[0]["tool_choice"] == "auto"

    assert result["search_api_calls"] == 1
    assert result["usage"]["prompt_tokens"] == 4000 and result["usage"]["cached_tokens"] == 400
    assert result["cost"] == {"tokens_usd": round((4000 * 1.40 + 400 * 4.40) / 1e6, 6), "web_search_usd": 0.01,
                              "total_usd": round((4000 * 1.40 + 400 * 4.40) / 1e6 + 0.01, 6)}
    assert result["metrics"]["search_api_calls"] == 1 and result["metrics"]["cost_search_usd"] == 0.01

    run_dir = runs / "b1" / HANDSHAKE_RECORD_ID
    events = load_events(runs, "b1", HANDSHAKE_RECORD_ID)
    kinds = [e["kind"] for e in events]
    assert kinds.count("api_call") == 5 and "search" in kinds and "document" in kinds and "evidence" in kinds
    assert events[0]["glm_config"]["chat_path"] == "chat/completions"
    search_result = next(e for e in events if e["kind"] == "tool_result" and e["name"] == "search_web")
    assert search_result["result"]["results"][0]["url"] == page
    model_turn = next(e for e in events if e["kind"] == "model_response")
    assert model_turn["reasoning_content"] == "thinking..." and model_turn["latency_ms"] is not None
    exported = list((run_dir / "documents").glob("*/body.bin"))
    assert len(exported) == 1 and b"660 Nm" in exported[0].read_bytes()
    for path in runs.rglob("*"):
        if path.is_file():
            assert API_KEY not in path.read_text("utf-8", errors="ignore"), path


def test_run_preserves_raw_api_error(tmp_path):
    session = ScriptedPostSession({"chat/completions": [PostResponse(401, {"error": {"code": "1000", "message": "auth"}})]})
    client = make_client(session)
    vehicle = next(v for v in benchmark_vehicles() if v["upstream_record_id"] == HANDSHAKE_RECORD_ID)
    result = research_one(vehicle, {"upstream_record_id": HANDSHAKE_RECORD_ID}, client=client,
                          cache=DocumentCache(tmp_path / "c"), runs_dir=tmp_path, batch_id="b",
                          agent_cfg=AgentConfig(field_recovery_enabled=False), tool_cfg=ToolConfig(), pricing=default_pricing("glm-5.3"),
                          level15_source="snapshot")
    assert result["status"] == "research_failed" and result["stop_reason"] == "api_failure"
    assert result["api_error"]["status"] == 401 and "1000" in result["api_error"]["body"]
    assert result["api_errors"][0]["status"] == 401
    saved = json.loads((tmp_path / "b" / HANDSHAKE_RECORD_ID / "result.json").read_text("utf-8"))
    assert saved["api_error"]["status"] == 401
    assert client.hook is None  # the per-run hook is removed afterwards


def test_cli_dry_run_for_vehicle_44_needs_no_key(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("GLM_EXTRA_BODY", raising=False)
    assert cli.main(["--dry-run", "--model", "glm-5.3", "--thinking", "enabled", "--runs-dir", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["vehicle"].startswith("#44 ") and "G6 2026 MAX (101122)" in out["vehicle"]
    assert out["level15_identity"]["model_code"] == "NSGHA"
    assert out["level15_engine_drivetrain"]["power_hp"] == 486
    assert out["level15_engine_drivetrain"]["drivetrain_normalized"] == "awd"
    assert out["api_key_present"] is False and out["level15_source"] == "snapshot"
    assert out["glm_config"]["thinking"] == {"type": "enabled"}
    assert out["pricing"]["input_per_mtok"] == 1.40
    assert not any(tmp_path.iterdir())  # a dry run writes nothing


def test_level3_is_opt_in_everywhere(monkeypatch):
    from src.agent import agent_config_from_env, build_user_message

    assert AgentConfig().include_level3 is False
    assert agent_config_from_env(env=lambda name: None).include_level3 is False
    assert agent_config_from_env(env=lambda name: "true" if name == "INCLUDE_LEVEL3" else None).include_level3
    assert "Level 3 research is disabled for this run" in build_user_message({}, False)
    for name in ("GLM_MODEL", "INCLUDE_LEVEL3"):
        monkeypatch.delenv(name, raising=False)
    assert cli._agent_cfg(cli._parse([]), {}).include_level3 is False
    assert cli._agent_cfg(cli._parse(["--level3"]), {}).include_level3 is True
    monkeypatch.setenv("INCLUDE_LEVEL3", "true")
    assert cli._agent_cfg(cli._parse([]), {}).include_level3 is True
    assert cli._agent_cfg(cli._parse(["--no-level3"]), {}).include_level3 is False


def test_israeli_cadillac_domain_is_an_official_hint_not_an_allowlist():
    from src.tools.search import default_domains

    domains = default_domains({"manufacturer": "קאדילאק"})
    assert domains[0] == "cadillac.co.il" and "cadillac.com" in domains
