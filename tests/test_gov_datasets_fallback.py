"""Government datasets: diagnosable download errors (HTTP status, host, body start), the download request (headers,
one retry, redirects only to *.gov.il), the CKAN datastore fallback with its fail-safes, and the workflow's build-status
step. No network: a fake CKAN, and the real Http over a fake transport."""

from __future__ import annotations

import csv
import email.message
import gzip
import hashlib
import io
import urllib.error
import urllib.parse
from pathlib import Path

import pytest
import yaml

from test_gov_datasets import (ACTIVE, CANCELLED, CHASSIS, ENGINE, PLATE, PRICES, RECALLS, ROOT, FakeCkan, B,
                               bodies, build, gov)  # noqa: F401 - gov is a fixture
from src.gov_data import ckan
from src.gov_data import snapshot as SN
from src.gov_data.ingest import canonical_line

NEVER = {"mispar_rechev", "misgeret", "mispar_manoa"}
SURVIVAL = (*CANCELLED, ACTIVE)


def http_error(url: str, code: int, body: bytes = b"", headers: dict | None = None) -> urllib.error.HTTPError:
    message = email.message.Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError(url, code, "status", message, io.BytesIO(body))


class Response(io.BytesIO):
    def __init__(self, data: bytes, status: int = 200):
        super().__init__(data)
        self.status = status


class Transport:
    """A fake urllib transport: `script` maps a URL to the answers given in turn (an int status with a body, a
    redirect (status, location), or bytes for a 200)."""

    def __init__(self, script: dict[str, list]):
        self.script = {k: list(v) for k, v in script.items()}
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, request, timeout):
        url = request.full_url
        self.requests.append((url, dict(request.header_items())))
        answer = self.script[url].pop(0) if len(self.script[url]) > 1 else self.script[url][0]
        if isinstance(answer, bytes):
            return Response(answer)
        status, extra = answer
        if status in ckan.REDIRECT_CODES:
            raise http_error(url, status, b"", {"Location": extra})
        raise http_error(url, status, extra)


def records_of(body: bytes) -> tuple[list[str], list[dict]]:
    """A fixture CSV body as datastore fields and records (every column, the identifiers included: the fake
    datastore projects to the requested fields as CKAN does)."""
    reader = csv.reader(io.StringIO(body.decode("utf-8-sig")))
    header = [h.strip() for h in next(reader)]
    out = []
    for n, row in enumerate(reader, start=1):
        rec = {"_id": n}
        for name, value in zip(header, row):
            rec[name] = int(value) if value.isdigit() else (value or None)
        out.append(rec)
    return header, out


class FallbackCkan(FakeCkan):
    """FakeCkan + file answers through the real Http (fake transport) + a datastore."""

    def __init__(self, bodies_: dict[str, bytes], file_answers: dict[str, list] | None = None,
                 datastore: dict[str, dict] | None = None, **kwargs):
        super().__init__(bodies_, **kwargs)
        self.file_answers = file_answers or {}
        self.datastore = datastore or {}
        self.slept: list[float] = []
        self.transport = Transport({})
        self.datastore_requests: list[dict] = []

    def get_json(self, url: str, retries: int | None = None) -> dict:
        parsed = urllib.parse.urlparse(url)
        action, params = parsed.path.rsplit("/", 1)[-1], dict(urllib.parse.parse_qsl(parsed.query))
        if action == "resource_show":
            body = super().get_json(url)
            if body.get("success"):
                body["result"]["datastore_active"] = params["id"] in self.datastore
            return body
        if action == "datastore_search" and params.get("resource_id") in self.datastore and \
                ("sort" in params or params.get("limit") == "0"):
            self.calls.append(url)
            self.datastore_requests.append(params)
            store = self.datastore[params["resource_id"]]
            if params.get("limit") == "0":
                return {"success": True, "result": {"fields": [{"id": "_id", "type": "int"}] +
                                                    [{"id": f, "type": "text"} for f in store["fields"]],
                                                    "records": [], "total": store.get("total", len(store["records"]))}}
            wanted = params["fields"].split(",")
            unknown = [f for f in wanted if f not in store["fields"]]
            if unknown:
                return {"success": False, "error": {"fields": [f"field {unknown[0]!r} not found"]}}
            offset, limit = int(params.get("offset", 0)), int(params["limit"])
            page = sorted(store["records"], key=lambda r: r["_id"])[offset:offset + limit]
            return {"success": True, "result": {"records": [{f: r.get(f) for f in wanted} for r in page],
                                                "fields": [{"id": f} for f in wanted],
                                                "total": store.get("total", len(store["records"]))}}
        return super().get_json(url)

    def open(self, url: str):
        rid = url.split("/")[3]
        if rid not in self.file_answers:
            return super().open(url)
        self.opened.append(url)
        self.transport.script[url] = self.file_answers[rid]
        return ckan.Http(transport=self.transport, sleep=self.slept.append).open(url)


def store_of(body: bytes, **changes) -> dict:
    fields, records = records_of(body)
    return {"fields": fields, "records": records, **changes}


def file_url(rid: str) -> str:
    return f"https://files.test/{rid}/served-{rid[:4]}.csv"


FORBIDDEN = (403, b"<html>\x00Forbidden\x1b[0m\r\n by the WAF</html>")


# --- D1 / D2: the download request ------------------------------------------------------------------------------------

def test_the_download_sends_the_data_gov_headers_and_names_status_host_and_body():
    url = "https://data.gov.il/dataset/x/resource/y/download/file.csv?token=abc"
    transport = Transport({url: [(403, b"\x00Access denied\x07 " + b"y" * 400)]})
    with pytest.raises(ckan.DownloadError) as caught:
        ckan.Http(transport=transport, sleep=lambda s: None).open(url)
    message = str(caught.value)
    assert message.startswith("HTTP 403 from data.gov.il: ") and caught.value.kind == "http"
    assert caught.value.status == 403 and caught.value.host == "data.gov.il"
    assert "token" not in message and "abc" not in message and "\x00" not in message and "\x07" not in message
    assert "Access denied" in message and len(ckan.body_snippet(b"\x00Access denied\x07 " + b"y" * 400)) == 200
    headers = {k.lower(): v for k, v in transport.requests[0][1].items()}
    assert headers["user-agent"] == "datagov-external-client" and headers["accept"] == "text/csv,*/*"


@pytest.mark.parametrize("first", [429, 500, 503])
def test_the_download_retries_once_after_ten_seconds_on_429_and_5xx(first):
    url = "https://data.gov.il/f.csv"
    slept: list[float] = []
    ok = ckan.Http(transport=Transport({url: [(first, b"busy"), b"a,b\n1,2\n"]}), sleep=slept.append).open(url)
    assert ok.read() == b"a,b\n1,2\n" and slept == [10.0]
    slept.clear()
    with pytest.raises(ckan.DownloadError, match=f"HTTP {first} from data.gov.il"):
        ckan.Http(transport=Transport({url: [(first, b"busy")]}), sleep=slept.append).open(url)
    assert slept == [10.0]                                                        # once, never twice
    with pytest.raises(ckan.DownloadError, match="HTTP 404"):
        ckan.Http(transport=Transport({url: [(404, b"")]}), sleep=slept.append).open(url)
    assert slept == [10.0]                                                        # a 404 is not retried


def test_redirects_are_followed_only_to_gov_il_hosts():
    start = "https://data.gov.il/f.csv"
    transport = Transport({start: [(302, "https://files.data.gov.il/x/f.csv?sig=1")],
                           "https://files.data.gov.il/x/f.csv?sig=1": [b"a,b\n"]})
    assert ckan.Http(transport=transport, sleep=lambda s: None).open(start).read() == b"a,b\n"
    transport = Transport({start: [(301, "https://evil.example.com/f.csv?k=secret")]})
    with pytest.raises(ckan.DownloadError) as caught:
        ckan.Http(transport=transport, sleep=lambda s: None).open(start)
    assert caught.value.kind == "redirect" and "evil.example.com" in str(caught.value)
    assert "secret" not in str(caught.value) and len(transport.requests) == 1
    assert not ckan.gov_host("gov.il.evil.com") and not ckan.gov_host("notgov.il") and ckan.gov_host("data.gov.il")


def test_api_json_is_retried_three_times_on_429_and_5xx_only():
    url = "https://data.gov.il/api/3/action/datastore_search?resource_id=x"
    slept: list[float] = []
    transport = Transport({url: [(429, b"slow down")] * 3 + [b'{"success": true}']})
    assert ckan.Http(transport=transport, sleep=slept.append).get_json(url, retries=3) == {"success": True}
    assert slept == [1.0, 2.0, 4.0]
    with pytest.raises(ckan.CkanError, match="HTTP 503 from data.gov.il"):
        ckan.Http(transport=Transport({url: [(503, b"")]}), sleep=lambda s: None).get_json(url, retries=3)
    transport = Transport({url: [(409, b'{"error": "bad field"}')]})
    with pytest.raises(ckan.CkanError, match="HTTP 409 from data.gov.il: .*bad field"):
        ckan.Http(transport=transport, sleep=lambda s: None).get_json(url, retries=3)
    assert len(transport.requests) == 1


def test_datastore_pages_page_by_offset_and_stop_at_a_short_page():
    class Pages:
        def __init__(self, n):
            self.n, self.params = n, []

        def get_json(self, url, retries=None):
            params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
            self.params.append(params)
            offset, limit = int(params["offset"]), int(params["limit"])
            rows = [{"a": i} for i in range(offset, min(self.n, offset + limit))]
            return {"success": True, "result": {"records": rows, "total": self.n}}

    rate = ckan.RateLimit(clock=lambda: 0.0, sleep=lambda s: None)
    for n, offsets in ((5, ["0", "2", "4"]), (4, ["0", "2", "4"]), (1, ["0"])):
        http = Pages(n)
        pages = list(ckan.datastore_pages(http, "https://b", "r", ["a"], rate, limit=2))
        assert [p["offset"] for p in http.params] == offsets and sum(len(r) for _, r, _ in pages) == n
        assert all(p["sort"] == "_id" and p["fields"] == "a" for p in http.params)


def test_the_rate_limit_is_four_requests_per_second():
    now, slept = [0.0], []

    def sleep(s):
        slept.append(round(s, 6))
        now[0] += s
    rate = ckan.RateLimit(4.0, clock=lambda: now[0], sleep=sleep)
    for _ in range(5):
        rate.wait()
    assert slept == [0.25] * 4


# --- D3: the datastore fallback --------------------------------------------------------------------------------------

def test_a_403_on_the_file_falls_back_to_the_datastore(gov, tmp_path):
    out, work = gov
    build(tmp_path / "by-file", tmp_path / "w1", FakeCkan(bodies()), names=["new_car_prices"])
    http = FallbackCkan(bodies(), file_answers={PRICES: [FORBIDDEN]}, datastore={PRICES: store_of(bodies()[PRICES])})
    outcome = build(out, work, http, names=["new_car_prices"])
    status = outcome["status"][0]
    assert status["status"] == "built"
    res = status["resources"][0]
    assert res["access_method"] == "datastore_api" and res["file_http_status"] == 403 and res["rows"] == 7
    assert "HTTP 403" in res["file_error"] and "files.test" in res["file_error"] and "\x00" not in res["file_error"]
    entry = outcome["manifest"]["datasets"]["new_car_prices"]["resources"][0]
    assert entry["access_method"] == "datastore_api" and entry["file_size"] is None and entry["row_count"] == 7
    assert entry["source_url"] == f"https://data.gov.il/api/3/action/datastore_search?resource_id={PRICES}"
    assert entry["file_attempt"]["http_status"] == 403 and entry["file_attempt"]["host"] == "files.test"
    projected = []
    for rec in http.datastore[PRICES]["records"]:
        projected.append({k: (str(rec[k]) if rec[k] is not None else None) for k in
                          ("tozeret_cd", "degem_cd", "shnat_yitzur", "mehir", "kinuy_mishari", "semel_yevuan",
                           "shem_yevuan", "sug_degem", "tozeret_nm", "degem_nm")})
    assert entry["sha256"] == hashlib.sha256(b"".join(canonical_line(r) for r in projected)).hexdigest()
    assert entry["schema"] == http.datastore[PRICES]["fields"]
    assert all("extra_column" not in r.get("fields", "") for r in http.datastore_requests)
    assert http.slept == []                                                       # a 403 is not retried
    # the same rows give the same snapshot, whichever way they were read
    assert (out / "new_car_prices.sqlite.gz").read_bytes() == \
        (tmp_path / "by-file" / "new_car_prices.sqlite.gz").read_bytes()
    assert "via datastore_api" in B.report(outcome)
    assert not list((work / "raw").glob("*")) if (work / "raw").exists() else True


def test_a_403_without_the_datastore_fails_with_status_and_host(gov):
    out, work = gov
    http = FallbackCkan(bodies(), file_answers={PRICES: [FORBIDDEN]})
    status = build(out, work, http, names=["new_car_prices"])["status"][0]
    assert status["status"] == "failed" and status["reason"] == "resource_unavailable"
    assert "HTTP 403 from files.test" in status["detail"] and "Forbidden" in status["detail"]
    assert "\x00" not in status["detail"] and "\x1b" not in status["detail"]
    assert "datastore_active is not true" in status["detail"]


def test_an_html_body_falls_back_to_the_datastore(gov):
    out, work = gov
    http = FallbackCkan(bodies(**{PRICES: b"<!DOCTYPE html><html>captcha</html>"}),
                        datastore={PRICES: store_of(bodies()[PRICES])})
    status = build(out, work, http, names=["new_car_prices"])["status"][0]
    assert status["status"] == "built" and status["resources"][0]["access_method"] == "datastore_api"
    assert "captcha" in status["resources"][0]["file_error"]


def test_the_survival_datastore_request_never_asks_for_the_identifiers(gov, tmp_path):
    out, work = gov
    build(tmp_path / "by-file", tmp_path / "w1", FakeCkan(bodies()), names=["road_survival"])
    files = bodies()
    http = FallbackCkan(files, file_answers={rid: [FORBIDDEN] for rid in SURVIVAL},
                        datastore={rid: store_of(files[rid]) for rid in SURVIVAL})
    outcome = build(out, work, http, names=["road_survival"])
    assert outcome["status"][0]["status"] == "built"
    assert [r["access_method"] for r in outcome["status"][0]["resources"]] == ["datastore_api"] * 4
    pages = [r for r in http.datastore_requests if "sort" in r]
    assert len(pages) == 4 and all(r["sort"] == "_id" and r["limit"] == "32000" for r in pages)
    for request in http.datastore_requests:
        fields = {f.lower() for f in request.get("fields", "").split(",") if f}
        assert not fields & NEVER, request
    for request in pages:
        assert {"tozeret_cd", "degem_cd", "moed_aliya_lakvish"} <= set(request["fields"].split(","))
    assert all("fields" not in r for r in http.datastore_requests if r.get("limit") == "0")
    assert (out / "road_survival.sqlite.gz").read_bytes() == \
        (tmp_path / "by-file" / "road_survival.sqlite.gz").read_bytes()
    blobs = [gzip.decompress((out / "road_survival.sqlite.gz").read_bytes()), (out / "manifest.json").read_bytes(),
             (out / "build_status.json").read_bytes(), B.report(outcome).encode("utf-8")]
    for blob in blobs:
        for token in (PLATE, CHASSIS, ENGINE):
            assert token.encode("utf-8") not in blob


def test_a_datastore_total_above_the_rows_read_fails_and_keeps_the_snapshot(gov):
    out, work = gov
    build(out, work, FakeCkan(bodies()), names=["new_car_prices"])
    before = (out / "new_car_prices.sqlite.gz").read_bytes()
    fields = ["tozeret_cd", "degem_cd", "shnat_yitzur", "mehir", "kinuy_mishari"]
    records = [{"_id": n, "tozeret_cd": 413, "degem_cd": 100 + n, "shnat_yitzur": 2022, "mehir": 100000 + n,
                "kinuy_mishari": "M"} for n in range(99)]
    http = FallbackCkan(bodies(), file_answers={PRICES: [FORBIDDEN]},
                        datastore={PRICES: {"fields": fields, "records": records, "total": 100}})
    status = build(out, work, http, names=["new_car_prices"])["status"][0]
    assert status["status"] == "failed" and status["reason"] == "datastore_incomplete"
    assert "total 100, 99 rows read" in status["detail"] and "file: download: HTTP 403" in status["detail"]
    assert status["previous_snapshot_preserved"] is True
    assert (out / "new_car_prices.sqlite.gz").read_bytes() == before
    assert SN.manifest(out)["datasets"]["new_car_prices"]["resources"][0]["access_method"] == "file_download"


@pytest.mark.parametrize("change,reason", [
    ({"fields": ["tozeret_cd", "degem_cd", "shnat_yitzur", "kinuy_mishari"]}, "required_column_missing"),
    ({"records": []}, "row_count_drop"),
])
def test_the_datastore_keeps_the_fail_safes(gov, change, reason):
    out, work = gov
    build(out, work, FakeCkan(bodies()), names=["new_car_prices"])
    store = {**store_of(bodies()[PRICES]), **change}
    if change.get("records") == []:
        store["records"] = store_of(bodies()[PRICES])["records"][:4]                # 4 of 7 rows: < 70 %
    http = FallbackCkan(bodies(), file_answers={PRICES: [FORBIDDEN]}, datastore={PRICES: store})
    status = build(out, work, http, names=["new_car_prices"])["status"][0]
    assert status["status"] == "failed" and status["reason"] == reason


def test_a_redirect_to_a_non_gov_host_fails_without_the_fallback(gov):
    out, work = gov
    http = FallbackCkan(bodies(), file_answers={PRICES: [(302, "https://cdn.example.net/prices.csv?x=1")]},
                        datastore={PRICES: store_of(bodies()[PRICES])})
    status = build(out, work, http, names=["new_car_prices"])["status"][0]
    assert status["status"] == "failed" and status["reason"] == "resource_unavailable"
    assert "cdn.example.net" in status["detail"] and "x=1" not in status["detail"]
    assert http.datastore_requests == [] and not (out / "new_car_prices.sqlite.gz").exists()


# --- D4: the pull-request step ---------------------------------------------------------------------------------------

def _summarize(tmp_path: Path, status_path: Path, monkeypatch) -> tuple[dict, str, str]:
    output, summary = tmp_path / "gh-output", tmp_path / "gh-summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert B.main(["--summarize-status", str(status_path)]) == 0
    lines = dict(line.split("=", 1) for line in output.read_text("utf-8").splitlines())
    return lines, summary.read_text("utf-8"), output.read_text("utf-8")


def test_every_dataset_failed_skips_the_pull_request_step(gov, tmp_path, monkeypatch):
    out, work = gov
    http = FallbackCkan(bodies(), file_answers={rid: [FORBIDDEN] for rid in (PRICES, RECALLS, *SURVIVAL)})
    outcome = build(out, work, http)
    assert all(s["status"] == "failed" for s in outcome["status"])
    outputs, summary, _ = _summarize(tmp_path, out / "build_status.json", monkeypatch)
    assert outputs["create_pr"] == "false" and outputs["built"] == "0" and outputs["failed"] == "3"
    assert "every dataset failed; the pull-request step is skipped" in summary
    for name in ("new_car_prices", "road_survival", "recall_notices"):
        assert f"- {name}: failed: resource_unavailable (download: HTTP 403 from files.test" in summary
    assert "file HTTP status 403" in summary
    # no status file at all (the build crashed): skipped too
    outputs, summary, _ = _summarize(tmp_path, tmp_path / "missing.json", monkeypatch)
    assert outputs["create_pr"] == "false" and "nothing was built" in summary


def test_one_built_dataset_opens_the_pull_request(gov, tmp_path, monkeypatch):
    out, work = gov
    http = FallbackCkan(bodies(), file_answers={RECALLS: [FORBIDDEN]})
    build(out, work, http)
    outputs, summary, _ = _summarize(tmp_path, out / "build_status.json", monkeypatch)
    assert outputs["create_pr"] == "true" and outputs["built"] == "2" and outputs["failed"] == "1"
    assert "- recall_notices: failed: resource_unavailable" in summary


def test_the_workflow_skips_the_pull_request_on_the_status_summary():
    workflow = yaml.safe_load((ROOT / ".github/workflows/build-gov-datasets.yml").read_text("utf-8"))
    steps = {s.get("id"): s for s in workflow["jobs"]["build"]["steps"] if s.get("id")}
    assert steps["status"]["if"] == "always()"
    assert "--summarize-status data/gov/build_status.json" in steps["status"]["run"]
    assert steps["cpr"]["if"] == "always() && steps.status.outputs.create_pr == 'true'"
    assert steps["paths"]["if"] == steps["cpr"]["if"]
    assert steps["cpr"]["with"]["add-paths"] == "${{ steps.paths.outputs.list }}"
    assert 'if [ -e "$path" ]' in steps["paths"]["run"]
