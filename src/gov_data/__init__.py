"""Government datasets of data.gov.il ingested into TRIPY and served through the facts API (`vehicle-facts/1`).

Architecture decision (owner): every vehicle data source is ingested in TRIPY and served to yeda-rechev through the
facts API; nothing is written to MILO (TRIPY's MILO access is read-only). The datasets become committed, aggregated
snapshots in data/gov/ built by the build-gov-datasets GitHub Action (the open-data pattern):

    new_car_prices   the new-car price list         -> per (tozeret_cd, degem_cd, shnat_yitzur) every distinct
                                                       (mehir, kinuy_mishari, shem_yevuan, ...) with its row count
    recall_notices   the recall notices              -> per recall: make, model (DEGEM), production range, fault
    road_survival    the final cancellations (three   -> per (tozeret_cd, degem_cd, cohort year): cohort size,
                     files) + the active registry       cancelled count, age at cancellation, cancelled share by age

Model / model-year level only: no per-vehicle record is stored or served; licence-plate, chassis and engine numbers
are dropped by the projection while the stream is read and never written, logged or sampled.

    ckan.py        resource_show / package_show / datastore_search (QA samples and the datastore fallback); the
                   file request (headers, one retry, redirects only to *.gov.il, diagnosable errors)
    download.py    the streamed download: sha256, size, the HTML / error-body guard, the projected CSV reader
    schemas.py     the dataset config (data/gov_datasets.json), column resolution, the schema hash
    provenance.py  the per-resource provenance record and the fail-safe checks (licence, format, row count)
    ingest.py      one resource end to end: CKAN metadata, the checks, the streamed projected rows (the file, else the
                   datastore), the provenance
    names.py       the exact normalizations (numbers, dates, model names, make resolution via make_canonical.json)
    snapshot.py    deterministic SQLite snapshots, gzip and the read side (sha256-verified, decompressed once)
    prices.py / recalls.py / survival.py   the aggregations and their reports
    facts.py       the facts API fields (original_new_price_ils, original_importer, recalls, recall_count,
                   road_survival)
"""

INGESTION_VERSION = "gov-ingestion-v1"
SOURCE_LEVEL = "government_dataset"
SOURCES = {"new_car_prices": "gov_new_car_prices", "recall_notices": "gov_recall_notices",
           "road_survival": "gov_road_survival"}
