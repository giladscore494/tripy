"""serper: Google results through serper.dev (POST /search; gl=il, hl=iw; the site restriction is Google's native
`site:` operator in the query)."""

from __future__ import annotations

from . import CallInfo, SearchResult, backend_settings, site_of_query
from .base import Backend

ENDPOINT = "https://google.serper.dev/search"


def parse(data: dict, *, site_native: bool) -> list[SearchResult]:
    """The organic results of a serper.dev response, in rank order (`position`, else response order)."""
    out = []
    for n, item in enumerate((data or {}).get("organic") or [], start=1):
        if not isinstance(item, dict) or not item.get("link"):
            continue
        out.append(SearchResult(url=str(item["link"]), title=str(item.get("title") or ""),
                                snippet=str(item.get("snippet") or ""), rank=int(item.get("position") or n),
                                backend="serper", raw_url=str(item["link"]), site_native=site_native,
                                extra={"published": str(item.get("date") or "")} if item.get("date") else {}))
    return out


class SerperBackend(Backend):
    name = "serper"

    def __init__(self, session, api_key: str, timeout=(10.0, 40.0)):
        self.session, self.api_key, self.timeout = session, api_key, timeout

    def payload(self, query: str, site: str | None, country: str, lang: str, count: int) -> dict:
        cfg = backend_settings(self.name)
        q = query if (not site or site_of_query(query)) else f"site:{site} {query}"
        lang = {"he": "iw"}.get(lang, lang)
        return {"q": q, "gl": country or cfg.get("gl") or "il", "hl": lang or cfg.get("hl") or "iw",
                "num": max(1, min(int(count), 100))}

    def query(self, query, *, site=None, country="il", lang="he", count=10):
        t0 = self.started()
        body = self.payload(query, site, country, lang, count)
        resp = self.session.post(backend_settings(self.name).get("endpoint") or ENDPOINT, json=body,
                                 headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
                                 timeout=self.timeout)
        resp.raise_for_status()
        results = parse(resp.json(), site_native=bool(site or site_of_query(query)))
        usd = float(backend_settings(self.name).get("usd_per_query") or 0.0)
        return results, CallInfo(backend=self.name, usd=usd, latency_ms=self.elapsed_ms(t0), queries=[body["q"]])
