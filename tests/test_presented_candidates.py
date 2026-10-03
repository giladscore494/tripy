"""Candidates count as PRESENTED to a model only once a model call that carried them returned. A failed sweep chunk
(e.g. a ReadTimeout) or a failed cluster call showed its candidates to no model: they stay fresh, so tail recovery
still gives their cluster its free local-only pass. Scripted GLM / fake HTTP only: no network."""

import json

from fixtures import corolla_tail as tail
from test_phase_contracts import EU, GATE_OFF, PhaseClient, _timeout, fetch, run, say
from test_tools_smoke import _call

from src.fields import resolve_requested_fields
from src.tail_planner import candidate_key, fresh_candidates, presented_keys, triage

SPECS = resolve_requested_fields(tail.FIELDS, propulsion="hybrid")


def _sweep_starts(events):
    return [e for e in events if e["kind"] == "document_sweep_started"]


def _presented(events, source):
    return [e for e in events if e["kind"] == "candidates_presented" and e["source"] == source]


def _two_chunk_run(tmp_path, fail_first=True):
    def sweep(call_number):
        if call_number == 1 and fail_first:
            raise _timeout()
        return say({"reviewed": []})

    client = PhaseClient([fetch("a", EU, tail.CARTUBE), say({"done": True, "reason": "x"})], sweep=sweep)
    return run(tmp_path, client, acquisition_mode="contract", document_sweep_max_fields=4, **GATE_OFF)


def test_a_failed_sweep_chunk_presents_nothing_and_its_cluster_keeps_the_local_pass(tmp_path):
    result, events = _two_chunk_run(tmp_path)
    starts = _sweep_starts(events)
    assert len(starts) >= 2
    chunk1, chunk2 = starts[0], starts[1]
    assert "presented_candidate_keys" not in chunk1 and chunk1["offered_candidate_keys"]
    presented = _presented(events, "document_sweep")
    assert [p["chunk"]["index"] for p in presented] == [s["chunk"]["index"] for s in starts[1:]
                                                        if s["offered_candidate_keys"]]
    assert presented[0]["presented_candidate_keys"] == chunk2["offered_candidate_keys"]
    # presented_keys as tail recovery saw it before its first attempt
    queue_seq = next(e["seq"] for e in events if e["kind"] == "field_retry_queue")
    before_recovery = [e for e in events if e["seq"] <= queue_seq]
    shown = presented_keys(before_recovery)
    assert set(chunk2["offered_candidate_keys"]) <= shown
    assert not set(chunk1["offered_candidate_keys"]) & shown
    # chunk 1's fields with offered candidates are local material and get the local-only pass first
    offered_fields = {k.split("|")[0] for k in chunk1["offered_candidate_keys"]}
    first_start = {}
    for e in events:
        if e["kind"] == "cluster_recovery_started":
            for f in e["fields"]:
                first_start.setdefault(f, e)
    checked = [f for f in offered_fields if f in first_start]
    assert checked
    for f in checked:
        assert first_start[f]["triage"][f] == "candidate_rich_local"
        assert first_start[f]["mode"] == "local_only"
    assert result["document_sweep"]["failed_chunk_candidates_kept_fresh"] == len(set(chunk1["offered_candidate_keys"]))
    finished = next(e for e in events if e["kind"] == "document_sweep_finished")
    assert finished["failed_chunk_candidates_kept_fresh"] == len(set(chunk1["offered_candidate_keys"]))
    from src import diagnostics as D
    diag = D.vehicle_diagnostics(events, result=result)
    assert diag["document_sweep"]["summary"]["failed_chunk_candidates_kept_fresh"] == \
        len(set(chunk1["offered_candidate_keys"]))
    assert D.vehicle_row(diag)["sweep_failed_chunk_candidates_kept_fresh"] == len(set(chunk1["offered_candidate_keys"]))


def test_a_successful_sweep_chunk_presents_its_offered_candidates(tmp_path):
    result, events = _two_chunk_run(tmp_path, fail_first=False)
    starts = [s for s in _sweep_starts(events) if s["offered_candidate_keys"]]
    presented = _presented(events, "document_sweep")
    assert [p["presented_candidate_keys"] for p in presented] == [s["offered_candidate_keys"] for s in starts]
    queue_seq = next(e["seq"] for e in events if e["kind"] == "field_retry_queue")
    shown = presented_keys([e for e in events if e["seq"] <= queue_seq])
    assert shown == {k for s in starts for k in s["offered_candidate_keys"]}
    assert result["document_sweep"]["failed_chunk_candidates_kept_fresh"] == 0


class FailingRecovery(PhaseClient):
    """Cluster recovery: the first call of every attempt raises (sweep and research succeed)."""

    def chat(self, messages, tools=None, **kwargs):
        from src.agent import CLUSTER_RECOVERY_SYSTEM_PROMPT
        if messages[0]["content"] == CLUSTER_RECOVERY_SYSTEM_PROMPT:
            raise _timeout()
        return super().chat(messages, tools, **kwargs)


def test_a_cluster_attempt_whose_first_call_fails_presents_nothing(tmp_path):
    client = FailingRecovery([fetch("a", EU, tail.CARTUBE), say({"done": True, "reason": "x"})],
                             sweep=lambda n: (_ for _ in ()).throw(_timeout()))
    result, events = run(tmp_path, client, acquisition_mode="contract", **GATE_OFF)
    started = [e for e in events if e["kind"] == "cluster_recovery_started"]
    assert started and started[0]["offered_candidate_keys"] and "presented_candidate_keys" not in started[0]
    assert _presented(events, "cluster_recovery") == []
    assert not set(started[0]["offered_candidate_keys"]) & presented_keys(events)


def test_a_cluster_attempt_presents_its_candidates_once_its_first_call_returns(tmp_path):
    client = PhaseClient([fetch("a", EU, tail.CARTUBE), say({"done": True, "reason": "x"})],
                         sweep=lambda n: (_ for _ in ()).throw(_timeout()))
    result, events = run(tmp_path, client, acquisition_mode="contract", **GATE_OFF)
    started = [e for e in events if e["kind"] == "cluster_recovery_started" and e["offered_candidate_keys"]]
    presented = _presented(events, "cluster_recovery")
    assert started and [(p["cluster"], p["attempt"], p["presented_candidate_keys"]) for p in presented] == [
        (s["cluster"], s["attempt"], s["offered_candidate_keys"]) for s in started]
    assert presented[0]["seq"] > next(e["seq"] for e in events if e["kind"] == "model_response"
                                      and e["phase"] == "field_recovery" and e["seq"] > started[0]["seq"])


# --- cluster_candidates_announced: only after the NEXT call returned --------------------------------------------

def _announce_run(tmp_path, fail_second):
    """A web cluster attempt: turn 1 fetches a page whose harvest adds new candidates for the open field (announced
    in the next request's operational note), then the turn-2 call returns or raises."""
    from src.agent import CLUSTER_RECOVERY_SYSTEM_PROMPT

    class Announcing(PhaseClient):
        recovery_calls = 0

        def chat(self, messages, tools=None, **kwargs):
            if messages[0]["content"] == CLUSTER_RECOVERY_SYSTEM_PROMPT:
                Announcing.recovery_calls += 1
                packet = json.loads(messages[1]["content"].split("\n", 1)[1])
                turns = sum(1 for m in messages if m["role"] == "assistant")
                from src.glm_client import ChatResponse
                if packet["mode"] == "local_only":       # the local pass ends at once; the web attempt follows
                    return ChatResponse(message=say({"cluster": packet["cluster"], "fields": []}),
                                        finish_reason="stop",
                                        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                if turns == 0:
                    from src.glm_client import ChatResponse
                    return ChatResponse(message={"role": "assistant", "content": "", "tool_calls": [
                        _call("f1", "fetch_url", {"url": tail.CARTUBE})]}, finish_reason="tool_calls",
                        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                if fail_second:
                    raise _timeout()
                from src.glm_client import ChatResponse
                return ChatResponse(message=say({"cluster": packet["cluster"], "fields": []}), finish_reason="stop",
                                    usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
            return super().chat(messages, tools, **kwargs)

    client = Announcing([fetch("a", EU), say({"done": True, "reason": "x"})])
    return run(tmp_path, client, acquisition_mode="contract", cluster_max_attempts=1, requested_fields=["length_mm"],
               primary_research_min_base_documents=0, primary_research_min_base_scoped_coverage=0,
               document_sweep_max_turns=0)


def test_announced_candidates_are_logged_only_after_the_next_call_returns(tmp_path):
    result, events = _announce_run(tmp_path, fail_second=False)
    announced = [e for e in events if e["kind"] == "cluster_candidates_announced"]
    assert announced, "the fixture must announce new candidates"
    first = announced[0]
    assert first["presented_candidate_keys"] and first["turn"] == 1
    later_response = [e for e in events if e["kind"] == "model_response" and e["phase"] == "field_recovery"
                      and e["seq"] < first["seq"]]
    assert len(later_response) >= 2                     # logged after the turn-2 call returned
    assert set(first["presented_candidate_keys"]) <= presented_keys(events)


def test_announced_candidates_are_not_logged_when_the_next_call_raises(tmp_path):
    result, events = _announce_run(tmp_path / "f", fail_second=True)
    assert any(e["kind"] == "cluster_turn_novelty" and e["novelty"] for e in events)
    assert not any(e["kind"] == "cluster_candidates_announced" for e in events)


# --- backward compatibility: runs logged before the fix --------------------------------------------------------

def test_old_started_events_keep_producing_the_same_presented_keys():
    old = [{"kind": "document_sweep_started", "presented_candidate_keys": ["a|d1|num:1.0", "b|d1|num:2.0"]},
           {"kind": "cluster_recovery_started", "presented_candidate_keys": ["c|d2|num:3.0"]},
           {"kind": "cluster_candidates_announced", "presented_candidate_keys": ["d|d3|num:4.0"]},
           {"kind": "document_sweep_started", "presented_candidate_keys": []}]
    assert presented_keys(old) == {"a|d1|num:1.0", "b|d1|num:2.0", "c|d2|num:3.0", "d|d3|num:4.0"}
    # new runs: offered keys alone are never presented
    new = [{"kind": "document_sweep_started", "offered_candidate_keys": ["a|d1|num:1.0"]},
           {"kind": "cluster_recovery_started", "offered_candidate_keys": ["c|d2|num:3.0"]},
           {"kind": "candidates_presented", "source": "document_sweep", "presented_candidate_keys": ["e|d4|num:5.0"]}]
    assert presented_keys(new) == {"e|d4|num:5.0"}
    cand = {"field": "length_mm", "value": 4650, "document_id": "d1"}
    matrix = {"fields": {"length_mm": [cand]}}
    entry = [{"field": "length_mm", "state": "missing", "retry_eligible": True}]
    offered_only = [{"kind": "document_sweep_started", "offered_candidate_keys": [candidate_key(cand)]}]
    assert fresh_candidates(matrix, offered_only, ["length_mm"]) == {"length_mm": [cand]}
    spec = [s for s in SPECS if s["name"] == "length_mm"] or [{"name": "length_mm"}]
    assert triage(entry, spec, matrix, offered_only, 2)["length_mm"]["triage"] == "candidate_rich_local"
