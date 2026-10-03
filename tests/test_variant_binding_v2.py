"""PR #34 (binding-v2): generic government trims match only as a qualified phrase (A), single-trim inference from the
government catalog (B), binding gap diagnosis (C), grounded not-admissible reasons (D), column identity of
multi-variant tables (E) and the re-acquisition gate on the reason a field is open (F). Offline only."""

import copy
import json

import pytest

from fixtures import corolla_binding
from fixtures.corolla_harvest import put
from fixtures.corolla_touring import PAYLOAD as COROLLA, VEHICLE as COROLLA_VEHICLE
from test_phase_contracts import GATE_OFF, ROUTES, fetch, run, say
from test_reacquire import ReacquireClient

from src import research_memory
from src.adjudication import candidate_request
from src.candidate_harvest import harvest_document
from src.document_binding import (BINDING_VERSION, LEVELS, bind, binding_gaps, mentions, single_catalog_trim,
                                  statuses, target_identity)
from src.evidence_admission import AdmissionContext, admit
from src.field_recovery import current_evaluation
from src.fields import load_schema, resolve_requested_fields
from src.research_memory import ResearchMemory
from src.storage.cache import DocumentCache
from src.tail_planner import binding_gap_gate

XPENG = {"identity": {"manufacturer": "אקספנג", "commercial_name": "G6", "year": 2026, "trim": "MAX"},
         "engine_drivetrain": {"engine_cc": 0, "power_hp": 486, "propulsion_normalized": "battery_electric",
                               "drivetrain_normalized": "awd"},
         "structure": {"body_normalized": "suv"}}
XPENG_KEY = "אקספנג|g6|2026|suv|battery_electric|awd|485|"
FULL = 'XPeng G6 MAX 2026 SUV חשמלי AWD 486 כ"ס'
TECH = 'XPeng G6 2026 SUV חשמלי AWD 486 כ"ס'      # the exact technical variant, no trim
SPECS = load_schema()


def xpeng(**changes):
    payload = copy.deepcopy(XPENG)
    for part, values in changes.items():
        payload[part].update(values)
    return target_identity(payload)


@pytest.fixture
def index(tmp_path, monkeypatch):
    """Point the runtime at a fixture catalog index: index({key: entry}, complete=True)."""
    def write(entries, complete=True):
        path = tmp_path / f"trim_index_{len(list(tmp_path.glob('trim_index_*')))}.json"
        path.write_text(json.dumps({"complete": complete, "entries": entries}, ensure_ascii=False), "utf-8")
        monkeypatch.setenv("CATALOG_TRIM_INDEX_PATH", str(path))
        return path
    return write


def bind_text(identity, text, *, authority="official_importer", market="IL", others=(), names_family=True,
              requirement="exact_market_trim", layers=None):
    return bind(identity, statuses(text, identity), layers, market=market, requirement=requirement,
                source_authority=authority, document_names_family=names_family, other_trims_named=list(others))


# --- A: a generic government trim matches only as a qualified phrase ------------------------------------------------

def test_xpeng_g6_max_binds_the_market_trim_through_the_qualified_phrase():
    ident = xpeng()
    assert ident.trim_tokens == [] and ident.trim_words == ["max"] and "g6 max" in ident.qualified_trim_phrases
    st = statuses(FULL, ident)
    assert st["trim"] == "match" and all(v in ("match", "absent") for v in st.values())
    result = bind(ident, st, requirement="exact_market_trim", market="IL")
    assert result["binding_level"] == "exact_market_trim" and result["variant_match"] == "exact"
    assert result["binding_basis"] == "qualified_trim_phrase"
    assert result["binding_dimensions"]["trim"]["rule"] == "qualified_trim_phrase"
    for text in ("xpeng g6-max", "אקספנג G6 MAX", "XPeng G6 בגרסת MAX"):
        assert mentions(text, ident)["trim"] == "match", text
    # outside the target market the phrase still does not reach the market trim
    assert bind(ident, st, requirement="exact_market_trim", market="DE")["variant_match"] == "unclear"


@pytest.mark.parametrize("text", [
    "max power 486 hp", "Max. speed 200 km/h, MAX range", "g6 maximum comfort",
    "XPeng G6 MAX power 486 hp", "XPeng G6 - MAX power 486 hp", "G6\nMAX power 486 hp",
    'אקספנג G6 MAX הספק 486 כ"ס',
])
def test_generic_max_metric_wording_is_never_the_trim(text):
    assert mentions(text, xpeng())["trim"] != "match"


def test_a_negated_qualified_phrase_is_negated():
    ident = xpeng()
    assert mentions("לא בגרסת G6 MAX", ident)["trim"] == "negated"
    assert statuses("not available on the G6 MAX", ident)["trim"] == "mismatch"


def test_the_targets_own_generic_word_is_not_another_named_trim():
    ident = xpeng()
    assert mentions("The XPeng G6 MAX version", ident)["trim"] == "match"
    assert mentions("the Performance version", ident)["trim"] == "negated"
    assert mentions("Base version: smaller battery", ident)["trim"] == "negated"
    assert mentions("Top version: larger wheels", ident)["trim"] == "negated"


def test_corolla_business_edi_keeps_its_matching():
    ident = target_identity(COROLLA, COROLLA_VEHICLE)
    assert ident.trim_tokens == ["business"] and ident.qualified_trim_phrases == []
    assert mentions("Toyota Corolla Touring Sports Business Edition", ident)["trim"] == "match"
    assert mentions("קורולה טורינג ספורט BUSINESS EDI", ident)["trim"] == "match"
    assert mentions("Premium version: 17-inch wheels", ident)["trim"] == "negated"


# --- B: single-trim inference from the government catalog -----------------------------------------------------------

PRICE_PAGE = """<html><head><title>XPeng G6 2026 - מפרט טכני</title></head><body><h1>XPeng G6 AWD</h1>
<p>רכב חשמלי, SUV, הנעה כפולה AWD</p><p>הספק מרבי: 486 כ"ס</p><p>צמיגים: 255/45 R20</p></body></html>"""


def admitted(tmp_path, url, html, field="tire_size_front", payload=XPENG):
    cache = DocumentCache(tmp_path / "cache")
    doc = put(cache, url, html, "html")
    adm = AdmissionContext.for_run(payload, None, SPECS, "IL")
    cand = next(c for c in harvest_document(cache, doc, SPECS)[0] if c["field"] == field)
    decision = admit(adm, cache, candidate_request(field, {**cand, "document_id": doc}), [doc])
    assert decision["accepted"], decision
    return decision["record"]


def test_single_catalog_trim_on_an_official_il_page_binds_the_market_trim(tmp_path, index):
    index({XPENG_KEY: {"trims": ["MAX"], "records": ["101122"]}})
    record = admitted(tmp_path, "https://www.xpeng.co.il/g6/specifications", PRICE_PAGE)
    assert record["source_authority"] == "official_importer" and record["market"] == "IL"
    assert record["binding_level"] == "exact_market_trim" and record["variant_match"] == "exact"
    assert record["binding_basis"] == "single_trim_catalog"
    trim = record["binding_dimensions"]["trim"]
    assert trim["basis"] == "single_trim_catalog" and trim["catalog_key"] == XPENG_KEY
    assert trim["catalog_trim"] == "MAX" and trim["catalog_records"] == ["101122"]


@pytest.mark.parametrize("entries,complete", [
    ({XPENG_KEY: {"trims": ["BLACKEDITION", "CORE PERF", "MAX"]}}, True),           # two or more trims for the key
    ({}, True),                                                                      # key missing
    ({XPENG_KEY: {"trims": ["MAX"], "complete": False}}, True),                      # incomplete entry
    ({XPENG_KEY: {"trims": ["MAX"]}}, False),                                        # incomplete index
    ({XPENG_KEY: {"trims": ["PRO"]}}, True),                                         # the one trim is another trim
    ({XPENG_KEY: {"trims": ["MAX"]}, XPENG_KEY.replace("|485|", "|480|"): {"trims": ["PERF"]}}, True),  # within 3 %
])
def test_the_catalog_rule_fails_closed(tmp_path, index, entries, complete):
    index(entries, complete)
    record = admitted(tmp_path, "https://www.xpeng.co.il/g6/specifications", PRICE_PAGE)
    assert record["binding_level"] == "exact_technical_variant" and record["variant_match"] == "unclear"
    assert "binding_basis" not in record


@pytest.mark.parametrize("kw", [
    {"market": "DE"},                                                   # foreign-market source
    {"authority": "publisher", "names_family": False},                 # non-official, identity zone without the family
    {"others": ["performance"]},                                        # the document names another trim
])
def test_the_catalog_rule_needs_the_market_an_official_source_and_no_other_trim(index, kw):
    index({XPENG_KEY: {"trims": ["MAX"]}})
    ident = xpeng()
    assert bind_text(ident, TECH)["binding_basis"] == "single_trim_catalog"            # the baseline applies
    result = bind_text(ident, TECH, **kw)
    assert result["binding_level"] == "exact_technical_variant" and result["variant_match"] == "unclear"


def test_catalog_inference_is_blocked_by_an_explicit_generic_competing_tier(tmp_path, index):
    index({XPENG_KEY: {"trims": ["MAX"], "records": ["101122"]}})
    html = PRICE_PAGE.replace("</body>", "<p>Base version: fabric seats.</p></body>")
    record = admitted(tmp_path, "https://www.xpeng.co.il/g6/specifications", html)
    assert record["binding_level"] == "exact_technical_variant"
    assert record["variant_match"] == "unclear"
    assert "binding_basis" not in record


def test_the_catalog_rule_never_applies_with_an_unknown_other_trim_scan_or_a_veto(index):
    index({XPENG_KEY: {"trims": ["MAX"]}})
    ident = xpeng()
    result = bind(ident, statuses(TECH, ident), market="IL", requirement="exact_market_trim",
                  source_authority="official_importer", document_names_family=True, other_trims_named=None)
    assert result["variant_match"] == "unclear"
    # a non-official target-market document whose identity zone names the family qualifies
    assert bind_text(ident, TECH, authority="publisher", names_family=True)["binding_basis"] == "single_trim_catalog"
    # another trim named in the fact's own context: trim mismatch -> a veto, as today
    vetoed = bind_text(ident, TECH, layers=[("quote", "only on the Performance version")])
    assert vetoed["variant_match"] == "different"
    # a mixed power / drivetrain document stays below the technical variant
    mixed = bind_text(ident, 'XPeng G6 2026 SUV חשמלי AWD 486 כ"ס / RWD 296 כ"ס')
    assert mixed["binding_level"] == "body_powertrain" and mixed["variant_match"] == "unclear"
    # a field that only needs the technical variant is untouched
    assert "binding_basis" not in bind_text(ident, TECH, requirement="exact_technical_variant")


def test_single_catalog_trim_needs_every_identity_part(index):
    index({XPENG_KEY: {"trims": ["MAX"], "records": ["101122"]}})
    assert single_catalog_trim(xpeng())["trim"] == "MAX"
    assert single_catalog_trim(xpeng(engine_drivetrain={"drivetrain_normalized": None})) is None
    assert single_catalog_trim(xpeng(engine_drivetrain={"power_hp": None})) is None


def test_the_committed_index_has_the_benchmark_vehicle_and_reports_its_trims():
    """data/catalog_trim_index.json is the real catalog: the G6 2026 AWD 486 hp has more than one trim (MAX,
    BLACKEDITION, CORE PERF), so the benchmark vehicle is NOT bound by the catalog rule; only Part A can help it."""
    from src.document_binding import trim_index

    data = trim_index()
    assert data.get("complete") is True and len(data["entries"]) > 1000
    assert "MAX" in data["entries"][XPENG_KEY]["trims"] and single_catalog_trim(xpeng()) is None


# --- golden: the Corolla fixtures under binding-v1 vs binding-v2 ------------------------------------------------------

def test_corolla_bindings_are_unchanged_or_rise_with_a_basis():
    golden = json.loads(corolla_binding.GOLDEN.read_text("utf-8"))
    current = corolla_binding.compute()
    assert set(current) == set(golden)
    for key, old in golden.items():
        new = current[key]
        if old["variant_match"] != "unclear" or old["binding_level"] == "exact_market_trim":
            assert {k: new.get(k) for k in ("binding_level", "variant_match")} == old, key
        elif new != old:
            assert LEVELS.index(new["binding_level"]) > LEVELS.index(old["binding_level"]), key
            assert new.get("binding_basis") in ("qualified_trim_phrase", "single_trim_catalog"), key


# --- C: binding gap diagnosis --------------------------------------------------------------------------------------------

TRIM_SPEC = {"name": "tire_size_front", "applicable": True, "binding_requirement": "exact_market_trim"}


def _ev(eid, **kw):
    return {"evidence_id": eid, "field": "tire_size_front", "value": "255/45 R20", "market": "IL",
            "binding_requirement": "exact_market_trim", "admission_status": "accepted",
            "source_authority": "official_importer", **kw}


def _events(*items):
    return [{"kind": "run_started", "seq": 0, "target_market": "IL"}] + \
        [{"kind": "evidence", "seq": i + 1, "evidence": item} for i, item in enumerate(items)]


@pytest.mark.parametrize("item,gaps", [
    (_ev("e1", binding_level="exact_technical_variant", variant_match="unclear",
         binding_dimensions={"model": {"status": "match"}, "power": {"status": "match"}}), ["trim_absent"]),
    (_ev("e1", binding_level="body_powertrain", variant_match="unclear",
         binding_dimensions={"model": {"status": "match"}, "power": {"status": "mixed"}}), ["power_mixed"]),
    (_ev("e1", binding_level="body_powertrain", variant_match="different", binding_veto=["body_mismatch@quote"]),
     ["veto:body"]),
])
def test_a_variant_not_exact_field_reports_its_binding_gap(item, gaps):
    entry = current_evaluation(_events(item), [TRIM_SPEC])[0]
    assert entry["state"] == "variant_not_exact"
    assert [i for i in entry["info"] if i.startswith("binding_gap:")] == [f"binding_gap:{g}" for g in gaps]
    assert binding_gaps(item) == gaps


def test_binding_gaps_of_a_body_powertrain_item_name_the_missing_technical_dimension():
    item = {"binding_level": "body_powertrain", "binding_requirement": "exact_technical_variant",
            "binding_dimensions": {"model": {"status": "match"}}}
    assert binding_gaps(item) == ["displacement_absent", "power_absent"]


def test_diagnostics_count_binding_gaps_per_vehicle_and_field():
    from src import diagnostics as D

    events = _events(_ev("e1", binding_level="exact_technical_variant", variant_match="unclear"))
    events[0]["requested_field_specs"] = [TRIM_SPEC]
    final = D.final_field_states(events)
    assert final["counts"]["variant_not_exact"] == 1
    assert final["binding_gaps"] == {"tire_size_front": ["trim_absent"]}
    assert final["binding_gap_counts"] == {"trim_absent": 1}
    diag = D.vehicle_diagnostics(events)
    row = D.vehicle_row(diag)
    assert row["binding_gap_trim"] == 1 and row["binding_gap_technical"] == 0


# --- D: grounded not-admissible reasons ----------------------------------------------------------------------------------

def test_grounded_not_admissible_reasons_are_counted():
    from src import diagnostics as D

    events = [{"kind": "grounded_candidates_finished", "stage": "sweep", "model_calls": 2, "items": 5,
               "admissible": 0, "not_admissible": 3,
               "not_admissible_by_reason": {"semantic_mismatch": 2, "offset_invalid": 1}},
              {"kind": "grounded_candidates_finished", "stage": "sweep", "model_calls": 1, "items": 1,
               "admissible": 0, "not_admissible": 1, "not_admissible_by_reason": {"semantic_mismatch": 1,
                                                                                  "quote_not_in_document": 1}}]
    summary = D.sweep_summary(events, [])
    assert summary["grounded_not_admissible_by_reason"] == {"semantic_mismatch": 3, "offset_invalid": 1,
                                                           "quote_not_in_document": 1}
    row = D.vehicle_row(D.vehicle_diagnostics(events))
    assert json.loads(row["sweep_grounded_not_admissible_by_reason"])["semantic_mismatch"] == 3


def test_grounded_pointer_problems_map_to_reasons():
    from src.agent import GROUNDED_PROBLEM_REASON
    from src.grounded import parse_reply

    blocks = {"b1": {"text": "Screen size 15.6 inch"}}
    _, invalid = parse_reply({"items": [{"field": "screen_size_in", "value": 15.6, "block": "b1", "start": 0,
                                         "end": 99},
                                        {"field": "nope", "value": 1, "block": "b1", "start": 0, "end": 4},
                                        {"field": "screen_size_in", "value": 15.6, "block": "b9", "start": 0,
                                         "end": 4}]}, blocks, ["screen_size_in"])
    assert [GROUNDED_PROBLEM_REASON[r["problem"]] for r in invalid] == ["offset_invalid", "unknown_field",
                                                                         "offset_invalid"]


# --- E: column identity of multi-variant tables ---------------------------------------------------------------------------

COLUMNS = [("Standard", 190, 258, "RWD", "1,995"), ("Long Range", 218, 296, "RWD", "2,040"),
           ("Performance", 316, 430, "AWD", "2,135"), ("MAX", 357, 486, "AWD", "2,180")]


def spec_table(columns):
    rows = [["גרסה"] + [c[0] for c in columns], ["הספק (kW)"] + [f"{c[1]} kW" for c in columns],
            ['הספק (כ"ס)'] + [f'{c[2]} כ"ס' for c in columns], ["הנעה"] + [c[3] for c in columns],
            ['משקל עצמי (ק"ג)'] + [c[4] for c in columns]]
    body = "".join("<tr>" + "".join(f"<td>{x}</td>" for x in r) + "</tr>" for r in rows)
    return f"""<html><head><title>XPeng G6 2026 - מפרט טכני</title></head><body><h1>XPeng G6</h1>
<p>רכב חשמלי SUV</p><table>{body}</table></body></html>"""


def weights(tmp_path, columns):
    cache = DocumentCache(tmp_path / "cache")
    doc = put(cache, "https://www.xpeng.co.il/g6/specifications", spec_table(columns), "html")
    adm = AdmissionContext.for_run(XPENG, None, SPECS, "IL")
    out = {}
    for cand in harvest_document(cache, doc, SPECS)[0]:
        if cand["field"] == "curb_weight_kg":
            decision = admit(adm, cache, candidate_request("curb_weight_kg", {**cand, "document_id": doc}), [doc])
            out[cand["value"]] = (cand, decision["record"])
    return out, adm.material(cache, doc, None).profile["statuses"]


def test_the_columns_own_power_and_drivetrain_bind_its_values(tmp_path):
    out, doc_statuses = weights(tmp_path, COLUMNS)
    assert doc_statuses["power"] == "mixed" and doc_statuses["drivetrain"] == "mixed"
    cand, record = out[2180]
    assert cand["column_identity"].startswith("MAX | הספק (kW) 357 kW") and len(cand["column_identity"]) <= 300
    assert record["binding_level"] == "exact_technical_variant" and record["variant_match"] == "exact"
    assert record["binding_dimensions"]["power"]["basis"] == "column_identity"
    assert record["binding_dimensions"]["drivetrain"]["basis"] == "column_identity"
    for value in (1995, 2040, 2135):
        assert out[value][1]["variant_match"] == "different", value
        assert any(v.endswith("@column_identity") for v in out[value][1]["binding_veto"])


def test_a_value_in_two_target_columns_stays_unclear(tmp_path):
    out, _ = weights(tmp_path, COLUMNS + [("MAX Plus", 357, 486, "AWD", "2,180")])
    cand, record = out[2180]
    assert len(cand["column_identities"]) == 2
    assert record["variant_match"] == "unclear"


def test_tables_store_their_column_identity():
    from src.tools.extract import _html_tables, with_column_identities

    tables = with_column_identities(_html_tables(spec_table(COLUMNS)))
    idents = tables[0]["column_identity"]
    assert idents[0] is None and idents[4].startswith("MAX | ") and "הנעה AWD" in idents[4]


# --- F: the re-acquisition gate -------------------------------------------------------------------------------------------

PRICES = "https://www.toyota.co.il/new-cars/corolla-touring-sports/prices"
PRICES_HTML = """<html><head><title>טויוטה קורולה טורינג ספורט 2024 1.8 היברידי - מחירון</title></head><body>
<h1>טויוטה קורולה טורינג ספורט 2024 1.8 היברידי</h1><p>מחיר: 179,990 ש"ח</p></body></html>"""
MODEL = "https://www.toyota.co.il/new-cars/corolla-touring-sports"
MODEL_HTML = """<html><head><title>טויוטה קורולה טורינג ספורט 2024</title></head><body>
<h1>טויוטה קורולה טורינג ספורט 2024</h1><p>גרסת Business: מערכת מולטימדיה ומצלמה אחורית.</p></body></html>"""


def reacquire_run(tmp_path, *urls):
    routes = {**ROUTES, PRICES: (PRICES_HTML, "text/html"), MODEL: (MODEL_HTML, "text/html")}
    client = ReacquireClient([fetch("a", *urls), say({"done": True, "reason": "enough"})],
                             lambda packet, turn_no: say({"done": True, "reason": "nothing more"}))
    result, events = run(tmp_path, client, routes=routes, acquisition_mode="contract",
                         requested_fields=["list_price", "length_mm"], **GATE_OFF)
    return result, events, client


@pytest.mark.recovery_mode("reacquire")
def test_a_cluster_open_only_on_binding_makes_no_search(tmp_path):
    result, events, client = reacquire_run(tmp_path, PRICES, MODEL)
    price = [e["evidence"] for e in events if e["kind"] == "evidence" and e["evidence"]["field"] == "list_price"]
    assert price and price[0]["source_authority"] == "official_importer" and price[0]["variant_match"] == "unclear"
    rec = result["field_recovery"]
    assert rec["triage"]["list_price"]["binding_gap"] == ["trim_absent"]
    assert rec["reacquire_skipped_fields"] == ["list_price"]
    skipped = [e for e in events if e["kind"] == "reacquire_skipped_binding_gap"]
    assert [(e["cluster"], e["fields"]) for e in skipped] == [("commercial", ["list_price"])]
    # the commercial cluster never got an episode; the other cluster is scheduled as before
    assert [r["packet"]["cluster"] for r in client.reacquire_requests] == ["technical_spec"]
    assert [a["cluster"] for a in rec["attempts"]] == ["technical_spec"]
    from src import diagnostics as D
    assert D.recovery_summary(events)["reacquire_skipped_fields"] == ["list_price"]


@pytest.mark.recovery_mode("reacquire")
def test_the_trim_exception_allows_one_search_with_the_trim_prompt(tmp_path):
    result, events, client = reacquire_run(tmp_path, PRICES)       # no official IL document names the trim
    rec = result["field_recovery"]
    assert rec["reacquire_skipped_fields"] == []
    commercial = [r for r in client.reacquire_requests if r["packet"]["cluster"] == "commercial"]
    assert len(commercial) == 1
    packet = commercial[0]["packet"]
    assert packet["budget"]["billable_searches"] == 1 and packet["trim_binding_fields"] == ["list_price"]
    assert "do not tie them to this exact trim" in packet["trim_binding_instruction"]
    episode = next(a for a in rec["attempts"] if a["cluster"] == "commercial")
    assert episode["search_budget"] == 1 and episode["searches"] <= 1
    assert any(e["kind"] == "reacquire_trim_exception" for e in events)
    technical = next(r for r in client.reacquire_requests if r["packet"]["cluster"] == "technical_spec")
    assert technical["packet"]["budget"]["billable_searches"] > 1 and "trim_binding_fields" not in technical["packet"]


def test_binding_gap_gate_rules():
    official = {"source_authority": "official_importer", "market": "IL"}
    evaluation = {"a": {"state": "variant_not_exact", "info": ["binding_gap:trim_absent"]},
                  "b": {"state": "variant_not_exact", "info": ["binding_gap:power_mixed"]},
                  "c": {"state": "missing", "info": []},
                  "d": {"state": "variant_not_exact", "info": ["binding_gap:trim_absent"]}}
    evidence = {"a": [official], "b": [official], "d": [{**official, "source_authority": "publisher"}]}
    gate = binding_gap_gate(["a", "b", "c", "d"], evaluation, evidence, "IL", trim_named_by_official=False)
    # b's gap is technical, so normal reacquisition must keep it; only trim-only a participates in the gate.
    # Because another field remains in the cluster, the trim exception keeps a too and does not cap the shared search.
    assert gate["skipped"] == [] and gate["fields"] == ["a", "b", "c", "d"] and gate["trim_exception"]
    assert gate["search_cap"] is None and gate["trim_fields"] == ["a", "d"]
    only_trim = binding_gap_gate(["a"], evaluation, evidence, "IL", trim_named_by_official=False)
    assert only_trim["trim_exception"] and only_trim["fields"] == ["a"] and only_trim["search_cap"] == 1
    named = binding_gap_gate(["a"], evaluation, evidence, "IL", trim_named_by_official=True)
    assert named["fields"] == [] and named["skipped"] == ["a"]
    foreign = binding_gap_gate(["a"], evaluation, {"a": [{**official, "market": "DE"}]}, "IL", False)
    assert foreign["fields"] == ["a"] and not foreign["skipped"] and foreign["search_cap"] is None

    technical = binding_gap_gate(["b"], evaluation, evidence, "IL", trim_named_by_official=True)
    assert technical["fields"] == ["b"] and technical["skipped"] == [] and not technical["trim_exception"]


# --- versions ---------------------------------------------------------------------------------------------------------------

def test_binding_version_bumped_and_memory_ignores_facts_of_the_old_version(tmp_path, monkeypatch):
    assert BINDING_VERSION == "binding-v2"
    hev = resolve_requested_fields(None, propulsion="hybrid")
    identity = target_identity(COROLLA, COROLLA_VEHICLE)
    fact = {"evidence_id": "e1", "field": "fuel_tank_l", "value": 43, "unit": "l", "document_id": "d1",
            "quote": "Fuel tank capacity 43 l", "market": "UK", "variant_match": "exact",
            "binding_level": "exact_technical_variant", "source_authority": "official_manufacturer",
            "admission_status": "accepted"}
    ok = [{"field": "fuel_tank_l", "state": "ok", "evidence_ids": ["e1"], "conflict_evidence_ids": []}]
    memory = ResearchMemory(tmp_path)
    monkeypatch.setattr(research_memory, "BINDING_VERSION", "binding-v1")
    assert memory.record_facts([fact], hev, identity, {"record_id": "38626"}, ok)
    monkeypatch.setattr(research_memory, "BINDING_VERSION", BINDING_VERSION)
    assert memory.reusable_facts(hev, identity) == ([], {"stale_schema_or_gate_version": 1})


# --- the index builder -------------------------------------------------------------------------------------------------------

def _builder():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "build_trim_index.py"
    spec = importlib.util.spec_from_file_location("build_trim_index", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(record, trim, **kw):
    base = {"upstream_record_id": record, "tozar": "אקספנג", "kinuy_mishari": "G6", "shnat_yitzur": 2026,
            "ramat_gimur": trim, "norm_body_style": "suv", "norm_propulsion_technology": "battery_electric",
            "norm_drivetrain": "awd", "koah_sus": 486, "nefah_manoa": 0}
    return {**base, **kw}


def test_the_builder_keys_rows_like_the_runtime_and_marks_unnormalized_siblings_incomplete(index):
    build = _builder()
    built = build.build_index([_row("1", "MAX"), _row("2", "max "), _row("3", "STAND RANGE", norm_drivetrain=
                                                                        "two_wheel_drive", koah_sus=258)],
                              source="test")
    assert built["entries"][XPENG_KEY] == {"trims": ["MAX"], "records": ["1", "2"]}
    path = index({})
    path.write_text(build.dumps(built), "utf-8")
    assert single_catalog_trim(xpeng())["records"] == ["1", "2"]
    # a row the mapper left without a body could be a sibling of every G6 2026 variant of its power
    unsure = build.build_index([_row("1", "MAX"), _row("9", "PERF", norm_body_style=None)], source="test")
    assert unsure["entries"][XPENG_KEY]["complete"] is False
    path.write_text(build.dumps(unsure), "utf-8")
    assert single_catalog_trim(xpeng()) is None
    # the benchmark-only index never applies
    assert all(e["complete"] is False for e in build.build_index([_row("1", "MAX")], source="b",
                                                                 complete=False)["entries"].values())


def test_generic_trims_have_distinct_exact_market_scope_keys():
    max_identity = xpeng(identity={"trim": "MAX"})
    pro_identity = xpeng(identity={"trim": "PRO"})
    assert max_identity.trim_tokens == pro_identity.trim_tokens == []
    assert max_identity.scope_key("exact_technical_variant") == pro_identity.scope_key("exact_technical_variant")
    assert max_identity.scope_key("exact_market_trim") != pro_identity.scope_key("exact_market_trim")
    assert '"max"' in max_identity.scope_key("exact_market_trim")
    assert '"pro"' in pro_identity.scope_key("exact_market_trim")


def test_bare_multi_word_phrases_only_for_all_generic_trims():
    assert "pro max" in xpeng(identity={"trim": "PRO MAX"}).qualified_trim_phrases
    assert mentions("the PRO MAX comes with", xpeng(identity={"trim": "PRO MAX"}))["trim"] == "match"
    short = xpeng(identity={"trim": "GT 4"})
    assert short.trim_tokens == [] and short.qualified_trim_phrases == ["g6 gt 4"]
    assert mentions("a GT 4 door body", short)["trim"] != "match"
    assert mentions("XPeng G6 GT 4", short)["trim"] == "match"
