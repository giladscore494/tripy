"""Pluggable search backends (PR #46, S1-S3).

One protocol for every web search provider the engine can use:

    backend.search(query, *, site=None, country="il", lang="he", count=10) -> [SearchResult]
    SearchResult = {url, title, snippet, rank, backend, raw_url, site_native}

Backends (BACKENDS; keys are one-time secrets read from the server environment, never a per-run value):

    glm         Z.ai `search-prime` (GLM web_search, GLM_API_KEY): the baseline, unchanged (search_domain_filter)
    serper      serper.dev Google results (SERPER_API_KEY): gl=il, hl=iw, `site:` in the query
    gemini      Gemini API `gemini-3.8-flash` with the google_search tool (GEMINI_API_KEY): URLs only, read from
                groundingMetadata.groundingChunks[].web.{uri,title}; each redirect URI is resolved with one request that
                does not download the body; the model's text is discarded (src/tools/search_backends/gemini.py)
    duckduckgo  the keyless HTML endpoint, unchanged

Rules every backend's results pass (sanitize(), deterministic):

    root_only_url   a result whose URL path is empty or "/" while its title names a specific article / model (a model
                    token such as "Q3" / "X1" / "i30", a catalog family name, or a year) is dropped and counted
                    (search_root_only_urls): search-prime returns the site root with an article's title
    site filter     with a site (the `site` argument, or a `site:` prefix in the query) a result outside that registrable
                    domain is counted (search_off_site) and dropped: never offered as that site's candidate. glm keeps
                    its current behaviour (counted, not dropped): its off-site rate is what the bake-off measures

Cost per backend comes from data/search_backends.json (never code): search_calls_by_backend / search_usd_by_backend.
A backend without its key is `unavailable` (backend_status) and is never silently replaced by another one.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import unquote, urlparse

DATA_PATH = Path(__file__).resolve().parents[3] / "data" / "search_backends.json"
BACKEND_NAMES = ("glm", "serper", "gemini", "duckduckgo")
BILLABLE = ("glm", "serper", "gemini")
FALLBACK_NONE = "none"
_PRICES: dict[str, Any] = {}


@dataclass
class SearchResult:
    url: str
    title: str = ""
    snippet: str = ""
    rank: int = 0
    backend: str = ""
    raw_url: str = ""                # the URL the provider returned (Gemini: the grounding redirect URI)
    site_native: bool = False        # the provider restricted the search to the site itself (native mechanism)
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        """The result as the search tools return it (the legacy keys `site` / `published` kept for every reader)."""
        out = {k: v for k, v in asdict(self).items() if k != "extra"}
        out["site"] = urlparse(self.url).netloc
        out.setdefault("published", "")
        out.update({k: v for k, v in (self.extra or {}).items() if k not in out})
        return out


@dataclass
class CallInfo:
    """One provider call: what it cost and how it went (the caller turns it into counters and the event)."""
    backend: str
    billable: bool = True
    usd: float = 0.0
    latency_ms: int = 0
    queries: list[str] = field(default_factory=list)      # Gemini: webSearchQueries (billed)
    usage: dict = field(default_factory=dict)             # Gemini: usageMetadata
    redirects_resolved: int = 0
    redirects_failed: int = 0
    credits: float | None = None                          # PR #47 (A6): the provider's own credit count (serper)


class SearchBackend(Protocol):
    name: str

    def search(self, query: str, *, site: str | None = None, country: str = "il", lang: str = "he",
               count: int = 10) -> list[SearchResult]: ...


class BackendUnavailable(RuntimeError):
    """The selected backend cannot run here (its key is not set). Never answered by another backend."""


def settings(path: Path | str | None = None) -> dict:
    """data/search_backends.json (SEARCH_BACKENDS_PATH overrides), loaded once per file version."""
    path = Path(path or os.environ.get("SEARCH_BACKENDS_PATH") or DATA_PATH)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {"backends": {}}
    cached = _PRICES.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        data = {"backends": {}}
    _PRICES[str(path)] = (mtime, data)
    return data


def backend_settings(name: str) -> dict:
    return dict((settings().get("backends") or {}).get(name) or {})


def key_env(name: str) -> str | None:
    return backend_settings(name).get("key_env") or {"glm": "GLM_API_KEY", "serper": "SERPER_API_KEY",
                                                       "gemini": "GEMINI_API_KEY"}.get(name)


def backend_status(lookup: Callable[[str], str | None] = os.environ.get) -> dict[str, dict]:
    """{backend: {available, reason, key_env, label}}: a backend whose key variable is not set is unavailable (listed as
    such in the run settings; a run selecting it is refused, never swapped)."""
    out = {}
    for name in BACKEND_NAMES:
        env = key_env(name) if name != "duckduckgo" else None
        ok = not env or bool((lookup(env) or "").strip())
        out[name] = {"available": ok, "reason": "" if ok else f"{env} is not set", "key_env": env,
                     "label": backend_settings(name).get("label") or name}
    return out


def unavailable_reason(name: str, lookup: Callable[[str], str | None] = os.environ.get) -> str | None:
    """Why `name` cannot run here (None: it can). An unknown name is never available."""
    if not name or name == FALLBACK_NONE:
        return None
    status = backend_status(lookup).get(name)
    if status is None:
        return f"unknown search backend {name!r}"
    return None if status["available"] else status["reason"]


# --- result sanity (every backend) --------------------------------------------------------------------------------------

YEAR = re.compile(r"(?<!\d)(?:19[5-9]\d|20[0-4]\d)(?!\d)")
MODEL_TOKEN = re.compile(r"(?<![\w])(?=[\w\-]*\d)(?=[\w\-]*[a-zA-Z])[a-zA-Z0-9][\w\-]{0,10}(?![\w])")


def _family_terms() -> list[str]:
    from ...document_binding import vocabulary
    from ...candidate_harvest import normalize_text

    terms = []
    for aliases in (vocabulary().get("model_families") or {}).values():
        terms += [normalize_text(a) for a in aliases or [] if a and not re.search(r"\d", a)]
    return terms


def names_specific(title: str) -> bool:
    """Does a result title name a specific article / model: a year, a model token (letters with digits: Q3, X1, i30,
    G6) or a catalog model family name?"""
    from ...candidate_harvest import normalize_text

    text = normalize_text(title or "")
    if YEAR.search(text) or MODEL_TOKEN.search(text):
        return True
    return any(re.search(rf"(?<![\wא-ת]){re.escape(t)}(?![\wא-ת])", text) for t in _family_terms() if len(t) >= 3)


def root_only(url: str, title: str) -> bool:
    """root_only_url: the URL path is empty or "/" while the title names a specific article / model."""
    path = unquote(urlparse(str(url or "")).path or "")
    return path.strip() in ("", "/") and names_specific(title)


def site_of_query(query: str) -> str | None:
    m = re.search(r"(?:^|\s)site:(\S+)", query or "")
    return m.group(1).strip().lower().removeprefix("www.") if m else None


def strip_site(query: str) -> str:
    return re.sub(r"(?:^|\s)site:\S+", " ", query or "").strip()


def registrable(host_or_site: str) -> str:
    from ...source_authority import split_host

    host = str(host_or_site or "").lower().strip().removeprefix("www.")
    try:
        _, label, suffix = split_host(host)
    except Exception:  # noqa: BLE001 - a malformed host is its own domain
        return host
    return f"{label}.{suffix}" if suffix else label


def on_site(url: str, site: str) -> bool:
    host = urlparse(str(url or "")).netloc.lower().split(":")[0].removeprefix("www.")
    want = str(site or "").lower().removeprefix("www.")
    if not host or not want:
        return False
    return host == want or host.endswith("." + want) or registrable(host) == registrable(want)


def sanitize(results: list[dict], *, backend: str, site: str | None, query: str = "") -> tuple[list[dict], dict]:
    """(usable results, {root_only: [...], off_site: n}) of one search: root-only URLs dropped; with a site, off-site
    results counted and (every backend but glm) dropped. Ranks are kept as the provider gave them."""
    site = site or site_of_query(query)
    kept, root, off = [], [], 0
    for item in results or []:
        url = str(item.get("url") or "")
        if not url:
            continue
        if root_only(url, str(item.get("title") or "")):
            root.append({"url": url, "title": str(item.get("title") or "")[:160]})
            continue
        if site and not on_site(url, site):
            off += 1
            if backend != "glm":
                continue
        kept.append(item)
    return kept, {"root_only": root, "off_site": off, "site": site}


# --- registry --------------------------------------------------------------------------------------------------------

def make_backend(name: str, *, session=None, glm=None, timeout: tuple[float, float] = (10.0, 40.0),
                 lookup: Callable[[str], str | None] = os.environ.get):
    """A backend object. BackendUnavailable when its key is not set (fail-closed: never another backend)."""
    reason = unavailable_reason(name, lookup)
    if name == "glm":
        from .glm import GlmBackend

        if glm is None:
            raise BackendUnavailable("GLM search backend selected but no GLM client is configured")
        return GlmBackend(glm)
    if reason:
        raise BackendUnavailable(f"search backend {name} is unavailable: {reason}")
    import requests

    session = session or requests.Session()
    if name == "serper":
        from .serper import SerperBackend

        return SerperBackend(session, lookup(key_env(name)) or "", timeout=timeout)
    if name == "gemini":
        from .gemini import GeminiBackend

        return GeminiBackend(session, lookup(key_env(name)) or "", timeout=timeout)
    if name == "duckduckgo":
        from .duckduckgo import DuckDuckGoBackend

        return DuckDuckGoBackend(session, timeout=timeout)
    raise BackendUnavailable(f"unknown search backend {name!r}")
