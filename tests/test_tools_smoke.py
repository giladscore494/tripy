"""Smoke tests for the research tools and the agent loop. No network is used."""

import json
import sys

from conftest import FakeResponse, cache_source, minimal_pdf

from src.agent import AgentConfig, run_vehicle
from src.benchmark import compute_metrics
from src.glm_client import ChatResponse
from src.schemas import TOOL_NAMES, parse_model_output
from src.storage.run_log import RunLog
from src.tools import ToolConfig, dispatch, tool_specs
from src.tools.search import parse_duckduckgo

SPEC_PAGE = """<html><head><title>Corolla Hybrid specs</title>
<script type="application/ld+json">{"@type": "Car", "name": "Corolla", "vehicleEngine": {"torque": "142 Nm"}}</script>
<script id="__NEXT_DATA__" type="application/json">{"props": {"pageProps": {"trunk": "361 l"}}}</script>
<script>window.__INITIAL_STATE__ = {"price": {"amount": 149900, "currency": "ILS"}};</script>
<meta property="og:title" content="Corolla Hybrid">
<style>.x{color:red}</style></head>
<body><h1>Toyota Corolla Hybrid</h1><h2>Dimensions</h2>
<table><caption>Specs</caption><tr><th>Length</th><td>4630 mm</td></tr><tr><th>Width</th><td>1780 mm</td></tr></table>
<dl><dt>Wheelbase</dt><dd>2700 mm</dd><dt>Fuel tank</dt><dd>43 l</dd></dl>
<ul><li>Apple CarPlay</li><li>Android Auto</li></ul>
<p>The combined fuel consumption is 4.5 l/100km under WLTP.</p>
<a href="/brochure.pdf">Brochure</a><a href="https://other.example/review">Review</a>
</body></html>"""


def test_tool_specs_cover_every_tool():
    assert set(TOOL_NAMES) == {
        "search_web", "search_official_domains", "fetch_url", "fetch_pdf", "render_page", "extract_html",
        "extract_tables", "find_in_document", "get_structured_data", "get_cached_document", "store_evidence",
        "report_field_status"}
    assert all(spec["type"] == "function" for spec in tool_specs())


def test_fetch_extract_and_cache_reuse(make_ctx):
    url = "https://www.toyota.co.il/cars/corolla"
    ctx = make_ctx({url: FakeResponse(SPEC_PAGE.encode())})
    first = dispatch(ctx, "fetch_url", {"url": url})
    assert first["cache_hit"] is False and first["status"] == 200
    assert "Toyota Corolla Hybrid" in first["text_preview"]
    assert ".x{color" not in first["text_preview"]
    second = dispatch(ctx, "fetch_url", json.dumps({"url": url}))
    assert second["cache_hit"] is True and second["document_id"] == first["document_id"]
    assert len(ctx.session.calls) == 1
    assert ctx.counters["cache_hits"] == 1 and ctx.counters["cache_misses"] == 1
    meta = ctx.cache.get(first["document_id"])
    assert "set-cookie" not in meta["headers"]

    doc = first["document_id"]
    html = dispatch(ctx, "extract_html", {"document_id": doc})
    assert {"level": 1, "text": "Toyota Corolla Hybrid"} in html["headings"]
    assert any(link["url"] == "https://www.toyota.co.il/brochure.pdf" for link in html["links"])
    assert ["Apple CarPlay", "Android Auto"] in html["lists"]

    tables = dispatch(ctx, "extract_tables", {"document_id": doc})
    sources = {t["source"] for t in tables["tables"]}
    assert sources == {"html_table", "definition_list"}
    assert ["Length", "4630 mm"] in tables["tables"][0]["rows"]

    found = dispatch(ctx, "find_in_document", {"document_id": doc, "query": "combined fuel consumption"})
    assert found["hit_count"] == 1 and "4.5 l/100km" in found["hits"][0]["snippet"]
    fuzzy = dispatch(ctx, "find_in_document", {"document_id": doc, "query": "wheelbase tank"})
    assert fuzzy["hit_count"] >= 1

    structured = dispatch(ctx, "get_structured_data", {"document_id": doc})
    data = structured["data"]
    assert data["json_ld"][0]["vehicleEngine"]["torque"] == "142 Nm"
    assert data["next_data"]["props"]["pageProps"]["trunk"] == "361 l"
    assert data["window_state"]["__INITIAL_STATE__"]["price"]["amount"] == 149900
    assert data["meta"]["og:title"] == "Corolla Hybrid"
    in_json = dispatch(ctx, "find_in_document", {"document_id": doc, "query": "149900", "scope": "structured"})
    assert in_json["hit_count"] == 1

    cached = dispatch(ctx, "get_cached_document", {"key": url})
    assert cached["found"] is True and cached["document_id"] == doc
    assert dispatch(ctx, "get_cached_document", {"key": "https://nowhere.example/"})["found"] is False


def test_fetch_pdf_extracts_text_and_tables_path(make_ctx):
    url = "https://example.com/spec.pdf"
    ctx = make_ctx({url: FakeResponse(minimal_pdf("Max torque 320 Nm"), content_type="application/pdf")})
    result = dispatch(ctx, "fetch_pdf", {"url": url})
    assert result["extraction_path"] == "pdfplumber" and result["pages"] == 1
    assert "Max torque 320 Nm" in result["text_preview"]
    hits = dispatch(ctx, "find_in_document", {"document_id": result["document_id"], "query": "320 nm"})
    assert hits["hit_count"] == 1
    assert dispatch(ctx, "extract_tables", {"document_id": result["document_id"]})["tables_total"] == 0
    # fetch_url detects PDFs too
    assert dispatch(ctx, "fetch_url", {"url": url})["extraction_path"] == "pdfplumber"


def test_technical_validation_only(make_ctx):
    ctx = make_ctx({}, max_response_bytes=10)
    assert "error" in dispatch(ctx, "fetch_url", {"url": "ftp://example.com/x"})
    assert dispatch(ctx, "fetch_url", "{not json")["error"] == "invalid_arguments"
    assert dispatch(ctx, "fetch_url", {})["error"] == "invalid_arguments"
    assert dispatch(ctx, "no_such_tool", {})["error"] == "unknown_tool"
    assert dispatch(ctx, "extract_html", {"document_id": "d_missing"})["error"] == "DocumentNotFound"
    ctx.session.routes["https://big.example/"] = FakeResponse(b"x" * 100, content_type="text/plain")
    big = dispatch(ctx, "fetch_url", {"url": "https://big.example/"})
    assert big["truncated"] is True and big["bytes"] == 10
    # Non-200 pages are stored and returned, not hidden.
    missing = dispatch(ctx, "fetch_url", {"url": "https://example.com/404"})
    assert missing["status"] == 404


def test_store_evidence_accepts_any_retrieved_source_and_rejects_the_rest(make_ctx):
    """No source TYPE is forbidden (a forum and a blog are fine), but the source must have been retrieved and the
    quote must state the value. A URL that was never fetched is not a source."""
    ctx = make_ctx()
    cache_source(ctx.cache, "https://forum.example/t/1", "Toyota Corolla owners: boot space is 520 litres in my car.")
    cache_source(ctx.cache, "https://blog.example", "Corolla review. Luggage capacity 536 l.")
    a = dispatch(ctx, "store_evidence", {"field": "cargo_volume_l", "value": 520, "source_url": "https://forum.example/t/1",
                                         "quote": "boot space is 520 litres"})
    b = dispatch(ctx, "store_evidence", {"field": "cargo_volume_l", "value": "536", "source_url": "https://blog.example",
                                         "quote": "Luggage capacity 536 l"})
    assert (a["evidence_id"], b["evidence_id"]) == ("e1", "e2")
    assert [item["value"] for item in ctx.evidence.items] == [520, "536"]
    assert {item["source_authority"] for item in ctx.evidence.items} == {"unknown"}
    never = dispatch(ctx, "store_evidence", {"field": "cargo_volume_l", "value": 540,
                                             "source_url": "https://never-fetched.example", "quote": "540 litres"})
    assert never["stored"] is False and never["reasons"] == ["source_not_retrieved"] and len(ctx.evidence.items) == 2


def test_search_backends(make_ctx):
    ddg_html = """<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.toyota.co.il%2Fcars&rut=x">Toyota</a>
    <a class="result__snippet">Official site</a></div>
    <div class="result"><a class="result__a" href="https://duckduckgo.com/y.js?ad=1">Ad</a></div>"""
    assert parse_duckduckgo(ddg_html, 5) == [{"title": "Toyota", "url": "https://www.toyota.co.il/cars",
                                              "snippet": "Official site", "site": "www.toyota.co.il", "published": ""}]

    class FakeGLM:
        settings = type("S", (), {"search_engine": "search-prime"})()

        def __init__(self):
            self.calls = []

        def web_search(self, query, count=8, domain=None):
            self.calls.append((query, count, domain))
            return [{"title": f"{domain or 'web'} result", "url": f"https://{domain or 'any.example'}/p",
                     "snippet": "", "site": "", "published": ""}]

    ctx = make_ctx(search_backend="glm")
    ctx.glm = FakeGLM()
    out = dispatch(ctx, "search_web", {"query": "corolla torque", "max_results": "5"})
    assert out["cache_hit"] is False and out["results"][0]["url"] == "https://any.example/p"
    assert dispatch(ctx, "search_web", {"query": "corolla torque", "max_results": 5})["cache_hit"] is True
    assert len(ctx.glm.calls) == 1

    official = dispatch(ctx, "search_official_domains", {"query": "corolla"})
    assert official["domains_used"][0] == "toyota.co.il"
    assert {c[2] for c in ctx.glm.calls[1:]} == set(official["domains_used"])
    custom = dispatch(ctx, "search_official_domains", {"query": "corolla", "domains": "example.org"})
    assert custom["domains_used"] == ["example.org"]


def test_render_page_unavailable_is_reported(make_ctx, monkeypatch):
    from src import tools
    from src.tools import render

    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    monkeypatch.setattr(tools, "_PROBED", {})
    result = dispatch(make_ctx(), "render_page", {"url": "https://example.com/spa"})
    assert result["error"] == "tool_unavailable" and "Playwright" in result["message"]     # refused, not attempted
    assert "render_page" not in [s["function"]["name"] for s in tool_specs()]             # never offered to a model
    assert render.render_page(make_ctx(), "https://example.com/spa")["error"] == "render_unavailable"


def test_optional_capabilities_shape_the_tool_schema(monkeypatch):
    from src import tools
    from src.agent import SYSTEM_PROMPT, research_system_prompt

    monkeypatch.setattr(tools, "_PROBED", {"render_page": True})
    names = [s["function"]["name"] for s in tool_specs()]
    assert "render_page" in names and research_system_prompt() == SYSTEM_PROMPT
    monkeypatch.setattr(tools, "_PROBED", {"render_page": False})
    assert "render_page" not in [s["function"]["name"] for s in tool_specs()]
    assert "render_page" not in research_system_prompt() and "fetch_pdf" in research_system_prompt()
    monkeypatch.setenv("DISABLED_TOOLS", "search_official_domains")
    assert "search_official_domains" not in [s["function"]["name"] for s in tool_specs()]
    assert len(tools.all_tool_specs()) == 12


def test_parse_model_output_is_lenient():
    assert parse_model_output('{"a": 1}') == ({"a": 1}, "ok")
    assert parse_model_output('Here:\n```json\n{"a": 2}\n```')[0] == {"a": 2}
    assert parse_model_output("prefix {\"a\": 3} suffix")[0] == {"a": 3}
    assert parse_model_output("no json here") == (None, "not_json")


class ScriptedGLM:
    """Plays back a fixed list of assistant messages."""

    model = "glm-test"

    def __init__(self, messages):
        self.messages = list(messages)
        self.requests = []

    def chat(self, messages, tools=None, **kwargs):
        self.requests.append({"messages": messages, "tools": tools, **kwargs})
        message = self.messages.pop(0)
        return ChatResponse(message=message, finish_reason="stop",
                            usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120})


def _call(call_id, name, args):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def test_agent_loop_end_to_end(make_ctx, tmp_path):
    url = "https://www.toyota.co.il/cars/corolla"
    ctx = make_ctx({url: FakeResponse(SPEC_PAGE.encode())})
    final = {
        "vehicle_id": "38626", "summary": "found specs",
        "fields": {"length_mm": {"value": 4630, "unit": "mm", "evidence_ids": ["e1"]},
                   "cargo_volume_l": {"value": [361, 370], "notes": "two figures"},
                   "torque_nm": {"value": None},
                   "boot_floor_height_mm": {"value": 700}},
        "conflicts": [{"field": "cargo_volume_l", "values": [361, 370], "model_comment": "VDA vs brochure"}],
        "additional_findings": [{"topic": "x", "finding": "y"}],
        "level3": {"recalls": {"finding": "none found"}},
    }
    client = ScriptedGLM([
        {"role": "assistant", "content": "", "tool_calls": [_call("c1", "fetch_url", {"url": url})]},
        {"role": "assistant", "content": "", "tool_calls": [
            _call("c2", "extract_tables", {"document_id": "d_will_be_replaced"}),
            _call("c3", "store_evidence", {"field": "length_mm", "value": 4630, "source_url": url, "quote": "4630 mm"}),
        ]},
        {"role": "assistant", "content": "```json\n" + json.dumps(final) + "\n```"},
    ])
    log = RunLog(tmp_path / "runs", "b1", "38626")
    row = {"upstream_record_id": "38626", "tozar": "טויוטה"}
    result = run_vehicle(row, {"identity": {"manufacturer": "טויוטה"}}, client=client, cache=ctx.cache,
                         run_log=log, config=AgentConfig(field_recovery_enabled=False, max_steps=5), tool_config=ToolConfig(),
                         vehicle_meta={"manufacturer": "טויוטה", "propulsion": "hybrid"}, batch_id="b1", ordinal=1,
                         session=ctx.session)
    assert result["status"] == "completed" and result["output"]["summary"] == "found specs"
    assert [c["name"] for c in result["tool_calls"]] == ["fetch_url", "extract_tables", "store_evidence"]
    assert result["tool_calls"][1]["error"] == "DocumentNotFound"  # bad id is reported back, not fatal
    assert result["evidence"][0]["value"] == 4630
    assert result["usage"]["total_tokens"] == 360
    # The tool results went back to the model as tool messages.
    last_request = client.requests[-1]["messages"]
    assert [m["role"] for m in last_request].count("tool") == 3

    metrics = compute_metrics(result, {"propulsion": "hybrid"}, ctx.cache,
                              pricing={"input_per_mtok": 1.0, "output_per_mtok": 2.0, "web_search_per_call": 0.01})
    assert metrics["fields_returned"] == 4 and metrics["fields_with_value"] == 3
    assert metrics["target_filled"] == 2 and metrics["extra_field_names"] == ["boot_floor_height_mm"]
    assert metrics["conflicts_reported"] == 1 and metrics["level3_topics"] == 1
    assert metrics["unique_sources"] == 1 and metrics["cost_usd"] == round((300 + 60 * 2) / 1e6, 6)
    events = (tmp_path / "runs" / "b1" / "38626" / "events.jsonl").read_text("utf-8").splitlines()
    kinds = [json.loads(e)["kind"] for e in events]
    assert kinds[0] == "run_started" and kinds[-1] == "run_finished" and "evidence" in kinds


def test_agent_finalizes_at_step_budget_and_keeps_unparsed_text(make_ctx, tmp_path):
    ctx = make_ctx()
    loop_call = {"role": "assistant", "content": "", "tool_calls": [_call("c", "search_web", {"query": "x"})]}
    ctx.config.search_backend = "duckduckgo"
    client = ScriptedGLM([loop_call, loop_call, {"role": "assistant", "content": "plain text answer"},
                          {"role": "assistant", "content": "still not json"}])
    result = run_vehicle({"upstream_record_id": "1"}, {}, client=client, cache=ctx.cache,
                         run_log=RunLog(tmp_path, "b", "1"), config=AgentConfig(field_recovery_enabled=False, max_steps=2),
                         tool_config=ctx.config, batch_id="b", session=ctx.session)
    assert result["status"] == "max_steps_finalized"
    assert result["output"] is None and result["raw_final_text"] == "plain text answer"
    assert client.requests[2]["tools"] is None  # finalization turn offers no tools
