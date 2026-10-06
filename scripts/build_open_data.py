"""Build the open-data snapshots now (the same build as the Data page's "Rebuild now" and the monthly job).

Each build reads the dataset's live header first and stops with a report when a mapped column of
data/open_datasets.json is absent; a stopped or failed build keeps the previous snapshot. Only datasets that
data/source_policy.json lists as `allowed` with bulk_store are downloaded.

    python scripts/build_open_data.py [dataset ...] [--dir <TRIPY_DATA_DIR>/derived/open]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import source_authority  # noqa: E402
from src.open_data import datasets as ds  # noqa: E402
from src.open_data.job import OpenDataJob  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    from src.storage.paths import resolve_paths

    paths = resolve_paths()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("datasets", nargs="*", help="default: every dataset with a snapshot")
    parser.add_argument("--dir", default=str(paths.data_dir / "derived" / "open"))
    args = parser.parse_args(argv)
    source_authority.set_policy_overlay_dir(paths.data_dir / "derived")
    job = OpenDataJob(Path(args.dir))
    names = args.datasets or [n for n, c in ds.datasets().items() if not c.get("identity_only")]
    results = {name: job.build(name, reason="cli") for name in names}
    print(json.dumps(results, ensure_ascii=False, indent=1, default=str))
    return 0 if all(r.get("status") == "built" for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
