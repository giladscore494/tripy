"""PR #35 (binding-v3): the binding-layer helper shared by Evidence Admission and Binding Replay (1), Binding Replay on a
fixture run (2), model-year rules (3), page chrome vs the identity zone (4), the Corolla golden (5), binding gap
semantics and year telemetry (6), recovery spend while binding is the blocker (7) and the version bump (8). Offline."""

import json

from fixtures import admission_records


# --- 1: refactor safety ---------------------------------------------------------------------------------------------

# keys a record may differ in from the pre-PR golden: the binding version (admission records do not carry it today,
# excluded as the spec allows) and the year telemetry binding-v3 adds to a record (Part D; never read by binding,
# evaluation or Final Assembly). Every other key, the binding fields included, must be identical.
NEW_TELEMETRY = ("binding_version", "year_context")


def test_admission_decisions_are_unchanged_by_the_helper_extraction():
    """The golden was written on main before binding_layers / fact_binding were extracted from admit(): the same
    requests are accepted / rejected for the same reasons and give the same records (binding fields included)."""
    golden = json.loads(admission_records.GOLDEN.read_text("utf-8"))
    current = admission_records.compute()
    assert set(current) == set(golden) and len(golden) > 400
    for key, old in golden.items():
        new = current[key]
        assert new["accepted"] == old["accepted"], key
        if not old["accepted"]:
            assert new["reasons"] == old["reasons"], key
            continue
        strip = lambda r: {k: v for k, v in r.items() if k not in NEW_TELEMETRY}  # noqa: E731
        assert strip(new["record"]) == strip(old["record"]), key


# --- shared fixtures ------------------------------------------------------------------------------------------------

import copy  # noqa: E402
import hashlib  # noqa: E402

import pytest  # noqa: E402

from fixtures.corolla_harvest import put  # noqa: E402
from test_phase_contracts import GATE_OFF, ROUTES, fetch, run, say, turn  # noqa: E402
from test_reacquire import ReacquireClient  # noqa: E402
from test_tools_smoke import _call  # noqa: E402

from src import research_memory  # noqa: E402
from src.adjudication import candidate_request  # noqa: E402
from src.candidate_harvest import harvest_document  # noqa: E402
from src.document_binding import (BINDING_VERSION, LEVELS, bind, binding_gaps, document_profile,  # noqa: E402
                                  statuses, target_identity, url_year_context, year_context)
from src.evidence_admission import AdmissionContext, admit  # noqa: E402
from src.fields import load_schema, resolve_requested_fields  # noqa: E402
from src.storage.cache import DocumentCache  # noqa: E402

XPENG = admission_records.XPENG
SPECS = load_schema()


def xpeng(year=2026):
    payload = copy.deepcopy(XPENG)
    payload["identity"]["year"] = year
    return target_identity(payload)


def profile(html=None, *, title="", url="https://www.xpeng.co.il/g6", text=None, h1=None, h2=None, body=None):
    return document_profile(text=text or "", title=title, url=url, identity=xpeng(), headings=h1, subheadings=h2,
                            body_text=body)


# --- 3: model-year rules ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,status", [
    ("XPeng G6 2026 SUV", "match"),                                   # target present, no other model year
    ("XPeng G6 2026 SUV. השוואה: G6 2025", "mixed"),                  # target plus another model year
    ("XPeng G6 2025 SUV", "adjacent"),                                # no target, all statements target ± 1
    ("שנתון 2027, XPeng G6", "adjacent"),
    ("XPeng G6 2024 SUV", "mismatch"),                                # no target, a statement >= 2 years away
    ("XPeng G6 2025 ו-G6 2023", "mismatch"),                          # one adjacent + one far: fail-closed
    ("XPeng G6 SUV", "absent"),                                       # no recognized statement
])
def test_the_year_status_table(text, status):
    assert statuses(text, xpeng())["year"] == status


def test_model_year_statements_and_their_kinds():
    ident = xpeng()
    for text, kind in (("G6 2026", "family_adjacent"), ("2026 XPeng G6", "family_adjacent"),
                       ("XPeng G6 (2026)", "family_adjacent"), ("שנת דגם: 2026", "label"), ("שנתון 2026", "label"),
                       ("model year 2026", "label"), ("MY2026", "label"), ("MY26", "label")):
        ctx = year_context(text, ident)
        assert ctx["years"] == {2026} and [s["kind"] for s in ctx["statements"]] == [kind], text
    # a year next to ANOTHER model family or in prose is no statement for the G6
    assert year_context("XPeng G9 2024", ident)["years"] == set()
    assert year_context("החברה נוסדה ב-2014 ומוכרת היום", ident)["ignored"] == [{"year": 2014, "kind": "prose"}]


@pytest.mark.parametrize("text,kind", [
    ("© 2023 XPeng G6. All rights reserved", "copyright"),
    ("XPeng G6 | כל הזכויות שמורות 2023", "copyright"),
    ("XPeng G6 - פורסם 2023", "publication_date"),
    ("Updated: 2023 XPeng G6 review", "publication_date"),
    ("XPeng G6 נבחן במרץ 2023", "date"),
    ("XPeng G6 tested 12 March 2023", "date"),
    ("XPeng G6 15/03/2023", "date"),
    ("XPeng G6 מחירון בתוקף החל מ-2023", "price_validity"),
    ("XPeng G6 www.example.co.il/2023/review", "url"),
])
def test_publication_copyright_and_dates_are_ignored(text, kind):
    ctx = year_context(text, xpeng())
    assert ctx["years"] == set() and {"year": 2023, "kind": kind} in ctx["ignored"]
    assert statuses(text, xpeng())["year"] == "absent"


def test_urls_count_only_as_an_explicit_family_year_slug():
    ident = xpeng()
    assert url_year_context("https://www.xpeng.co.il/2025/03/xpeng-g6-launch", ident)["years"] == set()
    assert url_year_context("https://www.xpeng.co.il/2025/03/xpeng-g6-launch", ident)["ignored"] == [
        {"year": 2025, "kind": "url"}]
    for url in ("https://www.xpeng.co.il/g6-2026/", "https://www.xpeng.co.il/2026-xpeng-g6/",
                "https://example.com/cars/g6_2026"):
        assert url_year_context(url, ident)["years"] == {2026}, url
    # in the document profile: the date directory no longer caps the binding at model_family, the slug decides
    dated = profile(title="XPeng G6 launch", url="https://www.xpeng.co.il/2024/03/xpeng-g6-launch", h1=[])
    assert dated["statuses"]["year"] == "absent" and {"year": 2024, "kind": "url"} in dated["year_context"]["ignored"]
    assert profile(title="XPeng G6", url="https://www.xpeng.co.il/g6-2026/", h1=[])["statuses"]["year"] == "match"
    old = profile(title="XPeng G6", url="https://www.xpeng.co.il/g6-2024/", h1=[])
    assert old["statuses"]["year"] == "mismatch" and old["year_context"]["basis"] == "identity_zone"


def test_identity_segments_count_their_years_and_adjacent_never_caps_the_level():
    ident = xpeng()
    page = profile(title="XPeng G6 – מפרט טכני מלא לשנת 2025", h1=["XPeng G6"])
    assert page["statuses"]["year"] == "adjacent"
    assert page["year_context"]["statements"] == [{"year": 2025, "kind": "identity_segment"}]
    assert bind(ident, page["statuses"])["binding_level"] != "model_family"
    far = profile(title="XPeng G6 – מפרט טכני מלא לשנת 2024", h1=["XPeng G6"])
    assert bind(ident, far["statuses"])["binding_level"] == "model_family"
    # an H2 counts only when it names the family
    assert profile(title="XPeng", h1=["XPeng G6"], h2=["הדור של 2024"])["statuses"]["year"] == "absent"
    assert profile(title="XPeng", h1=["XPeng G6"], h2=["XPeng G6 2024"])["statuses"]["year"] == "mismatch"


# --- 4: page chrome and the identity zone -----------------------------------------------------------------------------

CHROME_PAGE = """<html><head><title>XPeng G6 | מפרט</title></head><body>
<header class="site-header"><nav><a href="/g9">XPeng G9 2024</a></nav><h1>XPeng G6 MAX 2026</h1></header>
<main><p>רכב חשמלי SUV, הנעה כפולה AWD</p><p>הספק מרבי: 486 כ"ס</p><p>משקל עצמי: 2,180 ק"ג</p></main>
<footer><p>XPeng G6 RWD 296 כ"ס - הדגם הבסיסי</p><p>אורך: 4,753 מ"מ</p><p>© 2024 XPeng G6 Israel</p></footer>
</body></html>"""


def test_the_identity_zone_keeps_a_header_h1_and_the_footer_leaves_the_full_text(tmp_path):
    cache = DocumentCache(tmp_path / "cache")
    url = "https://www.xpeng.co.il/g6/specifications"
    doc = put(cache, url, CHROME_PAGE, "html")
    adm = AdmissionContext.for_run(XPENG, None, SPECS, "IL")
    material = adm.material(cache, doc, None)
    st = material.profile["statuses"]
    assert st["year"] == "match"                                   # <header><h1>XPeng G6 MAX 2026</h1></header>
    assert material.profile["year_context"]["basis"] == "identity_zone"
    # the footer's RWD 296 hp no longer makes power / drivetrain mixed in the full-text profile
    assert st["drivetrain"] == "match" and st["power"] == "match"
    assert "RWD" not in (material.body_text or "") and "G9" not in (material.body_text or "")
    # without the chrome rules (the stored text) the same page was mixed
    raw = document_profile(text=material.text, title=material.meta.get("title"), url=url, identity=adm.identity,
                           headings=material.headings)
    assert raw["statuses"]["drivetrain"] == "mixed"
    # admission still verifies a quote that sits in the footer (it reads the untouched document)
    decision = admit(adm, cache, {"field": "length_mm", "value": 4753, "document_id": doc,
                                  "quote": 'אורך: 4,753 מ"מ'}, [doc])
    assert decision["accepted"], decision


# --- 5: golden ----------------------------------------------------------------------------------------------------------

def test_corolla_golden_exact_and_different_are_unchanged_and_only_unclear_rises_with_a_basis():
    from fixtures import corolla_binding

    golden = json.loads(corolla_binding.GOLDEN.read_text("utf-8"))
    current = corolla_binding.compute()
    assert set(current) == set(golden)
    for key, old in golden.items():
        new = current[key]
        if old["binding_level"].startswith("exact_") or old["variant_match"] == "different":
            assert {k: new.get(k) for k in ("binding_level", "variant_match")} == \
                {k: old.get(k) for k in ("binding_level", "variant_match")}, key
        elif new != old:
            assert old["variant_match"] == "unclear", key
            assert LEVELS.index(new["binding_level"]) > LEVELS.index(old["binding_level"]), key
            assert new.get("binding_basis"), key


# --- 6: binding gap semantics and year telemetry ----------------------------------------------------------------------

def test_binding_gap_never_reports_adjacent_or_ignored_years():
    ident = xpeng()
    adjacent = bind(ident, statuses("XPeng G6 2025 SUV חשמלי", ident), requirement="exact_technical_variant")
    assert adjacent["binding_dimensions"]["year"]["status"] == "adjacent"
    assert not any(g.startswith("year") for g in binding_gaps(adjacent))
    ignored = bind(ident, statuses("© 2023 XPeng G6 SUV חשמלי", ident), requirement="exact_technical_variant")
    assert "year" not in ignored["binding_dimensions"]
    assert not any(g.startswith("year") for g in binding_gaps(ignored))
    blocked = bind(ident, statuses("XPeng G6 2023 SUV חשמלי", ident), requirement="exact_technical_variant")
    assert blocked["binding_level"] == "model_family" and binding_gaps(blocked) == ["year_mismatch"]


def test_year_telemetry_counts_over_admitted_evidence():
    from src import diagnostics as D

    def ev(eid, year=None, ignored=()):
        dims = {"model": {"status": "match"}, **({"year": {"status": year}} if year else {})}
        return {"evidence_id": eid, "field": "length_mm", "value": 4753, "market": "IL",
                "admission_status": "accepted", "binding_level": "body_powertrain", "variant_match": "unclear",
                "binding_dimensions": dims, **({"year_context": {"ignored": [{"year": 2024, "kind": k}
                                                                             for k in ignored]}} if ignored else {})}
    items = [ev("e1", "match"), ev("e2", "adjacent", ("copyright", "url")), ev("e3", None, ("copyright",)),
             ev("e4", "mismatch"), ev("e5", "mixed")]
    events = [{"kind": "run_started", "seq": 0, "target_market": "IL",
               "requested_field_specs": [{"name": "length_mm", "applicable": True}]}] + \
        [{"kind": "evidence", "seq": i + 1, "evidence": e} for i, e in enumerate(items)]
    summary = D.binding_year_summary(events)
    assert summary["binding_year_status_counts"] == {"match": 1, "adjacent": 1, "mixed": 1, "mismatch": 1,
                                                     "absent": 1}
    assert summary["ignored_year_contexts"] == {"copyright": 2, "url": 1}
    row = D.vehicle_row(D.vehicle_diagnostics(events))
    assert json.loads(row["binding_year_status_counts"])["adjacent"] == 1
    assert json.loads(row["ignored_year_contexts"]) == {"copyright": 2, "url": 1}
    gaps = D.final_field_states(events)["binding_gap_counts"]
    assert not any(g.startswith("year_adjacent") for g in gaps)


# --- 8: version -----------------------------------------------------------------------------------------------------------

def test_binding_v3_and_memory_ignores_facts_of_binding_v2(tmp_path, monkeypatch):
    from fixtures.corolla_touring import PAYLOAD as COROLLA, VEHICLE as COROLLA_VEHICLE
    from src.research_memory import ResearchMemory

    assert BINDING_VERSION == "binding-v3"
    hev = resolve_requested_fields(None, propulsion="hybrid")
    identity = target_identity(COROLLA, COROLLA_VEHICLE)
    fact = {"evidence_id": "e1", "field": "fuel_tank_l", "value": 43, "unit": "l", "document_id": "d1",
            "quote": "Fuel tank capacity 43 l", "market": "UK", "variant_match": "exact",
            "binding_level": "exact_technical_variant", "source_authority": "official_manufacturer",
            "admission_status": "accepted"}
    ok = [{"field": "fuel_tank_l", "state": "ok", "evidence_ids": ["e1"], "conflict_evidence_ids": []}]
    memory = ResearchMemory(tmp_path)
    monkeypatch.setattr(research_memory, "BINDING_VERSION", "binding-v2")
    assert memory.record_facts([fact], hev, identity, {"record_id": "38626"}, ok)
    monkeypatch.setattr(research_memory, "BINDING_VERSION", BINDING_VERSION)
    assert memory.reusable_facts(hev, identity) == ([], {"stale_schema_or_gate_version": 1})


# --- 2: Binding Replay on a fixture run ------------------------------------------------------------------------------

LAUNCH = "https://www.xpeng.co.il/2024/03/xpeng-g6-launch"         # a date-directory URL: capped under binding-v2
LAUNCH_HTML = """<html><head><title>XPeng G6 | השקה בישראל</title></head><body><h1>XPeng G6</h1>
<p>רכב חשמלי SUV, הנעה כפולה AWD</p><p>הספק מרבי: 486 כ"ס</p><p>משקל עצמי: 2,180 ק"ג</p>
<p>אורך: 4,753 מ"מ</p></body></html>"""
TABLE = "https://www.xpeng.co.il/g6/specifications"


def _digest(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def replay_fixture(tmp_path):
    """A finished-run folder (runs/R1/101122) and a shared cache: two curb-weight evidence items recorded under
    binding-v2 (the launch page capped at model_family by its date-directory URL; the spec table's 2,180 in two target
    columns), one item citing a document the cache lost, and an open length field with candidates only."""
    cache = DocumentCache(tmp_path / "cache")
    launch = put(cache, LAUNCH, LAUNCH_HTML, "html")
    columns = admission_records.XPENG_COLUMNS + [("MAX Plus", 357, 486, "AWD", "2,180")]
    rows = [["גרסה"] + [c[0] for c in columns], ["הספק (kW)"] + [f"{c[1]} kW" for c in columns],
            ['הספק (כ"ס)'] + [f'{c[2]} כ"ס' for c in columns], ["הנעה"] + [c[3] for c in columns],
            ['משקל עצמי (ק"ג)'] + [c[4] for c in columns]]
    table_html = ("<html><head><title>XPeng G6 2026 - מפרט טכני</title></head><body><h1>XPeng G6</h1><p>רכב חשמלי "
                  "SUV</p><table>" + "".join("<tr>" + "".join(f"<td>{x}</td>" for x in r) + "</tr>" for r in rows)
                  + "</table></body></html>")
    table = put(cache, TABLE, table_html, "html")
    specs = resolve_requested_fields(["curb_weight_kg", "length_mm"], propulsion="battery_electric")
    adm = AdmissionContext.for_run(XPENG, None, specs, "IL")
    events = [{"kind": "run_started", "seq": 1, "target_market": "IL", "record_id": "101122", "vehicle_label": {},
               "requested_field_specs": [{"name": s["name"], "applicable": True} for s in specs]}]
    evidence = []
    for doc, url in ((launch, LAUNCH), (table, TABLE)):
        cands, _ = harvest_document(cache, doc, specs)
        events.append({"kind": "candidates_harvested", "seq": len(events) + 1, "document_id": doc, "url": url,
                       "candidates": cands})
        cand = next(c for c in cands if c["field"] == "curb_weight_kg" and c["value"] == 2180)
        decision = admit(adm, cache, candidate_request("curb_weight_kg", {**cand, "document_id": doc}), [doc])
        assert decision["accepted"], decision
        evidence.append({**decision["record"], "evidence_id": f"e{len(evidence) + 1}"})
    # as binding-v2 recorded the launch page: its URL year capped it at model_family
    evidence[0].update(binding_level="model_family", variant_match="unclear",
                       binding_dimensions={**evidence[0]["binding_dimensions"], "year": {"status": "mismatch",
                                                                                         "basis": "document"}})
    evidence[0].pop("year_context", None)
    evidence.append({**evidence[1], "evidence_id": "e3", "document_id": "0" * 24, "source_url": "https://gone.example"})
    for item in evidence:
        events.append({"kind": "evidence", "seq": len(events) + 1, "evidence": item})
    run_dir = tmp_path / "runs" / "R1" / "101122"
    run_dir.mkdir(parents=True)
    (run_dir / "input.json").write_text(json.dumps(XPENG, ensure_ascii=False), "utf-8")
    (run_dir / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), "utf-8")
    return run_dir, tmp_path / "cache", {"launch": launch, "table": table}


def test_binding_replay_on_a_fixture_run(tmp_path):
    from src import binding_replay as R

    run_dir, cache_dir, docs = replay_fixture(tmp_path)
    before_run, before_cache = _digest(run_dir), _digest(cache_dir)
    out = R.replay_run(run_dir, cache_dir)
    # read-only: the run's own files and the shared cache are untouched; only the two replay files are new
    after = _digest(run_dir)
    assert {k: v for k, v in after.items() if k not in (R.REPLAY_FILE, R.SUMMARY_FILE)} == before_run
    assert set(after) - set(before_run) == {R.REPLAY_FILE, R.SUMMARY_FILE}
    assert _digest(cache_dir) == before_cache
    items = {r["evidence_id"]: r for r in out["items"] if r["kind"] == "evidence"}
    # e1: the date-directory URL no longer caps the launch page; it rose because of the year rules
    e1 = items["e1"]
    assert (e1["binding_level_recorded"], e1["binding_level_now"]) == ("model_family", "exact_technical_variant")
    assert e1["variant_match_now"] == "exact" and e1["rose_by_year_rules"] and e1["binding_gap_now"] == []
    assert {"year": 2024, "kind": "url"} in e1["year_context"]["ignored"]
    assert e1["document_statuses"]["year"] == "absent" and e1["zone_statuses"]["model"] == "match"
    assert {"quote", "source_line"} <= set(e1["layer_statuses"]) and e1["layer_statuses"]["quote"]["year"] == "absent"
    assert all(set(st) >= {"model", "power", "drivetrain", "trim", "year"} for st in e1["layer_statuses"].values())
    assert e1["candidate_match_count"] == 1 and not e1["candidate_match_ambiguous"]
    assert e1["matching_candidates"][0]["quote"] and "row_index" in e1
    # e2: the table's 2,180 sits in two target columns: one candidate, two column identities -> ambiguous, unclear
    e2 = items["e2"]
    assert e2["candidate_match_count"] == 1 and e2["candidate_match_ambiguous"]
    assert " || " in e2["matching_candidates"][0]["column_identity"] and e2["effective_column_identity"] is None
    assert e2["variant_match_now"] == "unclear" and e2["candidates_source"] == "run_events"
    # e3: its document is in neither the shared cache nor the run folder: reported, not fatal
    assert items["e3"]["missing_document"] and "binding_level_now" not in items["e3"]
    summary = out["summary"]
    assert summary["missing_documents"] == [{"document_id": "0" * 24, "source_url": "https://gone.example"}]
    # would_be_ok: the evaluator over an in-memory copy with the replayed binding
    field = summary["fields"]["curb_weight_kg"]
    assert field["state_recorded"] == "variant_not_exact" and field["would_be_state"] == "ok" and field["would_be_ok"]
    assert field["best_variant_match_now"] == "exact" and e2["would_be_ok"]
    vehicle = summary["vehicle"]
    assert (vehicle["fields_ok_recorded"], vehicle["fields_ok_now"]) == (0, 1)
    assert vehicle["evidence_rose_by_year_rules"] == 1 and vehicle["missing_documents"] == 1
    assert vehicle["gap_counts"] and "year_mismatch" not in vehicle["gap_counts"]
    # the open length field: its candidates get the same binding fields (no admission)
    length = [r for r in out["items"] if r["kind"] == "candidate" and r["field"] == "length_mm"]
    assert length and all("binding_level_now" in r and r["evidence_id"] is None for r in length)
    assert summary["fields"]["length_mm"]["state_recorded"] == "missing"
    # the files on disk are the same replay; a second call with the same code reads them back
    assert R.load_replay(run_dir)["summary"] == json.loads((run_dir / R.SUMMARY_FILE).read_text("utf-8"))
    assert len((run_dir / R.REPLAY_FILE).read_text("utf-8").splitlines()) == len(out["items"])


def test_binding_replay_cli_and_diagnostics(tmp_path, capsys):
    from src import binding_replay as R
    from src import diagnostics as D

    run_dir, cache_dir, _ = replay_fixture(tmp_path)
    assert R.main(["--runs-dir", str(tmp_path / "runs"), "--all", "--cache-dir", str(cache_dir)]) == 0
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[0])
    assert printed["fields_ok_now"] == 1 and printed["run_dir"] == str(run_dir)
    assert R.main(["--run-dir", str(run_dir), "--cache-dir", str(cache_dir)]) == 0
    diag = D.load_vehicle_diagnostics(run_dir)
    row = D.vehicle_row(diag)
    assert row["replay_fields_ok_now"] == 1 and row["replay_fields_ok_recorded"] == 0
    assert json.loads(row["replay_gap_counts"]) == diag["binding_replay"]["gap_counts"]

    # A replay is a cache of today's binding, not historical truth. After a binding-code change diagnostics must not
    # silently surface old replay numbers as current benchmark results.
    summary_path = run_dir / R.SUMMARY_FILE
    stale = json.loads(summary_path.read_text("utf-8"))
    stale["code_version"] = "binding-v3:stale"
    summary_path.write_text(json.dumps(stale), "utf-8")
    stale_diag = D.load_vehicle_diagnostics(run_dir)
    assert "binding_replay" not in stale_diag
    stale_row = D.vehicle_row(stale_diag)
    assert stale_row["replay_fields_ok_now"] is None and stale_row["replay_gap_counts"] is None


# --- 7: recovery spend while binding is the blocker -------------------------------------------------------------------

from src.tail_planner import binding_budget, field_binding_gaps  # noqa: E402

OFFICIAL_IL = {"source_authority": "official_importer", "market": "IL"}


def _item(eid, level, match="unclear", **kw):
    return {"evidence_id": eid, "binding_level": level, "variant_match": match,
            "binding_requirement": "exact_technical_variant", **OFFICIAL_IL, **kw}


def test_binding_budget_by_gap_kind():
    evaluation = {"tech": {"state": "variant_not_exact", "info": ["binding_gap:power_mixed"]},
                  "trim": {"state": "variant_not_exact", "info": ["binding_gap:trim_absent"]},
                  "year": {"state": "variant_not_exact", "info": ["binding_gap:year_mismatch"]},
                  "gone": {"state": "missing", "info": []},
                  "publisher": {"state": "variant_not_exact", "info": ["binding_gap:power_mixed"]}}
    evidence = {"tech": [_item("a", "body_powertrain")], "trim": [_item("b", "exact_technical_variant")],
                "year": [_item("c", "model_family")],
                "publisher": [{**_item("d", "body_powertrain"), "source_authority": "publisher"}]}
    tech = binding_budget(["tech"], evaluation, evidence, "IL")
    assert (tech["reason"], tech["search_cap"], tech["technical_fields"]) == ("technical_gap", 2, ["tech"])
    assert binding_budget(["year"], evaluation, evidence, "IL")["search_cap"] == 2
    trim = binding_budget(["trim"], evaluation, evidence, "IL")
    assert (trim["reason"], trim["search_cap"], trim["trim_fields"]) == ("trim_gap", 1, ["trim"])
    assert binding_budget(["tech", "trim"], evaluation, evidence, "IL")["search_cap"] == 2
    # a true missing field (or a field without official target-market evidence) keeps the cluster's budget
    assert binding_budget(["tech", "gone"], evaluation, evidence, "IL")["search_cap"] is None
    assert binding_budget(["gone"], evaluation, evidence, "IL") == {"reason": "other", "search_cap": None, "gaps": {},
                                                                    "technical_fields": [], "trim_fields": []}
    assert binding_budget(["publisher"], evaluation, evidence, "IL")["search_cap"] is None


def test_a_conflicting_fields_gap_comes_from_its_conflict_evidence():
    trim_items = [_item("x1", "exact_technical_variant", binding_requirement="exact_market_trim", value=1),
                  _item("x2", "exact_technical_variant", binding_requirement="exact_market_trim", value=2),
                  _item("x3", "body_powertrain", value=3)]          # not in the conflict: its gap does not count
    entry = {"state": "conflicting", "info": [], "conflict_evidence_ids": ["x1", "x2"]}
    assert field_binding_gaps(entry, trim_items, "IL") == ["trim_absent"]
    assert binding_budget(["f"], {"f": entry}, {"f": trim_items}, "IL")["search_cap"] == 1
    entry["conflict_evidence_ids"] = ["x1", "x3"]
    assert "trim_absent" in field_binding_gaps(entry, trim_items, "IL")
    assert binding_budget(["f"], {"f": entry}, {"f": trim_items}, "IL")["reason"] == "technical_gap"
    # a value conflict between exactly bound items is no binding gap: the normal budget
    exact = [_item("y1", "exact_technical_variant", "exact"), _item("y2", "exact_technical_variant", "exact")]
    entry = {"state": "conflicting", "info": [], "conflict_evidence_ids": ["y1", "y2"]}
    assert field_binding_gaps(entry, exact, "IL") is None
    assert binding_budget(["f"], {"f": entry}, {"f": exact}, "IL")["search_cap"] is None


DIMENSIONS_PAGE = "https://www.toyota.co.il/new-cars/corolla-touring-sports/dimensions"
DIMENSIONS_HTML = """<html><head><title>טויוטה קורולה טורינג ספורט 2024 - מידות</title></head><body>
<h1>טויוטה קורולה טורינג ספורט 2024</h1><p>אורך: 4,650 מ"מ</p></body></html>"""


@pytest.mark.recovery_mode("reacquire")
def test_a_technical_gap_cluster_gets_two_searches_and_the_spec_page_instruction(tmp_path):
    def episodes(packet, turn_no):
        if turn_no == 1:
            return turn(*[_call(f"s{i}", "search_web", {"query": f"corolla touring sports spec {i}"})
                          for i in range(4)])
        return say({"done": True, "reason": "nothing more"})

    routes = {**ROUTES, DIMENSIONS_PAGE: (DIMENSIONS_HTML, "text/html")}
    client = ReacquireClient([fetch("a", DIMENSIONS_PAGE), say({"done": True, "reason": "enough"})], episodes)
    result, events = run(tmp_path, client, routes=routes, acquisition_mode="contract", requested_fields=["length_mm"],
                         cluster_search_budget=4, **GATE_OFF)
    stored = [e["evidence"] for e in events if e["kind"] == "evidence"]
    assert stored and stored[0]["source_authority"] == "official_importer" and stored[0]["variant_match"] == "unclear"
    rec = result["field_recovery"]
    assert rec["triage"]["length_mm"]["binding_gap"] and not any(
        g.startswith("trim") for g in rec["triage"]["length_mm"]["binding_gap"])
    packet = client.reacquire_requests[0]["packet"]
    assert packet["budget"]["billable_searches"] == 2 and packet["technical_binding_fields"] == ["length_mm"]
    assert "official specification page or specification PDF" in packet["technical_binding_instruction"]
    episode = rec["attempts"][0]
    assert episode["search_budget"] == 2 and episode["billable_searches"] <= 2
    assert episode["budget_reason"] == "technical_gap"
    started = next(e for e in events if e["kind"] == "reacquire_started")
    assert started["budget_reason"] == "technical_gap" and started["binding_gaps"]["length_mm"]
    from src import diagnostics as D
    assert D.recovery_summary(events)["reacquire_searches_by_reason"] == {"technical_gap": episode["billable_searches"]}
    assert rec["reacquire_searches_by_reason"] == {"technical_gap": episode["billable_searches"]}


@pytest.mark.recovery_mode("reacquire")
def test_the_stage_cap_holds_across_clusters_and_true_missing_keeps_its_budget(tmp_path):
    def episodes(packet, turn_no):
        if turn_no == 1:
            return turn(*[_call(f"s{i}", "search_web", {"query": f"corolla {packet['cluster']} {i}"})
                          for i in range(4)])
        return say({"done": True, "reason": "nothing more"})

    from fixtures import corolla_tail as tail
    client = ReacquireClient([fetch("a", tail.FORUM), say({"done": True, "reason": "enough"})], episodes)
    result, events = run(tmp_path, client, acquisition_mode="contract", cluster_search_budget=4,
                         requested_fields=["length_mm", "top_speed_kmh", "list_price"], **GATE_OFF)
    rec = result["field_recovery"]
    budgets = [a["search_budget"] for a in rec["attempts"]]
    assert [a["cluster"] for a in rec["attempts"]] == ["technical_spec", "performance", "commercial"]
    assert budgets == [4, 4, 0]                       # true missing: the cluster budget, until the stage cap of 8
    assert sum(a["billable_searches"] for a in rec["attempts"]) <= 8 == rec["reacquire_stage_search_cap"]
    assert rec["attempts"][2]["search_refused"] >= 1 and rec["attempts"][2]["billable_searches"] == 0
    assert {a["budget_reason"] for a in rec["attempts"]} == {"other"}
    started = [e for e in events if e["kind"] == "reacquire_started"]
    assert started[-1]["stage_searches_left"] == 0 and started[-1]["stage_search_cap"] == 8


def test_the_stage_cap_is_pinned_by_named_profiles_and_env_only_for_custom():
    from src.agent import AgentConfig, agent_config_from_env
    from src.run_profiles import CUSTOM, PRODUCTION, TREATMENT, build_agent_config

    import dataclasses
    assert {f.name: f.default for f in dataclasses.fields(AgentConfig)}["reacquire_stage_search_cap"] == 8
    env = {"REACQUIRE_STAGE_SEARCH_CAP": "3"}.get
    assert agent_config_from_env(env).reacquire_stage_search_cap == 3
    assert build_agent_config(env, profile=CUSTOM).reacquire_stage_search_cap == 3
    assert build_agent_config(env, profile=PRODUCTION).reacquire_stage_search_cap == 8
    assert build_agent_config(env, profile=TREATMENT).reacquire_stage_search_cap == 8
