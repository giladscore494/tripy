# Codex handoff: finish the existing PR36 draft on TRIPY

Repository: `giladscore494/tripy`.
Branch: `codex/automatic-binding-replay-candidate-hygiene`.
Original base: `b933853820e9dd5efba16436f85d1ada021c0acb` (PR35 merged).

Continue this branch and update its existing draft PR. Do not create another PR or merge. The user asked the
previous agent to checkpoint its current work, open a PR, stop, and provide this continuation prompt.
Read the current diff and the complete original user task reproduced below before making changes.
The draft is partial, and its regression suite is not green. Do not treat existing implementation as correct merely
because it is present. Complete and verify the ORIGINAL task, keeping the user's constraints.

## User-authorized correction to the original category assumption

The original B1 assumed Level 1.5 contains regulatory M1/N1 classes. `src/db.py::build_level15_payload` and all 50 rows
of `data/benchmark_v1_level15_snapshot.json` expose `identity.segment` / `raw_row.vehicle_segment` (`private` or
`commercial`), and `government_codes.sug_degem` (`P` / `M`); they do not supply M1/N1.
The user explicitly authorized correcting the source and missing-category behavior and continuing.
Do NOT equate P/M or private/commercial to regulatory M1/N1.
Current draft uses:
- Price scoped minimum: known `private` -> 20,000 ILS; missing/unrecognized segment -> existing global bounds.
- Rims: default whole inches; half inches only for known `commercial` AND recorded gross weight >3500 kg. This is an
  operational heavy-commercial rule, not a claim about regulatory classification. Missing weight/segment keeps
  the conservative whole-inch default. The predicate and threshold live in the dictionary.
- Source precedence: `identity.segment`, then `raw_row.vehicle_segment`, then explicit vehicle metadata; gross
  weight from `structure.gross_weight_kg` / `raw_row.mishkal_kolel`. Review cache/context behavior end-to-end.
These corrected rules override only the unavailable M1/N1 assumption in the original prompt.

## What has been implemented so far (review it; some requirements remain incomplete)

- `src/binding_replay.py`: run-end hook, subprocess-bounded lazy replay (30 s default), extended field summaries,
  binding-level histogram/common blocker, replay-v2 and code/data cache invalidation. The lazy child computes with
  write=False and only the parent publishes a complete replay; a timeout kills the child.
- Hooks after result persistence in `run_vehicle` and `finalize_existing_run`.
- `src/diagnostics.py`: lazy replay for finished diagnostics, replay_error, per-vehicle fields, compact per-field rows,
  and `binding_replay_items.jsonl` benchmark output.
- `app.py` / `src/ui/diagnostics_view.py`: automatic main-area and secondary Binding views, error boundaries across
  secondary tabs, summary/field table and third items download.
- `src/fields.py` / dictionary: scoped plausibility, wheel steps, geometry sibling/axis metadata and text-value rules.
- `src/candidate_harvest.py`: geometry ownership/composite triples, semantic sanity at harvest, tyre span/axle rules,
  rejection collection/cache/events, initial origin/block metadata, kgf.m conversion and catalog trim hints.
- `src/evidence_admission.py`: shared semantic sanity, contextual specs, matching geometry/tyre ownership, initial
  navigation trim rejection. Review these changes especially carefully: admission must stay strict, and accepted
  facts and binding must not change without a justified semantic/parse correction.
- Added `tests/test_pr36_replay.py` and `tests/test_pr36_candidate_sanity.py`. The old on-demand UI expectation and a
  diagnostics reload assertion were updated for automatic replay metadata.

## Remaining work required by the ORIGINAL task

1. Fix all regressions first. Some old expectations need a justified update (on-demand replay, harvest-v5, or
   cross-propulsion cache reuse); others may reveal real lost correct candidates. For golden admission/binding
   failures, explain every changed/new/missing case and preserve binding rules. Do not blindly regenerate goldens,
   skip failing tests, or relax assertions simply to make CI green.
2. Finish B0: origin/rejection columns in the field table AND CSV export, every shown value linked to its actual
   layer/document/block, rejected values displayed as rejected rather than live candidates. Current parser rejection
   events and partial metadata alone do NOT satisfy B0. Include sweep/grounded/recovery proposals and rejection paths.
3. Finish B1: one synthetic regression per original table row and a passing normal-value test for every added
   exclusion. Initial focused tests cover only a subset. Audit model-proposed paths through the single admission gate,
   implement the unit-once charging-time formatter, improve multi-model trim rejection beyond one heuristic regexp,
   and verify front/rear ownership and multiple adjacent statements in every applicable origin (tables, DOM, PDF,
   unit anchor). Prove the actual production origin when artifacts exist; label unreproduced cases honestly.
4. Add B2 end-to-end fixture: 2100/2190 curb weights remain a genuine conflict; 750 kg braked towing is rejected with
   reason and does not participate. Do not change conflict/binding logic to hide 2100 vs 2190.
5. Complete Part A tests: force UI exceptions and verify st.error, verify finalize-existing replay hook, whole-result
   preservation on failures, bounded lazy retries/cache behavior, and every requested benchmark field/download.
6. A5 is NOT proved for the reported production incident. Code inspection proves the replay was on-demand and the
   benchmark only loaded saved replay, which explains null exports. `scripts/start.sh` hides error details, and a
   preceding tab exception could abort the Binding render; the draft isolates sibling views. Do NOT claim these
   observations prove why the reported production tab was empty. Reproduce with production artifacts/logs when
   available, or explicitly document the evidence limit and a synthetic failure-path reproduction.
7. Run the full suite, review the final diff against EVERY A1-A5/B0-B3 requirement below, update the same PR description
   with per-defect origin/fix, remaining unreproduced defects, and exact test results. Leave unmerged for review.

No production run files/cache for `20261003T193112Z` were attached to this coding session. The source of the reported
bad candidates cannot be established from the task text alone. Do not invent provenance, page text or a production
root cause. Do not invoke paid models or network enrichment merely to fill this gap without a separate user request.

## Validation at checkpoint

- Focused replay, candidate sanity, dashboard and diagnostics: **52 passed**.
  Command: `python -m pytest -q tests/test_pr36_replay.py tests/test_pr36_candidate_sanity.py tests/test_dashboard_app.py tests/test_diagnostics.py`.
- Full suite with external requests blocked: **852 passed, 9 failed**, 138.34 seconds.
  Command: `PYTHONPATH=tests python -m pytest -p offline_http_guard`.
  `tests/offline_http_guard.py` blocks non-loopback HTTP through requests.Session.send before transmission. This is
  an optional test-run plugin, not a production setting or a complete sandbox for every networking library.
- Automatic approval review blocked the ordinary run after detecting an api.z.ai request because the request
  payload and credential use were unknown. The guarded full-suite run above used a materially safer alternative;
  do not repeat live model requests merely to validate this PR.
- Python compilation and `git diff --check` passed.
- `src/document_binding.py` is unchanged; dictionary binding_requirement values are unchanged.

Failing tests (must investigate before making the PR ready):

```
FAILED tests/test_binding_v3.py::test_admission_decisions_are_unchanged_by_the_helper_extraction
FAILED tests/test_binding_v3.py::test_corolla_golden_exact_and_different_are_unchanged_and_only_unclear_rises_with_a_basis
FAILED tests/test_candidate_harvest.py::test_tire_axles_are_never_assumed - A...
FAILED tests/test_candidate_harvest.py::test_one_document_is_parsed_once_for_all_fields_and_the_harvest_is_reused
FAILED tests/test_candidate_harvest.py::test_cadillac_fixture_harvests_all_documents_across_all_fields
FAILED tests/test_reliability_foundation.py::test_review_false_accepts_are_rejected[torque_nm-185-185 Nm-semantic_mismatch]
FAILED tests/test_structure_harvest.py::test_harvester_version_bumped - Asser...
FAILED tests/test_structure_harvest.py::test_corolla_coverage_table_before_vs_after
FAILED tests/test_variant_binding_v2.py::test_corolla_bindings_are_unchanged_or_rise_with_a_basis
```

The failures include changed tyre expectations/version/cache behavior, lost candidate coverage, and admission/binding
regressions. An expected behavioral change does not justify updating all these assertions or goldens blindly.


## Original user prompt (verbatim, except newline normalization)

The task below remains the acceptance specification, with only the continuation instruction and category-source
correction above taking precedence. Its instruction to open a PR is already satisfied by this draft: update that PR.

---

# TASK: Implement PR #36 "Automatic binding replay + candidate hygiene" in giladscore494/tripy

One pull request against current `main` (`b933853`, PR #35 merged). Read the code first; if anything below conflicts
with it, stop and report before changing it. Two independent parts. Do not touch binding rules
(`src/document_binding.py` levels, `binding_requirement` values); the next PR will change them from replay data.

Reference run (production, XPeng G6 2026 AWD MAX, run 20261003T193112Z): 0/43 ok; 68 admitted evidence items;
23 variant_not_exact, 14 missing, 5 conflicting. The field table export shows the defects listed in Part B. Note:
even `warranty_years` / `warranty_km` / `vehicle_warranty` (binding_requirement = body_powertrain) ended
variant_not_exact. That suggests binding fails below body_powertrain (generation / year / family). Part A must make
that visible.

---

## PART A — Automatic binding replay (no clicks, no env)

### Why
In production the "Binding" tab under Technical details renders EMPTY for a finished run: no caption, no button, no
error. `tests/test_dashboard_app.py::test_technical_details_binding_tab_runs_the_replay_on_demand` passes with
AppTest. The benchmark export already has `replay_fields_ok_now`, `replay_fields_ok_recorded` and
`replay_gap_counts`, but all of them are `null` for every vehicle, because the replay never ran. The replay is cheap
(no model, no network), so it must run automatically.

### Changes
A1. Run end: after `result.json` is persisted in `run_vehicle` (and in the finalize-existing path), call
    `binding_replay.replay_run(run_dir, cache_root, write=True)` inside try/except. Log either
    `binding_replay_written` (fields ok recorded → now, evidence exact recorded → now, gap counts) or
    `binding_replay_failed` (error). The replay must never change the run's status or result.
A2. Old runs: the diagnostics view and the benchmark export compute the replay lazily for every selected finished run
    that has no current replay summary (same code-version check as today). Per-run time limit: 30 s. On failure,
    record the error text in the row (`replay_error`) instead of null.
A3. Benchmark export (`diagnostics.aggregate` / `vehicle_row` / `write_benchmark`): every vehicle row includes
    `replay_fields_ok_recorded`, `replay_fields_ok_now`, `replay_evidence_exact_recorded`,
    `replay_evidence_exact_now`, `replay_rose_by_year_rules`, `replay_gap_counts`, `replay_missing_documents`,
    `replay_error`, and a compact `replay_fields` list with one entry per field:
    `{field, state_recorded, state_now, best_variant_match_now, best_binding_level_now, blocking_dims}`.
    Also add, per field, a histogram of binding levels reached by its evidence
    (`{"model_family": n, "generation": n, ...}`) and the most common blocking dimension. These answer "why is a
    value identical in every source (e.g. wheelbase) not exact?".
    Write `binding_replay_items.jsonl` (all replay rows of the selected runs) as a third download next to
    benchmark.json and parser_gaps.jsonl.
A4. UI: render the replay summary and per-field table in the run's main result area, under a "Binding replay"
    heading, with no button. Keep the Technical details tab as a secondary view. Wrap both renders in try/except and
    show `st.error` with the exception text on failure, so an empty area can never happen silently again.
A5. Find out why the tab rendered empty in production and fix the root cause. Candidates to check: tabs inside an
    expander inside `st.fragment(run_every=...)`; a widget key collision across fragment reruns; a production-only
    flag in `scripts/start.sh` or the Streamlit config (`client.showErrorDetails`, `logger.level`) that hides errors;
    an exception swallowed in `render_binding_replay`. Describe the cause in the PR.

---

## PART B — Candidate hygiene (wrong values must not become candidates or evidence)

Principle: `plausible_min/max` and `semantic_exclusions` are parser and admission sanity rules, never truth.
Everything stays data-driven in `data/enrichment_fields.json` where the mechanism exists. Add a new dictionary key
only when no existing key can express the rule, and document it in `_about`. No vehicle-specific code.

### B0. Provenance first
For every candidate value shown in the run's field table ("מועמדים שנמצאו") and its CSV export, record and show where
it came from: the harvest layer (`line`, `table`, `dom_pair`, `pdf_table_text`, `unit_anchor`, `column_identity`),
`sweep`, `grounded`, or `recovery`, plus the document id and block. If the value was rejected, also show the
rejection reason. Add two columns to the table and the export: `origin` and `rejection`. Fix every defect below at
its origin layer. If one of them comes from a model-proposed path (sweep / grounded) that bypassed a check, close
the bypass. Do not add a second filter downstream.

### B1. Observed defects → required behaviour (each needs a regression test with synthetic text that reproduces it)

| Field | Bad candidate seen | Required behaviour |
|---|---|---|
| width_mm, height_mm | width got 1650 (height) and 2890 (wheelbase); width 1920 never found; height list identical to width list | Dimension sibling disambiguation. A combined "L×W×H" (or "אורך/רוחב/גובה" in one row or line) is split in stated order. One number from one block/cell is a candidate for only ONE dimension field, chosen by its nearest own label. A number already claimed by a sibling dimension field's own label in the same block is never a candidate for another dimension field. |
| tire_size_front/rear | `235/60 R20` (no such tyre; source had `255/45R20` and `235/60R18`) | A tyre size is one contiguous match (width/aspect + construction + rim) from one span. Never recombine parts of different sizes. Front/rear only when the clause says front/rear, or a single size applies to all wheels. |
| rim_diameter_in | 17.5 | Passenger categories (Level 1.5 category M1 / N1 light): whole inches only. Half inches only for heavy commercial categories. Use a dictionary key (e.g. `allowed_step`, `step_by_category`), not code. |
| screen_size_in | 10.2 (driver cluster) next to 14.96 (centre) | Extend `semantic_exclusions`: instrument cluster / driver display / digital cockpit / HUD / לוח מחוונים / תצוגת נהג / צג נהג. Keep the existing "a different screen" rule. |
| curb_weight_kg | 750 | Extend `semantic_exclusions`: towing / braked / unbraked / payload / load capacity / גרירה / עם בלמים / מטען / כושר העמסה / gross / משקל כולל. |
| ac_max_charging_power_kw, dc_max_charging_power_kw | DC 10 kW and 5 kW, AC 5 kW | Exclusions for V2L / V2H / vehicle-to-load / discharge / פריקה / הזנה חיצונית. Raise `dc_max_charging_power_kw.plausible_min` to 20 (a DC fast-charge peak below 20 kW is not a fast-charge figure). Keep max 1000 (451 kW is real). |
| torque_nm | 67.3 Nm | Find the origin (B0). If it comes from an unrelated number with a unit anchor (e.g. kgf·m or another quantity), fix that layer. For battery_electric, set `plausible_min` to 100 via a propulsion-scoped range (`plausible_by_propulsion`), not globally. |
| vehicle_warranty / warranty_years / warranty_km vs battery_hybrid_warranty | 8 years / 160,000 km landed in vehicle_warranty while battery_hybrid_warranty stayed empty | A warranty clause that names battery / high-voltage / hybrid system / סוללה / מערכת היברידית / מתח גבוה is a candidate only for `battery_hybrid_warranty`. Use `semantic_exclusions` on the vehicle fields and the existing `warranty_kind` on the battery field. |
| local_trim_name | `P7+ G6 G9 X9` (navigation menu), table header text with units and VAT words | Never take a trim name from navigation / menu / footer blocks, from a block that lists several model names, or from a string that contains units, numbers with units, or metric words (צריכת, טווח, מחיר, מע"מ, kWh, km). Prefer candidates that match a trim token of this model in `data/catalog_trim_index.json`. A non-matching candidate gets a lower parser confidence and the hint `trim_not_in_catalog`; it is not dropped. |
| list_price | `6000 ILS` among 200k prices | Exclusions for fee / deposit / discount / הנחה / מקדמה / אגרה / תוספת / אביזר. Raise passenger-category `plausible_min` to 20,000 ILS via a category-scoped range. |
| dc_charging_time (display) | `12 min min` | The unit is duplicated in display and export. Format the value with its unit exactly once. Test the formatter. |
| cargo_volume_l | 571 and 1374 shown together | Already excluded by `semantic_exclusions` (seats-folded). Verify that 1374 is rejected at admission with that reason, and that the table shows the rejection (B0) rather than listing it as a live candidate. |

### B2. Conflicts
After B1, a field whose disagreement came only from values that are now rejected must not be `conflicting`. Add an
end-to-end fixture: three documents with 2100 / 2190 kg curb weight and one "750 kg braked towing". Result: 750 is
rejected with its reason, and the field's state is decided only by 2100 vs 2190. That pair is still a real conflict
unless binding separates them; do not change that logic.

### B3. Do not
- Do not loosen any binding rule, `binding_requirement`, or admission layer.
- Do not add vehicle-specific or brand-specific code paths.
- Do not drop a correct value to fix a wrong one. For every new exclusion, add a negative test showing the normal
  phrasing still passes (e.g. "משקל עצמי 2100 ק"ג" next to "גרירה 750 ק"ג" keeps 2100).

---

## TESTS
- Part A: a finished fixture run gets `binding_replay_summary.json` automatically; a forced replay failure is logged
  and leaves `result.json` unchanged. The benchmark export contains the new per-vehicle replay columns, the per-field
  binding-level histogram, and the items file; a run without a replay gets it computed lazily. The UI shows the
  replay summary for a finished run with no click; a forced exception renders an error box. Existing replay tests
  still pass.
- Part B: one regression test per row in B1, plus the negative test for each new exclusion; the B2 end-to-end fixture;
  the `origin` / `rejection` columns are present in the field table and the CSV export.
- Run the full suite.

## PR DESCRIPTION
- Root cause of the empty Binding tab.
- The automatic replay and the new benchmark fields.
- For each B1 row: the origin layer found, and the fix.
- Any defect you could not reproduce, stated explicitly.
- Test results.

Do not merge; open for review.