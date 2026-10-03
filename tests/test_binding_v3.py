"""PR #35 (binding-v3): the binding-layer helper shared by Evidence Admission and Binding Replay (1), Binding Replay on a
fixture run (2), model-year rules (3), page chrome vs the identity zone (4), the Corolla golden (5), binding gap
semantics and year telemetry (6), recovery spend while binding is the blocker (7) and the version bump (8). Offline."""

import json

from fixtures import admission_records


# --- 1: refactor safety ---------------------------------------------------------------------------------------------

def test_admission_decisions_are_unchanged_by_the_helper_extraction():
    """The golden was written on main before binding_layers / fact_binding were extracted from admit()."""
    golden = json.loads(admission_records.GOLDEN.read_text("utf-8"))
    current = admission_records.compute()
    assert set(current) == set(golden)
    for key, old in golden.items():
        new = current[key]
        assert new["accepted"] == old["accepted"], key
        if not old["accepted"]:
            assert new["reasons"] == old["reasons"], key
            continue
        strip = lambda r: {k: v for k, v in r.items() if k != "binding_version"}  # noqa: E731
        assert strip(new["record"]) == strip(old["record"]), key
