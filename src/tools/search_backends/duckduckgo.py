"""duckduckgo: the keyless HTML endpoint (unchanged: `site:` prefixed to the query)."""

from __future__ import annotations

from . import CallInfo, SearchResult
from .base import Backend


class DuckDuckGoBackend(Backend):
    name = "duckduckgo"

    def __init__(self, session, timeout=(10.0, 40.0)):
        self.session, self.timeout = session, timeout

    def query(self, query, *, site=None, country="il", lang="he", count=10):
        from ..fetch import USER_AGENT
        from ..search import parse_duckduckgo

        t0 = self.started()
        q = f"site:{site} {query}" if site else query
        resp = self.session.get("https://html.duckduckgo.com/html/", params={"q": q},
                                headers={"User-Agent": USER_AGENT, "Accept-Language": "he,en;q=0.9"},
                                timeout=self.timeout)
        resp.raise_for_status()
        results = [SearchResult(url=i["url"], title=i.get("title") or "", snippet=i.get("snippet") or "", rank=n,
                                backend=self.name, raw_url=i["url"], site_native=bool(site))
                   for n, i in enumerate(parse_duckduckgo(resp.text, count), start=1)]
        return results, CallInfo(backend=self.name, billable=False, latency_ms=self.elapsed_ms(t0))
