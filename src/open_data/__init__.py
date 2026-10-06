"""Open structured data layer (L1 / L2): local snapshots of open vehicle datasets, a deterministic international match
of the target's government identity, and per-field offers that the existing admission ladder may admit.

    datasets.py   data/open_datasets.json, the SQLite snapshots in <TRIPY_DATA_DIR>/derived/open/ (read-only at runtime)
    build.py      the snapshot builders (live header checked against the column map first; never a guessed column)
    job.py        the monthly background rebuild + "Rebuild now" (the RunManager's job, like the catalog trim index)
    match.py      route by approval type (sug_tkina), candidates, vetoes, `international_variant` per source
    offers.py     field offers with their definition tag and the route they may port to (D-4)
    engine.py     the run integration: events, shadow / admit modes, admission allowlist, research-plan reuse
    vpic.py       NHTSA vPIC partial-VIN decode (identity keys only; on demand, cached)

The model is never the source of a value or a URL here: every value is a dataset cell, every decision a data-driven
rule. Sources are fetched only when data/source_policy.json allows them.
"""

OPEN_DATA_VERSION = "open-data-v1"
MODES = ("off", "shadow", "admit")
DEFAULT_MODE = "shadow"
