"""The facts API latency fixes (2026-10-09 production log): the process-wide MILO connection pool, the 10-minute
government row cache and the picker warm-up. No network: a fake pool / query stands in for MILO."""

from __future__ import annotations

import contextlib
import copy
import logging
import threading

import pytest

from src import catalog as C
from src.catalog import CatalogBrowser
from src.facts.service import GOV_CACHE_SIZE, GOV_TTL_S, FactsService
from test_facts_api import KEY, FakeMilo, snapshot_rows


# --- the connection pool --------------------------------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self.conn.executed.append((sql, params))

    def fetchall(self):
        return [{"n": 1}]


class FakeConnection:
    def __init__(self):
        self.read_only = False
        self.executed = []

    def cursor(self):
        return FakeCursor(self)


class FakePool:
    """psycopg_pool.ConnectionPool's surface used by catalog: connects lazily (counted), hands out its connection."""
    instances: list["FakePool"] = []

    def __init__(self, conninfo, **kwargs):
        self.conninfo, self.kwargs = conninfo, kwargs
        self.connects, self.checkouts, self.conn = 0, 0, None
        FakePool.instances.append(self)

    @staticmethod
    def check_connection(conn):
        return None

    @contextlib.contextmanager
    def connection(self):
        if self.conn is None:
            self.connects += 1
            self.conn = FakeConnection()
            self.kwargs["configure"](self.conn)
        self.checkouts += 1
        yield self.conn


@pytest.fixture
def fake_pool(monkeypatch):
    import psycopg_pool

    FakePool.instances = []
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", FakePool)
    monkeypatch.setattr(C, "_POOLS", {})
    return FakePool


def test_the_pool_is_reused_across_calls(fake_pool):
    query = C.database_query("postgresql://reader@db.example/postgres")
    for _ in range(10):
        assert query("SELECT 1 AS n", {}) == [{"n": 1}]
    again = C.database_query("postgresql://reader@db.example/postgres")     # the MCP / the fingerprint: same pool
    again("SELECT 1 AS n", {})
    [pool] = fake_pool.instances
    assert pool.connects == 1 and pool.checkouts == 11
    assert pool.conn.read_only is True                                      # every connection: read-only
    assert (pool.kwargs["min_size"], pool.kwargs["max_size"]) == (1, 5)
    assert pool.kwargs["check"] is FakePool.check_connection               # health-checked on checkout
    assert pool.kwargs["kwargs"]["prepare_threshold"] is None               # safe behind the transaction pooler
    assert set(C.take_timings()) == {"acquire_ms", "execute_ms", "fetch_ms", "release_ms"}
    assert C.take_timings() == {}                                           # taken once


def test_the_placement_names_regions_never_the_secret():
    dsn = "postgresql://postgres.ref:s3cret@aws-0-eu-west-1.pooler.supabase.com:6543/postgres"
    placed = C.placement(dsn, {"RAILWAY_REPLICA_REGION": "europe-west4-drams3a"}.get)
    assert placed == {"service_region": "europe-west4-drams3a", "db_region": "eu-west-1", "db_host_kind": "pooler",
                      "db_port": 6543}
    assert "s3cret" not in str(placed) and "ref" not in str(placed)


# --- the government row cache ---------------------------------------------------------------------------------------------

def gov_service(milo, now, **kw) -> FactsService:
    return FactsService(milo, rows_by_source=lambda row: snapshot_rows(row), gov_clock=lambda: now[0], **kw)


def test_the_government_row_is_cached_for_ten_minutes():
    assert (GOV_TTL_S, GOV_CACHE_SIZE) == (600.0, 5000)
    milo, now = FakeMilo(), [1000.0]
    svc = gov_service(milo, now)
    first = svc.government_rows([KEY["22010"]])
    original = copy.deepcopy(first)
    first[KEY["22010"]]["mispar_moshavim"] = 99                             # a caller's change never reaches the cache
    now[0] += 599.0
    stats: dict = {}
    assert svc.government_rows([KEY["22010"]], stats) == original          # a hit within 10 minutes
    assert len(milo.queries) == 1 and stats == {"cached": 1, "read": 0}
    now[0] += 2.0                                                            # 601 s after the read: expired
    svc.government_rows([KEY["22010"]], stats)
    assert len(milo.queries) == 2 and stats["read"] == 1 and "decode_ms" in stats


def test_only_the_keys_not_cached_are_read_and_unknown_keys_are_cached_too():
    milo, now = FakeMilo(), [0.0]
    svc = gov_service(milo, now)
    svc.government_rows([KEY["22010"]])
    rows = svc.government_rows([KEY["22010"], KEY["19754"], "no-such-key"])
    assert set(rows) == {KEY["22010"], KEY["19754"]}
    assert milo.queries[1][1]["keys"] == [KEY["19754"], "no-such-key"]
    assert svc.government_rows(["no-such-key"]) == {} and len(milo.queries) == 2


def test_the_government_cache_is_capped():
    milo, now = FakeMilo(), [0.0]
    svc = gov_service(milo, now, gov_cache_size=2)
    for record in ("22010", "19754", "85167"):
        now[0] += 1.0
        svc.government_rows([KEY[record]])
    assert list(svc._gov) == [KEY["19754"], KEY["85167"]]                  # the least recently used dropped
    svc.government_rows([KEY["22010"]])
    assert len(milo.queries) == 4


def test_a_failed_read_is_not_cached_and_the_call_log_carries_the_read():
    from src.catalog import CatalogUnavailable

    calls = []

    def flaky(sql, params):
        calls.append(params)
        if len(calls) == 1:
            raise OSError("down")
        return FakeMilo()(sql, params)

    svc = gov_service(flaky, [0.0])
    with pytest.raises(CatalogUnavailable):
        svc.records([KEY["22010"]])
    _, line = svc.records([KEY["22010"]])
    assert line["gov"]["read"] == 1 and line["status"] == {KEY["22010"]: "ok"}
    _, line = svc.records([KEY["22010"]])
    assert line["gov"] == {"cached": 1, "read": 0} and len(calls) == 2


# --- the picker warm-up and its 6 h cache ---------------------------------------------------------------------------------

class PickerDb:
    """20 manufacturers, manufacturer i with i private variants; one model each. `gate` holds every query."""

    def __init__(self, gate: threading.Event | None = None, fail: bool = False):
        self.gate, self.fail, self.queries = gate, fail, []

    def __call__(self, sql, params):
        if self.gate is not None:
            assert self.gate.wait(5)
        self.queries.append((sql, params))
        if self.fail:
            raise OSError("MILO down")
        assert params.get("segment") == "private"
        if "GROUP BY tozar" in sql:
            return [{"tozar": f"m{i:02d}", "n": i} for i in range(1, 21)]
        return [{"kinuy_mishari": f"{params['manufacturer']}-model", "n": 1}]


def test_the_warm_up_fills_the_picker_cache_without_blocking():
    gate = threading.Event()
    db = PickerDb(gate)
    browser = CatalogBrowser(db)
    thread = browser.start_picker_warmup("private")                      # returns while MILO is still answering
    assert thread is not None and thread.is_alive() and db.queries == []
    assert browser.start_picker_warmup("private") is thread              # once per browser
    gate.set()
    thread.join(5)
    assert not thread.is_alive()
    warmed = [p["manufacturer"] for _, p in db.queries if "manufacturer" in p]
    assert warmed == [f"m{i:02d}" for i in range(20, 5, -1)]             # the 15 largest manufacturers' models
    count = len(db.queries)
    browser.manufacturers(segment="private")
    browser.models("m20", segment="private")
    assert len(db.queries) == count                                      # served from the cache


def test_a_failing_warm_up_only_logs(caplog):
    browser = CatalogBrowser(PickerDb(fail=True))
    with caplog.at_level(logging.WARNING, logger="tripy.facts"):
        browser.start_picker_warmup("private").join(5)
    assert "facts picker warm-up failed: OSError" in caplog.text
    assert CatalogBrowser(None).start_picker_warmup("private") is None   # no database: nothing to warm


def test_the_picker_lists_live_six_hours_the_catalog_lists_ten_minutes():
    now = [0.0]
    db = PickerDb()
    browser = CatalogBrowser(db, clock=lambda: now[0])
    browser.manufacturers(segment="private")
    now[0] = 6 * 3600 - 1
    browser.manufacturers(segment="private")
    assert len(db.queries) == 1
    now[0] = 6 * 3600 + 1
    browser.manufacturers(segment="private")
    assert len(db.queries) == 2
    assert (C.CACHE_TTL_S, C.PICKER_TTL_S) == (600.0, 6 * 3600.0)        # /api/catalog (no segment) keeps 10 minutes
