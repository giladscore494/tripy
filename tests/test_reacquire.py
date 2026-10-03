"""RECOVERY_MODE=reacquire (PR #31 Part F): per recovery cluster a short targeted acquisition episode, the harvest of
its new documents, grounded candidates and adjudication. The recovery model only searches and fetches; nothing is
stored except through adjudication + Evidence Admission. Scripted GLM / fake HTTP only."""

import json

import pytest

from fixtures import corolla_tail as tail
from test_adjudication import AdjPhaseClient
from test_phase_contracts import EU, GATE_OFF, fetch, run, say, turn
from test_tools_smoke import _call

from src.agent import reacquire_system_prompt
from src.glm_client import ChatResponse, GLMError

pytestmark = pytest.mark.recovery_mode("reacquire")

CACHED_TOOLS = {"store_evidence", "report_field_status", "inspect_document_for_fields", "find_in_document",
                "extract_tables", "extract_html", "get_structured_data", "get_cached_document"}


class ReacquireClient(AdjPhaseClient):
    """Research / sweep / adjudication as AdjPhaseClient; re-acquisition episodes from `episodes(packet, turn)`, which
    returns an assistant message or an exception to raise."""

    def __init__(self, research, episodes, **kw):
        super().__init__(research, **kw)
        self.episodes = episodes
        self.reacquire_requests: list[dict] = []

    def chat(self, messages, tools=None, **kwargs):
        if messages[0]["content"] == reacquire_system_prompt():
            self.kinds.append("reacquire")
            packet = json.loads(messages[1]["content"].split("\n", 1)[1])
            turn_no = sum(1 for m in messages if m.get("role") == "assistant") + 1
            self.reacquire_requests.append({"messages": messages, "tools": tools, "packet": packet, **kwargs})
            reply = self.episodes(packet, turn_no)
            if isinstance(reply, BaseException):
                raise reply
            return ChatResponse(message=reply, finish_reason="stop",
                                usage={"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55})
        return super().chat(messages, tools, **kwargs)


def test_a_missing_field_is_reacquired_harvested_adjudicated_and_resolved(tmp_path):
    def episodes(packet, turn_no):
        if turn_no == 1:
            return turn(_call("r1", "search_web", {"query": "Toyota Corolla Touring Sports 2024 length"}),
                        _call("r2", "fetch_url", {"url": tail.CARTUBE}))
        return say({"done": True, "reason": "spec page fetched"})

    client = ReacquireClient([fetch("a", EU), say({"done": True, "reason": "enough"})], episodes,
                             search_url=tail.CARTUBE)
    result, events = run(tmp_path, client, acquisition_mode="contract", requested_fields=["fuel_tank_l", "length_mm"],
                         **GATE_OFF)
    rec = result["field_recovery"]
    assert rec["mode"] == "reacquire" and rec["queue"] == ["length_mm"]
    episode = rec["attempts"][0]
    assert episode["cluster"] == "technical_spec" and episode["fields"] == ["length_mm"]
    assert episode["searches"] <= episode["search_budget"] and episode["fetches"] == 1
    assert episode["new_useful_documents"] == 1 and episode["new_candidates"] >= 1
    assert episode["adjudication_accepted"] >= 1 and episode["admitted"] >= 1
    assert episode["fields_resolved"] == ["length_mm"] and rec["fields_recovered"] == ["length_mm"]
    assert {"searches", "fetches", "new_useful_documents", "new_candidates", "grounded_items",
            "adjudication_accepted", "admitted", "fields_resolved", "model_calls", "tokens"} <= set(episode)
    stored = [e["evidence"] for e in events if e["kind"] == "evidence" and e["evidence"]["field"] == "length_mm"]
    assert [s["value"] for s in stored] == [4650]
    # the recovery model only ever had the acquisition tools; the packet carries the cluster's task
    for request in client.reacquire_requests:
        names = {t["function"]["name"] for t in request["tools"] or []}
        assert names and names <= {"search_web", "search_official_domains", "fetch_url", "fetch_pdf", "render_page"}
        assert not names & CACHED_TOOLS
    packet = client.reacquire_requests[0]["packet"]
    assert [f["field"] for f in packet["missing_fields"]] == ["length_mm"] and packet["source_type"]
    assert EU in packet["already_fetched_urls"]
    # the episode's store_evidence calls are adjudication's synthetic ones (never the recovery model's)
    recovery_stores = [e for e in events if e["kind"] == "tool_call" and e["name"] == "store_evidence"
                       and e.get("phase") == "field_recovery"]
    assert recovery_stores and all(str(e["call_id"]).startswith("adj") for e in recovery_stores)
    # diagnostics see the new events
    from src import diagnostics as D
    diag = D.recovery_summary(events)
    assert diag["attempts"] == 1 and diag["mode"] == "reacquire" and diag["reacquire_fields_resolved"] == 1
    assert diag["fetches"] == 1


def test_a_failing_cluster_does_not_stop_the_next_one(tmp_path):
    seen = []

    def episodes(packet, turn_no):
        seen.append(packet["cluster"])
        if len(seen) == 1:
            error = GLMError("read timeout")
            error.timeout = True
            return error
        return say({"done": True, "reason": "nothing more"})

    client = ReacquireClient([fetch("a", tail.FORUM), say({"done": True, "reason": "enough"})], episodes)
    result, events = run(tmp_path, client, acquisition_mode="contract",
                         requested_fields=["length_mm", "top_speed_kmh", "list_price"], **GATE_OFF)
    rec = result["field_recovery"]
    assert seen == ["technical_spec", "performance", "commercial"]
    assert [a["failed"] for a in rec["attempts"]] == [True, False, False]
    assert rec["failed_attempts"] == 1 and not rec["api_failure_stop"]
    failed = [e for e in events if e["kind"] == "field_recovery_failed"]
    assert len(failed) == 1 and failed[0]["cluster"] == "technical_spec"


def test_two_consecutive_api_failures_stop_recovery(tmp_path):
    seen = []

    def episodes(packet, turn_no):
        seen.append(packet["cluster"])
        return GLMError("HTTP 503", status=503)

    client = ReacquireClient([fetch("a", tail.FORUM), say({"done": True, "reason": "enough"})], episodes)
    result, events = run(tmp_path, client, acquisition_mode="contract",
                         requested_fields=["length_mm", "top_speed_kmh", "list_price"], **GATE_OFF)
    rec = result["field_recovery"]
    assert seen == ["technical_spec", "performance"]
    assert rec["api_failure_stop"] and rec["stopped"] == "api_failure" and rec["failed_attempts"] == 2
    assert any(e["kind"] == "field_recovery_api_failure_stop" for e in events)
    assert result["status"].endswith("finalized") or result["status"] == "completed"


def test_an_episode_without_a_new_usable_document_stops_and_fetches_are_capped(tmp_path):
    urls = [f"https://www.example.org/missing-{i}" for i in range(5)]

    def episodes(packet, turn_no):
        return turn(*[_call(f"f{i}", "fetch_url", {"url": u}) for i, u in enumerate(urls)])

    client = ReacquireClient([fetch("a", EU), say({"done": True, "reason": "enough"})], episodes)
    result, events = run(tmp_path, client, acquisition_mode="contract", requested_fields=["fuel_tank_l", "length_mm"],
                         **GATE_OFF)
    episode = result["field_recovery"]["attempts"][0]
    assert episode["turns"] == 1 and episode["stop"] == "no_new_usable_document"
    assert episode["fetches"] == 3 and episode["fetch_refused"] == 2
    blocked = [e for e in events if e["kind"] == "tool_blocked" and e.get("reason") == "fetch_budget_exhausted"]
    assert len(blocked) == 2


def test_a_conflicting_field_asks_for_the_deciding_source():
    from src.agent import reacquire_packet

    packet = reacquire_packet(cluster="commercial", fields=["list_price"], specs=[{"name": "list_price",
                                                                                   "description": "List price"}],
                              evaluation={"list_price": {"state": "conflicting"}}, identity={}, target_market="IL",
                              source_type="target-market", site_urls=[], fetched_urls=[], negative={},
                              search_budget=4, fetch_budget=3, turns=2)
    assert packet["conflicting_fields"] == ["list_price"] and "DECIDING source" in packet["conflict_instruction"]
