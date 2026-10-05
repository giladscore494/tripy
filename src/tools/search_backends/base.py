"""The shared shape of a backend: `query()` returns (results, CallInfo); `search()` is the protocol (results only)."""

from __future__ import annotations

import time

from . import CallInfo, SearchResult


class Backend:
    name = ""

    def query(self, query: str, *, site: str | None = None, country: str = "il", lang: str = "he",
              count: int = 10) -> tuple[list[SearchResult], CallInfo]:
        raise NotImplementedError

    def search(self, query: str, *, site: str | None = None, country: str = "il", lang: str = "he",
               count: int = 10) -> list[SearchResult]:
        return self.query(query, site=site, country=country, lang=lang, count=count)[0]

    @staticmethod
    def started() -> float:
        return time.monotonic()

    @staticmethod
    def elapsed_ms(t0: float) -> int:
        return int((time.monotonic() - t0) * 1000)
