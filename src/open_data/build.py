"""D-1: the snapshot builders. One dataset at a time, into <TRIPY_DATA_DIR>/derived/open/<dataset>.sqlite.

Every build reads the dataset's LIVE header first and resolves the column map of data/open_datasets.json against it:
each canonical key lists the exact source spellings it accepts; a key whose spellings are all absent (or two of which
are present) stops the build with a report (`schema_mismatch`: the missing keys and the live header). Nothing is
guessed and a failed build keeps the previous snapshot. Downloads go through the production source policy (a dataset
whose domain is not `allowed` is never downloaded) with the fetch tools' user agent.

    eea_co2_cars        DISCODATA SQL: `SELECT TOP 1 *` for the live schema, then one grouped query per year
                        (distinct configurations + sum(r) as registrations), only makes of identity_vocabulary
                        `open_data_make_aliases`
    ademe_car_labelling the data.gouv.fr dataset page's CSV resource (`api_url` -> the first CSV resource)
    epa_fueleconomy     vehicles.csv (zip)
    nrcan_fuel_ratings  the CSV resources of the open.canada.ca package (`ckan_package`)
    tc_cvs              the CSV files listed in `download_urls` (none listed: the build stops and reports)
"""

from __future__ import annotations

import csv
import io
import json
import time
import zipfile
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote

from . import datasets as ds

Fetcher = Callable[[str], bytes]
MAX_DOWNLOAD_BYTES = 400 * 1024 * 1024
EEA_PAGE = 50_000


class BuildStopped(RuntimeError):
    """The build stopped with a report (schema mismatch, no download URL, a policy-blocked source)."""

    def __init__(self, reason: str, **report: Any):
        super().__init__(reason)
        self.reason, self.report = reason, report


def default_fetcher(session=None, timeout_s: float = 120.0) -> Fetcher:
    import requests

    from ..source_authority import fetch_allowed
    from ..tools.fetch import USER_AGENT

    http = session or requests.Session()

    def fetch(url: str) -> bytes:
        if not fetch_allowed(url):
            raise BuildStopped("policy_blocked", url=url)
        resp = http.get(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}, timeout=(15, timeout_s), stream=True)
        resp.raise_for_status()
        chunks, size = [], 0
        for chunk in resp.iter_content(1 << 16):
            size += len(chunk)
            if size > MAX_DOWNLOAD_BYTES:
                raise BuildStopped("download_too_large", url=url, bytes=size)
            chunks.append(chunk)
        return b"".join(chunks)
    return fetch


def resolve_columns(header: list[str], columns: dict[str, dict]) -> dict[str, str]:
    """{canonical key: the live source column} or BuildStopped(schema_mismatch) naming every key it cannot resolve."""
    live = [h for h in header]
    out, missing, ambiguous = {}, [], []
    for key, spec in columns.items():
        present = [s for s in spec.get("source") or [] if s in live]
        if len(present) == 1:
            out[key] = present[0]
        elif not present:
            missing.append({"key": key, "expected": spec.get("source")})
        else:
            ambiguous.append({"key": key, "present": present})
    if missing or ambiguous:
        raise BuildStopped("schema_mismatch", missing=missing, ambiguous=ambiguous, live_header=live[:200])
    return out


def _decode(body: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return body.decode(enc)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="replace")


def _csv_tables(body: bytes) -> list[tuple[str, list[dict]]]:
    """(name, rows) of a CSV body or of every CSV inside a zip."""
    if body[:2] == b"PK":
        out = []
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            for name in archive.namelist():
                if name.lower().endswith(".csv"):
                    out += _csv_tables(archive.read(name))
        return out
    text = _decode(body)
    dialect = csv.Sniffer().sniff(text[:5000], delimiters=",;\t") if text.strip() else csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    return [("csv", list(reader))]


def _canonical(rows: list[dict], mapping: dict[str, str], dataset: str, keep_makes: set[str] | None,
               id_prefix: str = "") -> list[dict]:
    out = []
    for n, row in enumerate(rows):
        item = {key: (row.get(column) if row.get(column) not in ("",) else None) for key, column in mapping.items()}
        make = str(item.get("make") or "").strip().upper()
        if keep_makes is not None and make not in keep_makes:
            continue
        item["row_id"] = str(item.pop("row_id", None) or f"{id_prefix}{n + 1}")
        out.append(item)
    return out


def known_makes() -> set[str]:
    from ..document_binding import vocabulary

    aliases = vocabulary().get("open_data_make_aliases") or {}
    return {str(m).upper() for k, v in aliases.items() if not k.startswith("_") and isinstance(v, list) for m in v}


def build_csv_dataset(dataset: str, urls: list[str], fetch: Fetcher, *, keep_makes: set[str] | None = None) -> dict:
    cfg = ds.datasets()[dataset]
    columns = cfg.get("columns") or {}
    rows: list[dict] = []
    headers: list[list[str]] = []
    for u, url in enumerate(urls):
        for t, (_, table) in enumerate(_csv_tables(fetch(url))):
            header = list(table[0].keys()) if table else []
            if not header:
                continue
            mapping = resolve_columns(header, columns)
            headers.append(header)
            rows += _canonical(table, mapping, dataset, keep_makes, id_prefix=f"{dataset}-{u}-{t}-")
    if not headers:
        raise BuildStopped("no_rows", urls=urls)
    return {"rows": rows, "schema": headers[0], "urls": urls}


def build_eea(fetch: Fetcher, *, years: list[int] | None = None) -> dict:
    """DISCODATA: the live schema (`SELECT TOP 1 *`), then per year the distinct configurations of the known makes."""
    cfg = ds.datasets()["eea_co2_cars"]
    base, table = cfg["source_url"], cfg["table"]

    def sql(query: str) -> list[dict]:
        body = fetch(f"{base}?query={quote(query)}&p=1&nrOfHits={EEA_PAGE}")
        data = json.loads(_decode(body))
        return data.get("results") or data.get("Results") or []

    sample = sql(f"SELECT TOP 1 * FROM {table}")
    header = list(sample[0].keys()) if sample else []
    mapping = resolve_columns(header, cfg["columns"])
    registrations = resolve_columns(header, {"registrations": cfg["registrations_column"]})["registrations"]
    makes = sorted(known_makes())
    if not makes:
        raise BuildStopped("no_makes", note="identity_vocabulary open_data_make_aliases is empty")
    year_col = mapping["year"]
    this_year = datetime.now(timezone.utc).year
    years = years or list(range(int(cfg.get("years_from") or 2017), this_year + 1))
    group = ", ".join(f"[{c}]" for c in mapping.values())
    in_makes = ", ".join("'" + m.replace("'", "''") + "'" for m in makes)
    rows: list[dict] = []
    for year in years:
        chunk = sql(f"SELECT {group}, SUM([{registrations}]) AS registrations FROM {table} "
                    f"WHERE [{year_col}] = {int(year)} AND UPPER([{mapping['make']}]) IN ({in_makes}) GROUP BY {group}")
        for n, raw in enumerate(chunk):
            item = {key: raw.get(column) for key, column in mapping.items()}
            item["registrations"] = raw.get("registrations")
            item["row_id"] = f"eea-{year}-{n + 1}"
            rows.append(item)
    return {"rows": rows, "schema": header, "urls": [base], "years": years}


def build_dataset(dataset: str, fetch: Fetcher | None = None) -> dict:
    """Build one snapshot. {status: built | stopped | failed, rows, built_at, report, ...}; never raises."""
    from ..source_authority import dataset_policy

    started = time.monotonic()
    cfg = ds.datasets().get(dataset)
    if not cfg:
        return {"status": "stopped", "reason": "unknown_dataset"}
    if cfg.get("identity_only"):
        return {"status": "skipped", "reason": "identity_only (on-demand decode, no snapshot)"}
    policy = dataset_policy(cfg.get("policy_id") or dataset)
    if policy["policy"] != "allowed" or not policy.get("bulk_store"):
        return {"status": "stopped", "reason": "policy", "policy": policy["policy"]}
    fetch = fetch or default_fetcher()
    try:
        if dataset == "eea_co2_cars":
            built = build_eea(fetch)
        elif dataset == "ademe_car_labelling":
            page = json.loads(_decode(fetch(cfg["api_url"])))
            urls = [r.get("url") for r in page.get("resources") or [] if str(r.get("format") or "").lower() == "csv"]
            if not urls:
                raise BuildStopped("no_csv_resource", api_url=cfg["api_url"])
            built = build_csv_dataset(dataset, urls[:1], fetch)
        elif dataset == "nrcan_fuel_ratings":
            page = json.loads(_decode(fetch(cfg["ckan_api"])))
            resources = (page.get("result") or {}).get("resources") or []
            urls = [r.get("url") for r in resources if str(r.get("format") or "").upper() == "CSV"
                    and "en" in (r.get("language") or ["en"])]
            if not urls:
                raise BuildStopped("no_csv_resource", ckan_api=cfg["ckan_api"])
            built = build_csv_dataset(dataset, urls, fetch)
        else:
            urls = cfg.get("download_urls") or []
            if not urls:
                raise BuildStopped("no_download_url", note=f"data/open_datasets.json {dataset}.download_urls is empty")
            built = build_csv_dataset(dataset, urls, fetch)
        meta = {"built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "dataset": dataset,
                "source_url": cfg.get("source_url"), "urls": built.get("urls"), "schema": built.get("schema"),
                "licence": policy.get("licence"), "attribution": policy.get("attribution"),
                "years": built.get("years")}
        ds.write_snapshot(dataset, built["rows"], meta)
        return {"status": "built", "rows": len(built["rows"]), "built_at": meta["built_at"],
                "duration_s": round(time.monotonic() - started, 1)}
    except BuildStopped as stop:
        return {"status": "stopped", "reason": stop.reason, "report": stop.report}
    except Exception as exc:  # noqa: BLE001 - a failed build keeps the previous snapshot
        return {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
