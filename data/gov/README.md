# Government-dataset snapshots

Built by the `build-gov-datasets` GitHub Action (`.github/workflows/build-gov-datasets.yml`,
`scripts/build_gov_datasets.py`, `src/gov_data`), never on the server. Aggregates only (model / model-year level): no
per-vehicle record is stored or served, and raw downloads never enter git.

- `new_car_prices.sqlite.gz` (G1): per `(tozeret_cd, degem_cd, shnat_yitzur)` every distinct (mehir, kinuy_mishari,
  shem_yevuan, ...) with its row count; the facts API first selects the key's rows by the catalogue's kinuy_mishari
  (else a key with one single name, else withheld `price_model_ambiguous`: one degem_cd can carry several models), then
  serves one price as a value and several only as a range;
- `recall_notices.sqlite.gz` (G2): the recall notices with their canonical make, normalized DEGEM, production range and
  match status (TELEPHONE / WEBSITE are never read);
- `road_survival.sqlite.gz` (G3): per `(tozeret_cd, degem_cd, cohort year)` the cohort (active + finally cancelled),
  the age at final cancellation (whole years) and the cancelled share by age (none for a cohort under 200 vehicles,
  with under 95 % of its cancellations dated, or whose (tozeret_cd, shnat_yitzur) has unkeyed cancellations over 10 %
  of its own: `withheld` small_cohort / undated_cancellations / unkeyed_cancellations); the model-year ->
  first-road-year map; the registry's model names (for the recall map). The build fails the dataset
  (`date_unparsed`) when a resource parses under 95 % of a date column, and (`unkeyed_rows`) when a cancellation file
  has over 20 % of its rows still unkeyed after the degem_nm fallback (1 % for the active registry and new_car_prices);
- `recall_model_map.json`: the REVIEWED map (canonical make, DEGEM normalized) -> catalogue kinuy_mishari. The build only
  adds entries from exact normalized equality (a multi-model DEGEM such as `VITO,VIANO` only when every part is
  exactly a catalogue model of the make: one entry per model, origin `exact_normalized_equality_split`) and lists the
  rest under `unresolved`; the Action's pull request is the
  review, and a reviewer may add or delete entries by hand. A model without an entry gets no `recalls` field;
- `manifest.json`: per dataset its file, sha256, rows, the provenance of every resource (resource_id, source_url,
  downloaded_at, source_last_modified, license as stated, row_count, file_size, sha256, schema_hash,
  ingestion_version, access_method), its stats and last attempt. `access_method` is `file_download` (the resource
  file) or `datastore_api` (the file download failed with an HTTP error or an HTML body and the resource is in the CKAN
  datastore: `source_url` is the `datastore_search` endpoint, `sha256` is over the canonical JSON lines of the projected
  rows in `_id` order, `file_size` is null, `schema_hash` is of the datastore fields, `file_attempt` says why the file
  failed: HTTP status, host, `redirect_host` when it redirected off *.gov.il — a redirect that is never followed). Its sha256 is the facts record's `versions.gov_datasets` and part of
  the facts cache key;
- `build_status.json`: the last build's outcome per dataset (`{"dataset", "status": "failed", "reason",
  "previous_snapshot_preserved": true}` for a stopped one), with per resource its status, access method, the HTTP
  status of the file attempt and the rows read;
- `report.md`: the last build's report (the pull-request body).

The server verifies each file against the manifest's sha256 and decompresses it into the container's temporary
directory at first use; a missing file or a mismatch means no facts of that dataset. Do not edit the snapshots or the
manifest by hand: run the Action and merge its pull request.
