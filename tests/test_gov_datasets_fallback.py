"""Government datasets: diagnosable download errors (HTTP status, host, body start), the download request (headers,
one retry, redirects only to *.gov.il), the CKAN datastore fallback with its fail-safes, and the workflow's build-status
step. No network: a fake CKAN, and the real Http over a fake transport."""

from __future__ import annotations

import csv
import email.message
import gzip
import hashlib
import io
import json
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

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
    assert caught.value.kind == "redirect_off_gov" and caught.value.fallback and "evil.example.com" in str(caught.value)
    assert caught.value.redirect_host == "evil.example.com" and caught.value.host == "data.gov.il"
    assert caught.value.status == 301
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


GOOGLE = (302, "https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fe.data.gov.il%2Fx&sig=abc")


def test_a_redirect_off_gov_il_is_not_followed_and_falls_back_to_the_datastore(gov, tmp_path):
    out, work = gov
    build(tmp_path / "by-file", tmp_path / "w1", FakeCkan(bodies()), names=["new_car_prices"])
    http = FallbackCkan(bodies(), file_answers={PRICES: [GOOGLE]}, datastore={PRICES: store_of(bodies()[PRICES])})
    outcome = build(out, work, http, names=["new_car_prices"])
    status = outcome["status"][0]
    assert status["status"] == "built"
    assert [u for u, _ in http.transport.requests] == [file_url(PRICES)]               # Google is never requested
    res = status["resources"][0]
    assert res["access_method"] == "datastore_api" and res["file_http_status"] == 302
    assert res["file_redirect_host"] == "accounts.google.com"
    entry = outcome["manifest"]["datasets"]["new_car_prices"]["resources"][0]
    assert entry["access_method"] == "datastore_api"
    attempt = entry["file_attempt"]
    assert {"http_status": attempt["http_status"], "redirect_host": attempt["redirect_host"]} == \
        {"http_status": 302, "redirect_host": "accounts.google.com"}
    assert "sig=abc" not in json.dumps(entry) and "ServiceLogin" not in json.dumps(entry)
    assert (out / "new_car_prices.sqlite.gz").read_bytes() == \
        (tmp_path / "by-file" / "new_car_prices.sqlite.gz").read_bytes()
    assert "redirect to accounts.google.com" in B.exit_summary(outcome["status"])["text"]


def test_a_redirect_off_gov_il_without_the_datastore_fails_with_both_reasons(gov):
    out, work = gov
    http = FallbackCkan(bodies(), file_answers={PRICES: [GOOGLE]})
    status = build(out, work, http, names=["new_car_prices"])["status"][0]
    assert status["status"] == "failed" and status["reason"] == "resource_unavailable"
    assert status["detail"] == ("file: download: HTTP 302 from files.test: redirect to a non-gov host "
                                "accounts.google.com refused; datastore: not used (datastore_active is not true)")
    assert http.datastore_requests == [] and not (out / "new_car_prices.sqlite.gz").exists()


def test_a_redirect_off_gov_il_and_a_failed_datastore_fail_with_both_reasons(gov):
    out, work = gov
    store = {**store_of(bodies()[PRICES]), "total": 8}
    http = FallbackCkan(bodies(), file_answers={PRICES: [GOOGLE]}, datastore={PRICES: store})
    status = build(out, work, http, names=["new_car_prices"])["status"][0]
    assert status["status"] == "failed" and status["reason"] == "datastore_incomplete"
    assert status["detail"].startswith("file: download: HTTP 302 from files.test: redirect to a non-gov host "
                                       "accounts.google.com refused; datastore: total 8, 7 rows read")


def test_every_survival_resource_is_tried_and_reported_but_nothing_is_built_unless_all_succeed(gov):
    out, work = gov
    files = bodies()
    # the first cancelled file is redirected to Google without a datastore; the other three are fine
    http = FallbackCkan(files, file_answers={CANCELLED[0]: [GOOGLE]})
    outcome = build(out, work, http, names=["road_survival"])
    status = outcome["status"][0]
    assert status["status"] == "failed" and status["resource_id"] == CANCELLED[0]
    assert status["detail"].startswith(f"1 of 4 resources failed: [{CANCELLED[0]}] resource_unavailable: file: "
                                       "download: HTTP 302")
    assert [(r["resource_id"], r["status"]) for r in status["resources"]] == \
        [(CANCELLED[0], "failed"), (CANCELLED[1], "ok"), (CANCELLED[2], "ok"), (ACTIVE, "ok")]
    assert [r["rows"] for r in status["resources"]] == [None, 10, 0, 300]
    assert status["resources"][0]["file_redirect_host"] == "accounts.google.com"
    assert not (out / "road_survival.sqlite.gz").exists()
    text = B.exit_summary(outcome["status"])["text"]
    assert text.count(" via file_download") == 4 and "redirect to accounts.google.com" in text
    assert "failed: resource_unavailable" in B.report(outcome)


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
    for name in ("new_car_prices", "recall_notices"):
        assert f"- {name}: failed: resource_unavailable (file: download: HTTP 403 from files.test" in summary
    assert "- road_survival: failed: resource_unavailable (4 of 4 resources failed: " in summary
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


def _step(text: str, step_id: str) -> str:
    """The text of the workflow step with this id (from its `- name:` line to the next step)."""
    blocks = text.split("\n      - ")
    return next(b for b in blocks if f"\n        id: {step_id}\n" in b)


def test_the_workflow_skips_the_pull_request_on_the_status_summary():
    text = (ROOT / ".github/workflows/build-gov-datasets.yml").read_text("utf-8")
    status, paths, cpr = _step(text, "status"), _step(text, "paths"), _step(text, "cpr")
    assert "        if: always()\n" in status
    assert "run: python scripts/build_gov_datasets.py --summarize-status data/gov/build_status.json" in status
    condition = "        if: always() && steps.status.outputs.create_pr == 'true'\n"
    assert condition in cpr and condition in paths
    assert "add-paths: ${{ steps.paths.outputs.list }}" in cpr and "data/gov/*.sqlite.gz" not in cpr
    assert 'if [ -e "$path" ]' in paths
    assert text.index("id: status") < text.index("id: paths") < text.index("id: cpr")


# --- D3: an estimated datastore total ---------------------------------------------------------------------------------

class BigDatastore:
    """A datastore of `rows` generated records (field `a`) whose first page reports `total` (estimated or not), with
    or without datastore_search_sql."""

    def __init__(self, rows: int, total: int, estimated: bool, sql_count: int | None = None):
        self.rows, self.total, self.estimated, self.sql_count = rows, total, estimated, sql_count
        self.calls: list[tuple[str, dict]] = []

    def get_json(self, url: str, retries: int | None = None) -> dict:
        parsed = urllib.parse.urlparse(url)
        action, params = parsed.path.rsplit("/", 1)[-1], dict(urllib.parse.parse_qsl(parsed.query))
        self.calls.append((action, params))
        if action == "datastore_search_sql":
            if self.sql_count is None:
                return {"success": False, "error": {"message": "Access denied: datastore_search_sql"}}
            return {"success": True, "result": {"records": [{"n": self.sql_count}], "fields": [{"id": "n"}]}}
        assert action == "datastore_search", url
        if params["limit"] == "0":
            return {"success": True, "result": {"fields": [{"id": "_id"}, {"id": "a"}], "records": [],
                                                "total": self.total, "total_was_estimated": self.estimated}}
        offset, limit = int(params["offset"]), int(params["limit"])
        page = [{"a": "x"}] * max(0, min(limit, self.rows - offset))
        return {"success": True, "result": {"records": page, "total": self.total,
                                            "total_was_estimated": self.estimated}}


def read_big(store: BigDatastore) -> dict:
    from src.gov_data import ingest as I
    from src.gov_data import provenance as P
    ctx = I.Context(http=store, base="https://data.gov.il/api/3/action", accepted_formats=["CSV"], min_row_ratio=0.7,
                    ingestion_version="v", rate=ckan.RateLimit(clock=lambda: 0.0, sleep=lambda s: None))
    seen = [0]

    def on_row(row):
        seen[0] += 1
    try:
        fields, _ = I._from_datastore(ctx, {"columns": {"required": ["a"]}}, "res-1", None, on_row, {})
    except P.DatasetFailed as exc:
        return {"failed": exc.reason, "detail": exc.detail, "seen": seen[0]}
    return {**fields, "seen": seen[0]}


def test_an_estimated_total_within_2_percent_and_a_short_last_page_is_accepted():
    store = BigDatastore(rows=995_000, total=1_000_000, estimated=True)
    out = read_big(store)
    assert "failed" not in out, out
    assert out["row_count"] == out["seen"] == 995_000
    assert out["datastore_total"] == 1_000_000 and out["total_was_estimated"] is True
    assert out["total_check"] == "estimate_within_2pct" and "datastore_exact_count" not in out
    assert [a for a, _ in store.calls].count("datastore_search_sql") == 1                    # tried once
    sql = next(p for a, p in store.calls if a == "datastore_search_sql")["sql"]
    assert sql == 'SELECT COUNT(*) AS n FROM "res-1"'


def test_an_estimated_total_more_than_2_percent_off_fails():
    out = read_big(BigDatastore(rows=900_000, total=1_000_000, estimated=True))
    assert out["failed"] == "datastore_incomplete" and "1000000 (estimated)" in out["detail"]
    assert "900000 rows read" in out["detail"] and "10.00% off" in out["detail"]


def test_an_exact_total_keeps_strict_equality():
    out = read_big(BigDatastore(rows=99_999, total=100_000, estimated=False))
    assert out["failed"] == "datastore_incomplete" and out["detail"] == "total 100000, 99999 rows read"
    store = BigDatastore(rows=100_000, total=100_000, estimated=False)
    assert read_big(store)["total_check"] == "exact_total"
    assert "datastore_search_sql" not in [a for a, _ in store.calls]                          # not needed


def test_the_sql_count_is_used_when_available():
    store = BigDatastore(rows=995_000, total=1_000_000, estimated=True, sql_count=995_000)
    out = read_big(store)
    assert out["total_check"] == "exact_count" and out["datastore_exact_count"] == 995_000
    assert out["total_was_estimated"] is True and out["datastore_total"] == 1_000_000
    # an exact count is strict: one row off fails, even inside the 2 % of the estimate
    out = read_big(BigDatastore(rows=995_000, total=1_000_000, estimated=True, sql_count=995_001))
    assert out["failed"] == "datastore_incomplete" and "exact count 995001" in out["detail"]


def test_the_sql_count_rejects_an_unsafe_resource_id():
    store = BigDatastore(rows=1, total=1, estimated=True, sql_count=1)
    rate = ckan.RateLimit(clock=lambda: 0.0, sleep=lambda s: None)
    assert ckan.datastore_count(store, "https://b", 'x" ; DROP TABLE y; --', rate) is None and store.calls == []
    assert ckan.datastore_count(store, "https://b", "053cea08-09bc-40ec-8f7a-156f0677aff3", rate) == 1


def test_the_estimate_check_is_recorded_in_the_report(gov):
    out, work = gov
    store = store_of(bodies()[PRICES])

    class Estimated(FallbackCkan):
        def get_json(self, url, retries=None):
            body = super().get_json(url, retries)
            if "datastore_search" in url and "sort=" in url:
                body["result"]["total_was_estimated"] = True
                body["result"]["total"] = 7
            return body
    http = Estimated(bodies(), file_answers={PRICES: [FORBIDDEN]}, datastore={PRICES: store})
    outcome = build(out, work, http, names=["new_car_prices"])
    res = outcome["manifest"]["datasets"]["new_car_prices"]["resources"][0]
    assert res["total_check"] == "estimate_within_2pct" and res["total_was_estimated"] is True
    assert res["datastore_total"] == 7
    text = B.report(outcome)
    assert "total check estimate_within_2pct" in text and "| estimate_within_2pct |" in text
