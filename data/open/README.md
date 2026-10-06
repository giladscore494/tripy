# Open-data snapshots

Built by the `build-open-data` GitHub Action (`.github/workflows/build-open-data.yml`), never on the server:

- `<dataset>.sqlite.gz`: the compacted snapshot (`src/open_data/build.py`, `compaction` in `data/open_datasets.json`);
- `manifest.json`: per dataset its file, sha256, rows, years / files, absent columns, units, build date and the Action
  run URL;
- `probe.json`: the Action's last live-schema probe.

The server verifies each file against the manifest's sha256 and decompresses it into the container's temporary
directory at first use (`src/open_data/datasets.py`); a missing file or a mismatch is `no_snapshot`. Do not edit these
files by hand: run the Action and merge its pull request.
