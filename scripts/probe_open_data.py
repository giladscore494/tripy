"""Step 1 of the build-open-data GitHub Action: the live-schema probe (src/open_data/probe.py).

Writes data/open/probe.json (committed with the snapshots) and the Markdown view, appended to $GITHUB_STEP_SUMMARY when
it is set. Exits 0 when a dataset's probe fails: that failure stops only that dataset's build (the build step reads the
probe). Needs network access to the sources; no secret.

    python scripts/probe_open_data.py [--datasets all|eea_co2_cars,...] [--out data/open/probe.json]
                                      [--markdown open-data-probe.md]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.open_data import datasets as ds  # noqa: E402
from src.open_data.build import default_fetcher  # noqa: E402
from src.open_data.probe import markdown, probe  # noqa: E402


def selected(value: str | None) -> list[str]:
    names = [n for n, c in ds.datasets().items() if not c.get("identity_only")]
    wanted = [v.strip() for v in str(value or "all").split(",") if v.strip()]
    if not wanted or "all" in wanted:
        return names
    unknown = [w for w in wanted if w not in names]
    if unknown:
        raise SystemExit(f"unknown dataset(s): {', '.join(unknown)} (known: {', '.join(names)})")
    return wanted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--datasets", default="all")
    parser.add_argument("--out", default=str(ROOT / "data" / "open" / "probe.json"))
    parser.add_argument("--markdown", default="")
    args = parser.parse_args(argv)
    report = probe(selected(args.datasets), default_fetcher())
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str) + "\n", "utf-8")
    text = markdown(report)
    if args.markdown:
        Path(args.markdown).write_text(text, "utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
