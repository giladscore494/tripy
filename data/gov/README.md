# Government-dataset snapshots

Built by the `build-gov-datasets` GitHub Action (`.github/workflows/build-gov-datasets.yml`,
`scripts/build_gov_datasets.py`, `src/gov_data`), never on the server. Aggregates only (model / model-year level): no
per-vehicle record is stored or served, and raw downloads never enter git.

- `new_car_prices.sqlite.gz` (G1): per `(tozeret_cd, degem_cd, shnat_yitzur)` every distinct (mehir, kinuy_mishari,
  shem_yevuan, ...) with its row count; the facts API serves one price as a value and several only as a range;
- `recall_notices.sqlite.gz` (G2): the recall notices with their canonical make, normalized DEGEM, production range and
  match status (TELEPHONE / WEBSITE are never read);
- `road_survival.sqlite.gz` (G3): per `(tozeret_cd, degem_cd, cohort year)` the cohort (active + finally cancelled),
  the age at final cancellation and the cancelled share by age; the model-year -> first-road-year map; the registry's
  model names (for the recall map);
- `recall_model_map.json`: the REVIEWED map (canonical make, DEGEM normalized) -> catalogue kinuy_mishari. The build only
  adds entries from exact normalized equality and lists the rest under `unresolved`; the Action's pull request is the
  review, and a reviewer may add or delete entries by hand. A model without an entry gets no `recalls` field;
- `manifest.json`: per dataset its file, sha256, rows, the provenance of every resource (resource_id, source_url,
  downloaded_at, source_last_modified, license as stated, row_count, file_size, sha256, schema_hash,
  ingestion_version), its stats and last attempt. Its sha256 is the facts record's `versions.gov_datasets` and part of
  the facts cache key;
- `build_status.json`: the last build's outcome per dataset (`{"dataset", "status": "failed", "reason",
  "previous_snapshot_preserved": true}` for a stopped one);
- `report.md`: the last build's report (the pull-request body).

The server verifies each file against the manifest's sha256 and decompresses it into the container's temporary
directory at first use; a missing file or a mismatch means no facts of that dataset. Do not edit the snapshots or the
manifest by hand: run the Action and merge its pull request.
