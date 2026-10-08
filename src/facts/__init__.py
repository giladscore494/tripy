"""The vehicle facts API for yeda-rechev (`vehicle-facts/1`): ONE merged, provenance-tagged record per exact variant.

Architecture decision (owner): every vehicle data source is ingested, organized and identity-matched in TRIPY;
yeda-rechev never reads MILO, EEA, EPA, CVS, NRCan or ADEME itself. A record merges

    the government Level 1.5 row     MILO public.catalog_variants_current, read-only, one short query by
                                     variant_identity_key on every call (MILO updates appear without a sync)
    the open-data facts              the committed snapshots (data/open/, src/open_data): the deterministic match
                                     (match.py) and its offers (offers.py), admitted by data/facts_admission.json

Deterministic: no LLM, no web, no run, no paid call, no vPIC; nothing is ever written to MILO.

    versions.py   the data files (admission, zero semantics, contract) and the version strings of a record
    record.py     the pure record: government facts (zero semantics), open-data facts (admission, government wins,
                  two-source corroboration / conflict) and the withheld diagnostics
    service.py    the service: the government read, the open-data cache (in-process LRU + derived/facts_cache/),
                  the shard free-space guard, the call log
"""

CONTRACT = "vehicle-facts/1"
FACTS_ENGINE_VERSION = "facts-engine-v1"
