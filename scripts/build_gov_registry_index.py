"""Build data/gov_registry_index.json: the tyre sizes of the registered vehicles per government model (PR #44, P1).

    key    tozeret_cd | degem_cd | shnat_yitzur | ramat_gimur       (src/gov_registry.registry_key)
    value  {"front": {majority, share, n, top: [{size, n}, ...3]}, "rear": {...}}   (sizes as "235/50 R19")

Source: the data.gov.il CKAN datastore resource of the registry of private and commercial vehicles (licence-plate
level), read with paged `datastore_search` calls (only the six columns used, PAGE rows per call, a pause of
PAUSE_S between calls). FAIL-CLOSED: the first call (limit=1) verifies the resource and its column names; when
tozeret_cd / degem_cd / shnat_yitzur / ramat_gimur / zmig_kidmi / zmig_ahori are not all there, or any page fails,
the script exits non-zero and writes NOTHING (the shipped index stays as it is). Runtime never reads the network:
src/gov_registry.py only reads the file this writes, and only when it says `"complete": true`.

    python scripts/build_gov_registry_index.py                         # resource id: --resource-id / env / default
    python scripts/build_gov_registry_index.py --records-json page.json  # offline: a saved datastore_search result
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.gov_registry import normalize_tire, registry_key  # noqa: E402

INDEX_VERSION = "gov-registry-index-v1"
OUT_PATH = ROOT / "data" / "gov_registry_index.json"
CKAN_URL = "https://data.gov.il/api/3/action/datastore_search"
# the registry of private and commercial vehicles ("מספרי רישוי של כלי רכב פרטיים ומסחריים"); verified at run time
DEFAULT_RESOURCE_ID = "053cea08-09bc-40ec-8f7a-156f0677aff3"
COLUMNS = ("tozeret_cd", "degem_cd", "shnat_yitzur", "ramat_gimur", "zmig_kidmi", "zmig_ahori")
PAGE = 10000
PAUSE_S = 1.0
MIN_KEEP = 20          # an entry with fewer vehicles on both axles is dropped (the runtime would emit nothing for it)
TOP = 3


class Fail(SystemExit):
    """A fail-closed stop: the message is printed, the exit code is 2 and nothing is written."""

    def __init__(self, message: str):
        super().__init__(2)
        self.message = message


def ckan_get(params: dict, *, timeout: float = 120, retries: int = 3) -> dict:
    query = urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(f"{CKAN_URL}?{query}", headers={"User-Agent": "tripy-gov-registry-index/1"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            if not body.get("success"):
                raise Fail(f"CKAN refused the request: {json.dumps(body.get('error'))[:300]}")
            return body["result"]
        except Fail:
            raise
        except Exception as exc:  # noqa: BLE001 - retried, then a fail-closed stop
            last = exc
            time.sleep(PAUSE_S * (2 ** attempt))
    raise Fail(f"CKAN request failed after {retries} attempts: {type(last).__name__}: {last}")


def verify_columns(result: dict) -> list[str]:
    """The resource's column ids; Fail when one of COLUMNS is missing."""
    fields = [str(f.get("id")) for f in result.get("fields") or []]
    missing = [c for c in COLUMNS if c not in fields]
    if missing:
        raise Fail(f"the resource lacks the registry columns {missing} (it has {fields}); nothing written")
    return fields


def aggregate(records, stats: dict | None = None) -> dict:
    """{key: {"front": Counter, "rear": Counter}} of registry records (dicts with COLUMNS)."""
    stats = stats if stats is not None else {}
    out: dict[str, dict[str, Counter]] = {}
    for record in records:
        stats["rows"] = stats.get("rows", 0) + 1
        key = registry_key(record.get("tozeret_cd"), record.get("degem_cd"), record.get("shnat_yitzur"),
                           record.get("ramat_gimur"))
        if key is None:
            stats["unkeyed"] = stats.get("unkeyed", 0) + 1
            continue
        entry = out.setdefault(key, {"front": Counter(), "rear": Counter()})
        for axle, column in (("front", "zmig_kidmi"), ("rear", "zmig_ahori")):
            size = normalize_tire(record.get(column))
            if size:
                entry[axle][size] += 1
            elif record.get(column) not in (None, ""):
                stats["unparsed_sizes"] = stats.get("unparsed_sizes", 0) + 1
    return out


def summarize(counts: Counter) -> dict | None:
    n = sum(counts.values())
    if not n:
        return None
    (majority, top_n), *_ = counts.most_common(1)
    return {"majority": majority, "share": round(top_n / n, 4), "n": n,
            "top": [{"size": s, "n": c} for s, c in counts.most_common(TOP)]}


def build_index(counts: dict, *, source: str, resource_id: str | None, rows: int, complete: bool = True) -> dict:
    entries = {}
    for key in sorted(counts):
        front, rear = summarize(counts[key]["front"]), summarize(counts[key]["rear"])
        if max((front or {}).get("n", 0), (rear or {}).get("n", 0)) < MIN_KEEP:
            continue
        entries[key] = {k: v for k, v in (("front", front), ("rear", rear)) if v}
    return {"_about": "Government vehicle registry index (PR #44, P1; scripts/build_gov_registry_index.py, read by "
                      "src/gov_registry.py): per tozeret_cd | degem_cd | shnat_yitzur | ramat_gimur, the front / rear "
                      "tyre size distribution of the registered vehicles. Entries with fewer than "
                      f"{MIN_KEEP} vehicles are left out.",
            "version": INDEX_VERSION, "complete": complete,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": source,
            "resource_id": resource_id, "rows": rows, "entries": entries}


def dumps(index: dict) -> str:
    """Compact JSON with one entry per line (reviewable diffs when the index is regenerated)."""
    head = json.dumps({k: v for k, v in index.items() if k != "entries"}, ensure_ascii=False)
    lines = [json.dumps(k, ensure_ascii=False) + ":" + json.dumps(v, ensure_ascii=False, separators=(",", ":"))
             for k, v in index["entries"].items()]
    return head[:-1] + ', "entries": {\n' + ",\n".join(lines) + "\n}}\n"


def read_ckan(resource_id: str, *, page: int = PAGE, pause_s: float = PAUSE_S, get=ckan_get, sleep=time.sleep):
    """(counts, stats): column check first (limit=1), then every page; any failure is a Fail."""
    first = get({"resource_id": resource_id, "limit": 1})
    verify_columns(first)
    total = int(first.get("total") or 0)
    if total <= 0:
        raise Fail(f"resource {resource_id} reports no rows")
    stats: dict = {}
    counts: dict = {}
    offset = 0
    while offset < total:
        sleep(pause_s)
        result = get({"resource_id": resource_id, "limit": page, "offset": offset, "fields": ",".join(COLUMNS)})
        records = result.get("records") or []
        if not records:
            raise Fail(f"empty page at offset {offset} of {total}; nothing written")
        for key, entry in aggregate(records, stats).items():
            into = counts.setdefault(key, {"front": Counter(), "rear": Counter()})
            into["front"].update(entry["front"])
            into["rear"].update(entry["rear"])
        offset += len(records)
    return counts, {**stats, "total": total}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--resource-id", default=os.environ.get("GOV_REGISTRY_RESOURCE_ID") or DEFAULT_RESOURCE_ID)
    ap.add_argument("--records-json", help="offline: a saved datastore_search result (its fields and records)")
    ap.add_argument("--page", type=int, default=PAGE)
    ap.add_argument("--out", default=str(OUT_PATH))
    args = ap.parse_args(argv)
    try:
        if args.records_json:
            result = json.loads(Path(args.records_json).read_text("utf-8"))
            result = result.get("result", result)
            verify_columns(result)
            stats: dict = {}
            counts = aggregate(result.get("records") or [], stats)
            label = f"records-json: {Path(args.records_json).name}"
        else:
            counts, stats = read_ckan(args.resource_id, page=args.page)
            label = f"ckan: data.gov.il datastore {args.resource_id}"
    except Fail as stop:
        print(f"build_gov_registry_index: {stop.message}", file=sys.stderr)
        return 2
    index = build_index(counts, source=label, resource_id=None if args.records_json else args.resource_id,
                        rows=int(stats.get("rows", 0)))
    Path(args.out).write_text(dumps(index), "utf-8")
    print(f"{args.out}: {stats.get('rows', 0)} rows, {len(counts)} keys, {len(index['entries'])} kept; "
          f"unparsed sizes {stats.get('unparsed_sizes', 0)}, unkeyed rows {stats.get('unkeyed', 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
