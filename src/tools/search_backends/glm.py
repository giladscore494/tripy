"""glm: Z.ai `search-prime` through the GLM client (the baseline, unchanged: the site goes into search_domain_filter,
the query is sent as written)."""

from __future__ import annotations

from . import CallInfo, SearchResult, backend_settings
from .base import Backend


class GlmBackend(Backend):
    name = "glm"

    def __init__(self, client):
        self.client = client

    def query(self, query, *, site=None, country="il", lang="he", count=10):
        t0 = self.started()
        items = self.client.web_search(query, count=count, domain=site)
        results = [SearchResult(url=str(i.get("url") or ""), title=str(i.get("title") or ""),
                                snippet=str(i.get("snippet") or ""), rank=n, backend=self.name,
                                raw_url=str(i.get("url") or ""), site_native=bool(site),
                                extra={k: i.get(k) for k in ("published", "icon", "refer", "site") if i.get(k)})
                   for n, i in enumerate(items or [], start=1)]
        usd = float(backend_settings(self.name).get("usd_per_query") or 0.0)
        return results, CallInfo(backend=self.name, usd=usd, latency_ms=self.elapsed_ms(t0))
