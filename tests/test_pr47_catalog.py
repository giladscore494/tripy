"""PR #47 (Part B): the live MILO catalog, read-only. A fake query function stands in for the database (no network):

    B1  GET /api/catalog* filtering and paging (server side, indexed filter sets only), snapshot mode without
        DATABASE_URL, a catalog set start (<= 50 vehicles)
    B2  the derived trim index job writes into the data volume; the engine prefers the newer copy
    B3  the MCP's catalog_search
    the hybrid_power_pairs report
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.catalog import CatalogBrowser, CatalogQueryRefused, CatalogUnavailable
from test_api import data_root, gate  # noqa: F401  (the API fixtures: a data root with runs, a research gate)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

ROWS = [
    {"upstream_record_id": "38626", "tozar": "טויוטה", "kinuy_mishari": "COROLLA", "shnat_yitzur": 2024,
     "ramat_gimur": "BUSINESS EDI", "degem_nm": "ZWE211L DWXNBW", "degem_cd": 139, "norm_body_style": "wagon",
     "norm_propulsion_technology": "hybrid", "norm_drivetrain": "two_wheel_drive", "koah_sus": 98, "nefah_manoa": 1798,
     "hanaa_nm": "4X2", "vehicle_segment": "private", "tozeret_eretz_nm": "בריטניה"},
    {"upstream_record_id": "38647", "tozar": "טויוטה", "kinuy_mishari": "COROLLA SW", "shnat_yitzur": 2024,
     "ramat_gimur": "COMFORT SW", "degem_nm": "ZWE219L-DWXNBW", "degem_cd": 152, "norm_body_style": "wagon",
     "norm_propulsion_technology": "hybrid", "norm_drivetrain": "two_wheel_drive", "koah_sus": 140,
     "nefah_manoa": 1798, "hanaa_nm": "4X2", "vehicle_segment": "private", "tozeret_eretz_nm": "בריטניה"},
] + [{"upstream_record_id": str(90000 + n), "tozar": "טויוטה", "kinuy_mishari": "COROLLA", "shnat_yitzur": 2023,
      "ramat_gimur": f"T{n:03d}", "degem_nm": "X", "degem_cd": 500 + n, "norm_body_style": "sedan",
      "norm_propulsion_technology": "hybrid", "norm_drivetrain": "two_wheel_drive", "koah_sus": 98, "nefah_manoa": 1798,
      "hanaa_nm": "4X2", "vehicle_segment": "private", "tozeret_eretz_nm": "יפן"} for n in range(250)] + [
    {"upstream_record_id": "101136", "tozar": "אקספנג", "kinuy_mishari": "G6", "shnat_yitzur": 2026,
     "ramat_gimur": "CORE", "degem_nm": "G6", "degem_cd": 3, "norm_body_style": "suv",
     "norm_propulsion_technology": "battery_electric", "norm_drivetrain": "two_wheel_drive", "koah_sus": 258,
     "nefah_manoa": 0, "hanaa_nm": "4X2", "vehicle_segment": "private", "tozeret_eretz_nm": "סין"}]


class FakeDatabase:
    """A query function over ROWS that honours the WHERE parameters the browser sends (never SQL in general)."""

    def __init__(self, rows=ROWS):
        self.rows, self.queries = rows, []

    def __call__(self, sql: str, params: dict) -> list[dict]:
        self.queries.append((sql, dict(params)))
        assert re.match(r"^SELECT ", sql) and not re.search(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER)\b", sql)
        rows = [r for r in self.rows if self._keep(r, sql, params)]
        if "GROUP BY" in sql:
            column = re.search(r"GROUP BY (\w+)", sql).group(1)
            counts: dict = {}
            for r in rows:
                counts[r[column]] = counts.get(r[column], 0) + 1
            return [{column: k, "n": v} for k, v in counts.items()]
        if sql.startswith("SELECT count(*)"):
            return [{"n": len(rows)}]
        rows.sort(key=lambda r: (r["tozar"], r["kinuy_mishari"], -r["shnat_yitzur"], r["ramat_gimur"],
                                 r["upstream_record_id"]))
        if "LIMIT" in sql:
            rows = rows[params["offset"]:params["offset"] + params["limit"]]
        return rows

    @staticmethod
    def _keep(r, sql, p):
        checks = [("manufacturer", "tozar"), ("model", "kinuy_mishari"), ("year", "shnat_yitzur"),
                  ("trim", "ramat_gimur")]
        if any(k in p and r[c] != p[k] for k, c in checks):
            return False
        if "ids" in p and r["upstream_record_id"] not in p["ids"]:
            return False
        if "trim_text" in p and p["trim_text"].strip("%").lower() not in r["ramat_gimur"].lower():
            return False
        if "degem_cd" in p:
            return r["upstream_record_id"] == p["record_id"] or r["degem_cd"] == p["degem_cd"]
        if "record_id" in p and r["upstream_record_id"] != p["record_id"]:
            return False
        return True


# --- B1: the browser ------------------------------------------------------------------------------------------------

def test_filtering_and_paging_are_server_side():
    db = FakeDatabase()
    browser = CatalogBrowser(db)
    page = browser.search(manufacturer="טויוטה", model="COROLLA", limit=500, offset=0)
    assert page["limit"] == 200 and page["total"] == 251 and len(page["items"]) == 200
    assert page["items"][0]["record_id"] == "38626" and page["items"][0]["label"].endswith("(38626)")
    second = browser.search(manufacturer="טויוטה", model="COROLLA", year=2023, limit=100, offset=200)
    assert second["total"] == 250 and len(second["items"]) == 50
    sql, params = db.queries[-2]
    assert "tozar = %(manufacturer)s" in sql and "kinuy_mishari = %(model)s" in sql and "shnat_yitzur = %(year)s" in sql
    assert params["limit"] == 100 and params["offset"] == 200


def test_record_id_search_and_degem_cd_only_with_manufacturer_and_year():
    browser = CatalogBrowser(FakeDatabase())
    assert [i["record_id"] for i in browser.search(q="38647")["items"]] == ["38647"]
    found = browser.search(manufacturer="טויוטה", year=2024, q="139")
    assert [i["record_id"] for i in found["items"]] == ["38626"]                 # degem_cd 139
    with pytest.raises(CatalogQueryRefused):
        browser.search(q="BUSINESS")                                            # free text without manufacturer + year
    with pytest.raises(CatalogQueryRefused):
        browser.search(manufacturer="טויוטה")                                   # no indexed filter set
    with pytest.raises(CatalogQueryRefused):
        browser.search(manufacturer="טויוטה", trim="COMFORT SW")


def test_cascading_lists_are_cached_for_ten_minutes():
    db = FakeDatabase()
    now = [0.0]
    browser = CatalogBrowser(db, clock=lambda: now[0])
    makers = browser.manufacturers()
    assert {m["manufacturer"] for m in makers} == {"טויוטה", "אקספנג"}
    assert {m["model"] for m in browser.models("טויוטה")} == {"COROLLA", "COROLLA SW"}
    assert [y["year"] for y in browser.years("טויוטה", "COROLLA")] in ([2024, 2023], [2023, 2024])
    assert browser.trims("טויוטה", "COROLLA", 2024) == [{"trim": "BUSINESS EDI", "variants": 1}]
    count = len(db.queries)
    browser.manufacturers()
    browser.models("טויוטה")
    assert len(db.queries) == count                                     # cached
    now[0] = 601.0
    browser.manufacturers()
    assert len(db.queries) == count + 1                                 # expired after 10 minutes


def test_without_a_database_the_browser_is_unavailable():
    browser = CatalogBrowser(None)
    assert not browser.available
    with pytest.raises(CatalogUnavailable):
        browser.search(q="38626")


# --- B1: the HTTP API -------------------------------------------------------------------------------------------------

@pytest.fixture
def api(data_root, gate):
    from test_api import make_context, scripted_research

    context = make_context(scripted_research(gate))
    yield context
    gate.set()
    context.manager.shutdown(grace_s=5)


def test_api_catalog_endpoints_filter_and_page(api):
    api.catalog_browser = CatalogBrowser(FakeDatabase())
    with TestClient(create_app(context=api, mount_mcp=False)) as client:
        status = client.get("/api/catalog/status").json()
        assert status["mode"] == "live" and status["browser_enabled"] and status["max_set"] == 50
        page = client.get("/api/catalog", params={"manufacturer": "טויוטה", "model": "COROLLA", "year": 2023,
                                                  "limit": 10, "offset": 20}).json()
        assert page["total"] == 250 and len(page["items"]) == 10 and page["offset"] == 20
        assert client.get("/api/catalog", params={"limit": 201, "q": "1"}).status_code == 422
        assert client.get("/api/catalog", params={"manufacturer": "טויוטה"}).json()["error"]["code"] \
            == "catalog_filter_refused"
        assert client.get("/api/catalog/models", params={"manufacturer": "טויוטה"}).json()["models"]
        assert client.get("/api/catalog/trims", params={"manufacturer": "טויוטה", "model": "COROLLA",
                                                        "year": 2024}).json()["trims"][0]["trim"] == "BUSINESS EDI"


def test_snapshot_mode_disables_the_browser(api):
    api.catalog_browser = CatalogBrowser(None)
    with TestClient(create_app(context=api, mount_mcp=False)) as client:
        status = client.get("/api/catalog/status").json()
        assert status == {**status, "mode": "snapshot", "browser_enabled": False,
                          "label": "snapshot (50 benchmark records)"}
        assert client.get("/api/catalog", params={"q": "38626"}).json()["error"]["code"] == "catalog_unavailable"
        refused = client.post("/api/runs", json={"scope": "set", "record_ids": ["38626"]})
        assert refused.status_code == 422 and "snapshot" in refused.json()["error"]["message"]


def test_a_catalog_set_is_capped_at_50_and_listed_vehicles_start(api):
    api.catalog_browser = CatalogBrowser(FakeDatabase())
    with TestClient(create_app(context=api, mount_mcp=False)) as client:
        too_many = client.post("/api/runs", json={"scope": "set", "record_ids": [str(90000 + n) for n in range(51)]})
        assert too_many.status_code == 422
        unknown = client.post("/api/runs", json={"scope": "set", "record_ids": ["38626", "1"]})
        assert unknown.status_code == 422 and "1" in unknown.json()["error"]["message"]
        started = client.post("/api/runs", json={"scope": "set", "record_ids": ["38647", "90001"]})
        assert started.status_code == 201, started.json()
        record = api.manager.get(started.json()["run_id"])
        assert record.record_ids == ["38647", "90001"] and record.target["label"] == "Catalog · 2 vehicles"


def test_no_catalog_route_writes():
    from src.api.routes import catalog

    methods = {m for route in catalog.router.routes for m in route.methods}
    assert methods <= {"GET", "POST"}
    posts = [route.path for route in catalog.router.routes if "POST" in route.methods]
    assert posts == ["/api/catalog/index/rebuild"]                 # the derived index on the data volume only
    source = (Path(__file__).resolve().parent.parent / "src" / "catalog.py").read_text("utf-8")
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER)\b", source)


# --- B2: the derived trim index ---------------------------------------------------------------------------------------

def test_the_derived_index_job_writes_to_the_volume_and_the_engine_prefers_the_newer_copy(tmp_path, monkeypatch):
    from src import document_binding as DB
    from src.jobs.derived_index import DerivedIndexJob

    monkeypatch.delenv("CATALOG_TRIM_INDEX_PATH", raising=False)
    folder = tmp_path / "derived"
    job = DerivedIndexJob(folder, rows=lambda: ROWS[:2])
    assert job.due()
    state = job.build(reason="test")
    assert state["status"] == "built" and state["rows"] == 2 and (folder / "catalog_trim_index.json").is_file()
    assert job.status()["built_at"] == state["built_at"] and not job.due()
    try:
        DB.set_derived_index_dir(folder)
        assert DB.trim_index_path() == folder / "catalog_trim_index.json"           # newer than the repository's
        assert "טויוטה|corolla|2024|wagon|hybrid|two_wheel_drive|100|1.8" in DB.trim_index()["entries"]
        older = json.loads((folder / "catalog_trim_index.json").read_text("utf-8").split(', "entries"')[0] + "}")
        older["generated_at"] = "2000-01-01T00:00:00+00:00"
        (folder / "catalog_trim_index.json").write_text(json.dumps({**older, "entries": {}}), "utf-8")
        assert DB.trim_index_path() == DB.TRIM_INDEX_PATH                            # an older copy is ignored
    finally:
        DB.set_derived_index_dir(None)


def test_a_failed_build_keeps_the_previous_file_and_no_database_means_no_job(tmp_path):
    from src.jobs.derived_index import DerivedIndexJob

    folder = tmp_path / "derived"
    good = DerivedIndexJob(folder, rows=lambda: ROWS[:2])
    first = good.build()

    def boom():
        raise RuntimeError("connection refused")
    failed = DerivedIndexJob(folder, rows=boom).build()
    assert failed["status"] == "failed" and "connection refused" in failed["error"]
    assert failed["built_at"] == first["built_at"] and (folder / "catalog_trim_index.json").is_file()
    none = DerivedIndexJob(folder, rows=None)
    assert not none.available and none.build()["status"] == "unavailable" and not none.rebuild_async()


def test_the_api_rebuilds_the_index_now(api, tmp_path):
    from src.jobs.derived_index import DerivedIndexJob

    api.manager.derived_index = DerivedIndexJob(tmp_path / "derived", rows=lambda: ROWS[:2])
    with TestClient(create_app(context=api, mount_mcp=False)) as client:
        out = client.post("/api/catalog/index/rebuild")
        assert out.status_code == 202 and out.json()["started"] is True
    import time

    for _ in range(100):
        if api.manager.derived_index.status().get("status") == "built":
            break
        time.sleep(0.05)
    assert api.manager.derived_index.status()["rows"] == 2


def test_the_gov_registry_coverage_runs_before_the_pull_request():
    workflow = (Path(__file__).resolve().parent.parent / ".github" / "workflows"
                / "build-gov-registry-index.yml").read_text("utf-8")
    assert workflow.index("gov_registry_coverage.py") < workflow.index("create-pull-request")


# --- B3: the MCP tool -------------------------------------------------------------------------------------------------

def test_mcp_catalog_search(tmp_path):
    from src.mcp_server.safety import ToolInputError
    from src.mcp_server.server import TOOL_NAMES
    from src.mcp_server.tools import Observer
    from src.storage.paths import resolve_paths

    assert "catalog_search" in TOOL_NAMES
    observer = Observer(resolve_paths(lambda n: str(tmp_path) if n == "TRIPY_DATA_DIR" else ""),
                        catalog=CatalogBrowser(FakeDatabase()))
    out = observer.catalog_search(manufacturer="טויוטה", model="COROLLA", year=2024, limit=5)
    assert out["total"] == 1 and out["items"][0]["record_id"] == "38626"
    with pytest.raises(ToolInputError):
        observer.catalog_search(q="BUSINESS")
    offline = Observer(resolve_paths(lambda n: str(tmp_path) if n == "TRIPY_DATA_DIR" else ""),
                       catalog=CatalogBrowser(None))
    assert offline.catalog_search(q="38626")["available"] is False


# --- the hybrid power pairs report ------------------------------------------------------------------------------------

def test_hybrid_power_pairs_lists_groups_with_two_powers():
    import hybrid_power_pairs as H

    rows = H.groups_from_rows(ROWS[:2] + [{**ROWS[0], "upstream_record_id": "38619", "koah_sus": 152,
                                           "nefah_manoa": 1987, "degem_nm": "MZEH12L"}])
    assert len(rows) == 1
    row = rows[0]
    assert (row["family"], row["body"], row["displacement_l"], row["powers"]) == ("corolla", "wagon", "1.8", "100|140")
    assert row["records_per_power"] == "1|1" and row["model_codes_per_power"] == "ZWE211L DWXNBW|ZWE219L-DWXNBW"
    assert H.to_csv(rows).splitlines()[0].startswith("manufacturer,family,year")
