"""Build data/gov_registry_index.json: the tyre sizes of the registered vehicles per government model (PR #44, P1).

    key    tozeret_cd | degem_cd | shnat_yitzur | ramat_gimur       (src/gov_registry.registry_key; the trim normalized
                                                                     by src/gov_registry.normalize_trim, PR #45)
    value  {"front": {majority, share, n, top: [{size, n}, ...3]}, "rear": {...}, "merged_trims"?: {raw: n}}
           (sizes as "235/50 R19", src/gov_registry.normalize_tire)
    stats  build diagnostics (PR #45): keys kept / dropped for n < MIN_KEEP / dropped by unparsed tyre strings, per
           manufacturer kept / dropped keys, merged-trim keys, the TOP_UNPARSED raw unparsed strings with counts

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
TOP_UNPARSED = 30      # the most frequent raw tyre strings the parser rejected, stored in `stats` and printed


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


def _new_entry() -> dict:
    return {"front": Counter(), "rear": Counter(), "unparsed": Counter(), "trims": Counter()}


def aggregate(records, stats: dict | None = None, into: dict | None = None) -> dict:
    """{key: {"front": Counter, "rear": Counter, "unparsed": Counter (per axle), "trims": Counter (raw ramat_gimur)}}
    of registry records (dicts with COLUMNS), added to `into` when given. `stats` gathers rows, unkeyed rows, unparsed
    tyre cells / rows and the raw unparsed strings (`unparsed_raw`, a Counter)."""
    stats = stats if stats is not None else {}
    out: dict[str, dict] = into if into is not None else {}
    raw_unparsed = stats.setdefault("unparsed_raw", Counter())
    for record in records:
        stats["rows"] = stats.get("rows", 0) + 1
        key = registry_key(record.get("tozeret_cd"), record.get("degem_cd"), record.get("shnat_yitzur"),
                           record.get("ramat_gimur"))
        if key is None:
            stats["unkeyed"] = stats.get("unkeyed", 0) + 1
            continue
        entry = out.get(key)
        if entry is None:
            entry = out[key] = _new_entry()
        entry["trims"][" ".join(str(record.get("ramat_gimur") or "").upper().split())] += 1
        row_unparsed = False
        for axle, column in (("front", "zmig_kidmi"), ("rear", "zmig_ahori")):
            raw = record.get(column)
            size = normalize_tire(raw)
            if size:
                entry[axle][size] += 1
            elif raw not in (None, "") and str(raw).strip():
                stats["unparsed_sizes"] = stats.get("unparsed_sizes", 0) + 1
                entry["unparsed"][axle] += 1
                raw_unparsed[" ".join(str(raw).split())[:60]] += 1
                row_unparsed = True
        if row_unparsed:
            stats["unparsed_rows"] = stats.get("unparsed_rows", 0) + 1
    return out


def summarize(counts: Counter) -> dict | None:
    n = sum(counts.values())
    if not n:
        return None
    (majority, top_n), *_ = counts.most_common(1)
    return {"majority": majority, "share": round(top_n / n, 4), "n": n,
            "top": [{"size": s, "n": c} for s, c in counts.most_common(TOP)]}


def build_stats(counts: dict, kept: set, stats: dict | None = None) -> dict:
    """The build diagnostics stored in the index (`stats`, PR #45 R7): keys kept / dropped for n < MIN_KEEP (even with
    the unparsed rows counted) / dropped because unparsed tyre strings kept them below MIN_KEEP; per manufacturer
    (tozeret_cd) kept / dropped keys; keys whose raw trims merged; the TOP_UNPARSED raw unparsed strings."""
    stats = stats or {}
    out = {"rows": int(stats.get("rows", 0)), "keys": len(counts), "kept": len(kept), "dropped_small": 0,
           "dropped_unparsed": 0, "merged_keys": 0, "unkeyed_rows": int(stats.get("unkeyed", 0)),
           "unparsed_sizes": int(stats.get("unparsed_sizes", 0)), "unparsed_rows": int(stats.get("unparsed_rows", 0)),
           "unparsed_top": [{"raw": raw, "n": n}
                            for raw, n in (stats.get("unparsed_raw") or Counter()).most_common(TOP_UNPARSED)],
           "manufacturers": {}}
    for key, entry in counts.items():
        maker = out["manufacturers"].setdefault(key.split("|")[0], {"kept": 0, "dropped": 0})
        if len(entry.get("trims") or {}) > 1:
            out["merged_keys"] += 1
        if key in kept:
            maker["kept"] += 1
            continue
        maker["dropped"] += 1
        unparsed = entry.get("unparsed") or Counter()
        with_unparsed = max(sum(entry["front"].values()) + unparsed["front"],
                            sum(entry["rear"].values()) + unparsed["rear"])
        out["dropped_unparsed" if with_unparsed >= MIN_KEEP else "dropped_small"] += 1
    out["manufacturers"] = dict(sorted(out["manufacturers"].items(), key=lambda kv: (len(kv[0]), kv[0])))
    return out


def build_index(counts: dict, *, source: str, resource_id: str | None, rows: int, complete: bool = True,
                stats: dict | None = None) -> dict:
    entries = {}
    for key in sorted(counts):
        front, rear = summarize(counts[key]["front"]), summarize(counts[key]["rear"])
        if max((front or {}).get("n", 0), (rear or {}).get("n", 0)) < MIN_KEEP:
            continue
        entries[key] = {k: v for k, v in (("front", front), ("rear", rear)) if v}
        trims = counts[key].get("trims") or {}
        if len(trims) > 1:
            # two raw trims normalize to this key ("S-LINE" / "S LINE"): their counts are merged, recorded here
            entries[key]["merged_trims"] = dict(sorted(trims.items()))
    index_stats = build_stats(counts, set(entries), {"rows": rows, **(stats or {})})
    return {"_about": "Government vehicle registry index (PR #44, P1; scripts/build_gov_registry_index.py, read by "
                      "src/gov_registry.py): per tozeret_cd | degem_cd | shnat_yitzur | ramat_gimur (the trim "
                      "normalized: upper case, - _ . as a space, collapsed whitespace), the front / rear tyre size "
                      f"distribution of the registered vehicles. Entries with fewer than {MIN_KEEP} vehicles are left "
                      "out. `merged_trims`: the raw trims merged into one key; `stats`: the build diagnostics (PR #45).",
            "version": INDEX_VERSION, "complete": complete,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": source,
            "resource_id": resource_id, "rows": rows, "stats": index_stats, "entries": entries}


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
        aggregate(records, stats, into=counts)
        offset += len(records)
    return counts, {**stats, "total": total}


def report(index: dict) -> str:
    """The build diagnostics as log lines (printed at the end of the build)."""
    st = index.get("stats") or {}
    lines = [f"keys {st.get('keys', 0)}, kept {st.get('kept', 0)} (n >= {MIN_KEEP}); dropped n < {MIN_KEEP}: "
             f"{st.get('dropped_small', 0)}, dropped by unparsed sizes: {st.get('dropped_unparsed', 0)}; merged trims: "
             f"{st.get('merged_keys', 0)} keys",
             f"unparsed tyre cells {st.get('unparsed_sizes', 0)} in {st.get('unparsed_rows', 0)} rows; top "
             f"{len(st.get('unparsed_top') or [])} raw strings:"]
    lines += [f"  {row['n']:>9}  {row['raw']!r}" for row in st.get("unparsed_top") or []]
    makers = sorted((st.get("manufacturers") or {}).items(), key=lambda kv: -(kv[1]["kept"] + kv[1]["dropped"]))
    lines.append("per manufacturer (tozeret_cd: kept / dropped keys), largest 30:")
    lines += [f"  {code:>6}: {m['kept']} / {m['dropped']}" for code, m in makers[:30]]
    return "\n".join(lines)


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
            stats = {}
            counts = aggregate(result.get("records") or [], stats)
            label = f"records-json: {Path(args.records_json).name}"
        else:
            counts, stats = read_ckan(args.resource_id, page=args.page)
            label = f"ckan: data.gov.il datastore {args.resource_id}"
    except Fail as stop:
        print(f"build_gov_registry_index: {stop.message}", file=sys.stderr)
        return 2
    index = build_index(counts, source=label, resource_id=None if args.records_json else args.resource_id,
                        rows=int(stats.get("rows", 0)), stats=stats)
    Path(args.out).write_text(dumps(index), "utf-8")
    print(f"{args.out}: {stats.get('rows', 0)} rows, {len(counts)} keys, {len(index['entries'])} kept; "
          f"unparsed sizes {stats.get('unparsed_sizes', 0)}, unkeyed rows {stats.get('unkeyed', 0)}")
    print(report(index))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
