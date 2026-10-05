"""Primary research as SOURCE ACQUISITION: deterministic progress tracking, stopping and source priority.

Primary research exists to obtain a compact, high-value source set (official importer / manufacturer spec pages,
brochures and technical PDFs, target-market commercial pages). Every fetched document is harvested for ALL fields by
code (src/candidate_harvest.py) and inspected locally (src/document_inspection.py), so the research model does not
need to Ctrl+F every field through documents it already has. This module decides, with no model call, when that
acquisition has stopped making progress:

    acquisition mode       ACQUISITION_MODE=contract (default) | legacy, passed explicitly (no global state).
                           contract: the research model may only search and fetch (ACQUISITION_TOOLS); progress is
                           only new_usable_document, new_target_market_document and new_candidate; official_sources
                           are the official URLs of useful FETCHED documents (official_urls_discovered = fetched +
                           seen in search results, telemetry only). legacy: the definitions below, bit for bit.
    acquisition artifact   what a research turn really acquired (legacy; contract keeps the three marked *):
                           * new_usable_document       a newly retrieved page/PDF with content (2xx), not a re-read,
                                                       not a document server-side bound to ANOTHER variant
                             new_official_source       an official (importer / manufacturer / media / government) URL
                                                       not seen before, retrieved or returned by a search
                           * new_target_market_document a new usable document of the target market
                           * new_candidate             a harvested candidate for an open applicable field, from a
                                                       usable document not bound to another variant
                             new_admitted_evidence     evidence admitted for the target (not different / unbound)
                             binding_improvement       a field's best admitted binding got more exact
                           NOT artifacts: re-reading a cached document, a repeated search, a 403/404/429 or failed
                           fetch, commentary, rejected evidence, candidates of another variant.
    no-artifact stop       PRIMARY_RESEARCH_NO_ARTIFACT_STOP consecutive turns without any artifact end research.
    extension futility     (contract) beyond the normal ceiling while the minimum base is unmet: an extension turn
                           with no executed search / fetch, or 2 consecutive extension turns without a new usable
                           document, end research (extension_exhausted / under_acquired_exhausted).
    acquisition sufficiency  optional: at least PRIMARY_RESEARCH_MIN_USEFUL_DOCUMENTS useful documents AND at least
                           PRIMARY_RESEARCH_CANDIDATE_FIELD_COVERAGE_THRESHOLD % of the applicable fields covered (a
                           candidate from a useful document, or already settled). Disabled when either is 0.
    source priority        where to try first (importer, manufacturer, official documents, media, publisher,
                           aggregator, marketplace/community); for commercial fields target-market sources first.

All of it is SCHEDULING. Nothing here creates evidence, admits or rejects anything, changes a field state, a binding,
a market or a conflict; current_evaluation() remains the only field-state authority.
"""

from __future__ import annotations

import re
from typing import Any, Iterable
from urllib.parse import unquote

from .source_authority import OFFICIAL_CLASSES, classify_source, host_of, split_host, url_market
from .storage import trace
from .tail_planner import _level, candidate_key, document_profile_for, usable_document

STOP_REASONS = ("model_finished", "max_turns", "hard_max_turns_under_acquired", "no_new_artifact",
                "acquisition_sufficient", "no_new_research", "under_acquired_exhausted", "user_cancelled", "error")
# run stop_reason -> primary_research_stop_reason
STOP_REASON_MAP = {"model_finished": "model_finished", "max_steps": "max_turns", "no_new_artifact": "no_new_artifact",
                   "acquisition_sufficient": "acquisition_sufficient", "no_new_research": "no_new_research",
                   "extension_exhausted": "under_acquired_exhausted",
                   "user_cancelled": "user_cancelled", "api_failure": "error", "research_exception": "error"}

ACQUISITION_MODES = ("contract", "legacy")
# The research phase's tool surface in contract mode: discovery and retrieval only. Field work (inspection, evidence,
# field status) belongs to the harvest, the sweep and recovery. Intersected with tools.tool_specs() by the caller, so a
# tool this host cannot run (render_page without Playwright) is never offered.
ACQUISITION_TOOLS = ("search_web", "search_official_domains", "fetch_url", "fetch_pdf", "render_page")
DISCOVERY_TOOLS = ("search_web", "search_official_domains", "fetch_url", "fetch_pdf", "render_page")


def acquisition_tool_specs(specs: list[dict]) -> list[dict]:
    """The ACQUISITION_TOOLS schemas among `specs` (the host's available tool schemas)."""
    return [s for s in specs if s["function"]["name"] in ACQUISITION_TOOLS]


# --- source categories (recovery clusters; scheduling hints only) -----------------------------------------------------

ANY_MARKET_SOURCE = "official technical source, any market"


def target_market_source(target_market: str = "IL") -> str:
    return f"target-market ({target_market}) source: importer model page, brochure, price list, warranty page"


def recovery_clusters(specs: Iterable[dict]) -> dict[str, list[dict]]:
    """{recovery_cluster: [applicable field specs]} in schema order (a field without a cluster uses its group)."""
    out: dict[str, list[dict]] = {}
    for spec in specs:
        if spec.get("applicable", True):
            out.setdefault(str(spec.get("recovery_cluster") or spec.get("group") or "requested"), []).append(spec)
    return out


def cluster_source_type(specs: Iterable[dict], target_market: str = "IL") -> str:
    """The source type that usually answers a cluster: every field market-insensitive (market_sensitivity "low") ->
    any official technical source; otherwise a target-market source (the same scope rule as scoped coverage)."""
    specs = list(specs)
    if specs and all(str(s.get("market_sensitivity") or "high") == "low" for s in specs):
        return ANY_MARKET_SOURCE
    return target_market_source(target_market)


def missing_source_categories(snap: dict, specs: list[dict], target_market: str = "IL") -> list[dict]:
    """Recovery clusters with applicable OPEN fields that no in-scope source covers yet: a cluster is covered when
    some useful document (not bound to another variant) has a harvested candidate for any of its fields from a source
    in that field's market scope (`scoped_candidate_fields`, the rule of scoped coverage). A hint for the research
    model only: it never gates or changes a stop decision."""
    open_fields = set(snap.get("open_fields") or ())
    covered = set(snap.get("scoped_candidate_fields") or ())
    out = []
    for cluster, items in recovery_clusters(specs).items():
        names = [s["name"] for s in items]
        if not any(n in open_fields for n in names) or any(n in covered for n in names):
            continue
        out.append({"cluster": cluster, "source_type": cluster_source_type(items, target_market),
                    "open_fields": [n for n in names if n in open_fields]})
    return out


def missing_categories_note(missing: list[dict]) -> str:
    if not missing:
        return ""
    return "Source categories still missing: " + "; ".join(f"{m['cluster']}: {m['source_type']}" for m in missing) + "."


# --- navigation links in research fetch results (contract mode; scheduling metadata only) ----------------------------

LINK_KEYWORDS = ("spec", "specification", "technical", "brochure", "price", "pricelist", "pricing", "configurator",
                 "warranty", "מפרט", "מפרט טכני", "להורדת המפרט", "חוברת", "קטלוג", "מחירון", "מחיר", "אחריות")
MAX_NAVIGATION_LINKS = 15


def _registrable(url: str) -> str:
    try:
        _, label, suffix = split_host(host_of(url))
    except Exception:  # noqa: BLE001 - a malformed host is just "another site"
        return host_of(url)
    return f"{label}.{suffix}" if suffix else label


def model_tokens(*names: Any) -> list[str]:
    """Lower-case word tokens (>= 2 chars) of the vehicle's model name(s)."""
    out: list[str] = []
    for name in names:
        for token in re.split(r"[^\w]+", str(name or "").lower()):
            if len(token) >= 2 and token not in out:
                out.append(token)
    return out


def rank_links(links: Iterable[dict], page_url: str, tokens: Iterable[str] = (),
               limit: int = MAX_NAVIGATION_LINKS) -> list[dict]:
    """Outbound links of a fetched page ranked deterministically for navigation: same registrable domain, .pdf
    targets, specification / brochure / price-list / warranty keywords (English and Hebrew) in the anchor or URL, and
    tokens of the vehicle's model name. Drops non-http(s) links (mailto, tel, javascript), same-page #fragments and
    duplicates (by URL without fragment). Ties keep page order. Returns up to `limit` {url, text}."""
    page = str(page_url or "").split("#")[0].rstrip("/")
    domain = _registrable(page) if page else ""
    tokens = [t.lower() for t in tokens if t]
    seen: set[str] = set()
    scored = []
    for index, link in enumerate(links or []):
        if not isinstance(link, dict):
            continue
        raw = str(link.get("url") or "").strip()
        if not raw.lower().startswith(("http://", "https://")):
            continue
        url = raw.split("#")[0]
        key = url.rstrip("/")
        if not key or key == page or key in seen:
            continue
        seen.add(key)
        text = re.sub(r"\s+", " ", str(link.get("text") or "")).strip()[:120]
        haystack = (text + " " + unquote(url)).lower()
        score = 0
        if domain and _registrable(url) == domain:
            score += 8
        if url.lower().split("?")[0].endswith(".pdf"):
            score += 6
        if any(k in haystack for k in LINK_KEYWORDS):
            score += 4
        if any(t in haystack for t in tokens):
            score += 3
        scored.append((-score, index, {"url": url, "text": text}))
    return [item for _, _, item in sorted(scored, key=lambda x: (x[0], x[1]))[:max(0, int(limit))]]


def navigation_links(ctx, result: Any, tokens: Iterable[str] = ()) -> list[dict] | None:
    """Ranked outbound links of a successfully fetched HTML document, from the cached body (no fetch); the raw link
    list is stored once per document in the cache's derived storage. None when not applicable."""
    from .tools.extract import html_links

    if not isinstance(result, dict) or result.get("error") or not result.get("document_id"):
        return None
    status = result.get("status")
    if isinstance(status, int) and not 200 <= status < 300:
        return None
    doc = str(result["document_id"])
    meta = ctx.cache.get(doc) or {}
    if meta.get("doc_type") != "html":
        return None
    base = meta.get("final_url") or meta.get("url") or result.get("final_url") or result.get("url") or ""

    def compute() -> list[dict]:
        return html_links(ctx.cache.read_body(doc).decode("utf-8", errors="replace"), base)

    links, _ = ctx.cache.derived(doc, "links", compute)
    return rank_links(links or [], base, tokens)

# --- guessed URLs (PR #42 F6; scheduling telemetry + one operational note) --------------------------------------------

GUESSED_404_NOTE_AT = 2          # guessed 404s on one official domain before the model is told to use offered links


def url_key(url: Any) -> str:
    """A URL as provenance compares it: no scheme, no "www.", no fragment, no trailing slash, host lower-cased, the path
    percent-decoded (a Hebrew slug offered decoded and fetched encoded is the same URL)."""
    raw = unquote(str(url or "").strip().split("#")[0])
    raw = re.sub(r"^[a-z][a-z0-9+.-]*://", "", raw, flags=re.I)
    host, _, path = raw.partition("/")
    host = host.lower()
    host = host[4:] if host.startswith("www.") else host
    return (host + ("/" + path if path else "")).rstrip("/")


class UrlProvenance:
    """Where the URLs a run fetches come from. Offered URLs are those of search results, of fetched pages' links and
    of the site map. A fetch on an OFFICIAL domain (source_authority) of a URL never offered is a `guessed_url`; the
    run counts them (`acq_guessed_urls`) and every 404 fetch (`acq_404`) in ctx.counters. After GUESSED_404_NOTE_AT
    guessed 404s on one domain, note() returns an operational note telling the model to use only offered links on
    that domain. Scheduling telemetry only: nothing is refused, no field state changes. Never raises."""

    def __init__(self, manufacturer: str | None = None):
        self.manufacturer = manufacturer
        self.offered: set[str] = set()
        self.linked: set[str] = set()          # offered by a fetched page's links, the site map or a fetch itself
        self.search_items: dict[str, str] = {}   # PR #46 (P2.2): a search result's url + title + snippet
        self.guessed_404: dict[str, int] = {}
        self.guessed: list[dict] = []
        self._noted: dict[str, int] = {}
        self.roots_allowed: set[str] = set()   # PR #47 (A4): an official domain's root, allowed once unoffered

    def offer(self, urls: Iterable[Any]) -> None:
        for url in urls or []:
            if url:
                self.offered.add(url_key(url))
                self.linked.add(url_key(url))

    def observe_search(self, result: Any) -> None:
        if not isinstance(result, dict):
            return
        for item in result.get("results") or []:
            if isinstance(item, dict) and item.get("url"):
                key = url_key(item["url"])
                self.offered.add(key)
                text = " ".join(str(item.get(k) or "") for k in ("title", "snippet"))
                self.search_items[key] = (self.search_items.get(key, "") + " " + text).strip()[:2000]

    def is_offered(self, url: Any) -> bool:
        return url_key(url) in self.offered

    def observe_fetch(self, ctx, url: Any, result: Any) -> dict | None:
        """Classify one executed fetch (counters on ctx) and offer the fetched page's own links."""
        try:
            return self._observe_fetch(ctx, url, result)
        except Exception:  # noqa: BLE001 - telemetry must never cost the run
            return None

    def _observe_fetch(self, ctx, url: Any, result: Any) -> dict | None:
        if not url:
            return None
        official = classify_source(str(url), self.manufacturer).get("source_authority") in OFFICIAL_CLASSES
        guessed = official and url_key(url) not in self.offered
        status = result.get("status") if isinstance(result, dict) else None
        not_found = status == 404
        if guessed:
            ctx.counters["acq_guessed_urls"] += 1
        if not_found:
            ctx.counters["acq_404"] += 1
        domain = _registrable(str(url))
        if guessed and not_found:
            ctx.counters["acq_guessed_404"] += 1
            self.guessed_404[domain] = self.guessed_404.get(domain, 0) + 1
        row = {"url": str(url), "domain": domain, "guessed_url": guessed, "status": status}
        if guessed:
            self.guessed.append(row)
        self.offer([url, result.get("final_url") if isinstance(result, dict) else None])
        if isinstance(result, dict) and not result.get("error") and result.get("document_id") \
                and isinstance(status, int) and 200 <= status < 300:
            self.offer(link.get("url") for link in self._links(ctx, str(result["document_id"])))
        return row

    @staticmethod
    def _links(ctx, doc: str) -> list[dict]:
        from .tools.extract import html_links

        meta = ctx.cache.get(doc) or {}
        if meta.get("doc_type") != "html":
            return []
        base = meta.get("final_url") or meta.get("url") or ""
        links, _ = ctx.cache.derived(doc, "links", lambda: html_links(
            ctx.cache.read_body(doc).decode("utf-8", errors="replace"), base))
        return [link for link in links or [] if isinstance(link, dict)]

    def blocked_domains(self) -> list[str]:
        return sorted(d for d, n in self.guessed_404.items() if n >= GUESSED_404_NOTE_AT)

    def note(self) -> str:
        """The operational note when a domain reached (or added to) GUESSED_404_NOTE_AT guessed 404s since the last
        note; "" otherwise."""
        fresh = [d for d in self.blocked_domains() if self.guessed_404[d] != self._noted.get(d)]
        if not fresh:
            return ""
        for d in fresh:
            self._noted[d] = self.guessed_404[d]
        listed = ", ".join(f"{d} ({self.guessed_404[d]} guessed URLs returned 404)" for d in fresh)
        return (f"Stop guessing URLs on {listed}: on that domain fetch only links offered to you (search results, "
                "links of pages you fetched, the site map).")

    def summary(self) -> dict:
        return {"guessed_urls": len(self.guessed), "guessed_404_by_domain": dict(sorted(self.guessed_404.items())),
                "blocked_domains": self.blocked_domains(), "offered_urls": len(self.offered)}


# --- acquisition hygiene (PR #46, P2; refusals before a fetch) -----------------------------------------------------------
#
#   guessed_url_refused   a fetch on an il_version_sites domain (cartube / icar / auto.co.il / carzone) of a URL that no
#                         search result, fetched page's links or site map offered (and that no cached document holds) is
#                         refused: the model guessed a slug (production: a cartube URL that returned 404). PR #47 (A4):
#                         the same on official manufacturer / importer / media domains (GUESS_GUARDED_CLASSES), except
#                         the domain's root, allowed once per run
#   irrelevant_result     in primary research, a search result whose URL + title + snippet names neither the target's
#                         make nor its model family (Hebrew or Latin, the identity vocabulary's aliases) is not fetched
#                         (production: a zap.co.il TV listing). PDFs and official domains are exempt
#
# Both return the refusal to the model as the tool result and are counted (acq_guessed_url_refused /
# acq_irrelevant_result_refused; tool_blocked events with the reason).

def _cached(ctx, url: str) -> bool:
    from .storage.cache import document_id_for

    cache = getattr(ctx, "cache", None)
    if cache is None:
        return False
    try:
        return any(cache.get(document_id_for(kind, url)) for kind in ("rendered", "pdf", "fetch"))
    except Exception:  # noqa: BLE001
        return False


def target_terms(ctx) -> list[str]:
    """The target's make and model family names, Hebrew and Latin (identity vocabulary aliases), normalized."""
    from .candidate_harvest import normalize_text
    from .document_binding import vocabulary

    adm = getattr(ctx, "admission", None)
    identity = getattr(adm, "identity", None)
    vehicle = getattr(ctx, "vehicle", None) or {}
    vocab = vocabulary()
    maker = getattr(identity, "manufacturer", None) or vehicle.get("manufacturer") or ""
    family = getattr(identity, "family", None) or ""
    terms = [maker, family, vehicle.get("model") or "", *((vocab.get("manufacturers") or {}).get(str(maker), [])),
             *((vocab.get("model_families") or {}).get(str(family), []))]
    return sorted({normalize_text(str(t)).strip() for t in terms if t and len(str(t).strip()) >= 2}, key=len,
                  reverse=True)


def names_target(text: str, terms: Iterable[str]) -> bool:
    from .candidate_harvest import normalize_text

    hay = normalize_text(unquote(text or "")).replace("-", " ").replace("_", " ")
    for term in terms:
        term = term.replace("-", " ")
        if re.search(r"[א-ת]", term):
            if re.search(rf"(?<![א-ת])[ובהלמשכ]?{re.escape(term)}", hay):
                return True
        elif re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", hay):
            return True
    return False


def fetch_refusal(ctx, name: str, url: Any, phase: str) -> dict | None:
    """P2: {reason, message, ...} when this fetch must not run (module comment above); None otherwise. Never raises."""
    try:
        return _fetch_refusal(ctx, name, str(url or ""), phase)
    except Exception:  # noqa: BLE001 - a guard failure never blocks a fetch
        return None


def _fetch_refusal(ctx, name: str, url: str, phase: str) -> dict | None:
    from .il_version_pages import site_of

    if not url:
        return None
    provenance = url_provenance(ctx)
    key = url_key(url)
    site = site_of(url)
    if site and key not in provenance.offered and not _cached(ctx, url):
        return {"reason": "guessed_url_refused", "site": site, "url": url,
                "message": (f"{url} was not offered by a search result, a fetched page's links or the site map, so it "
                            f"is not fetched: on {site} use only offered URLs (search for the page, or fetch the site's "
                            "model / price-list page and follow its links).")}
    authority = classify_source(url, provenance.manufacturer).get("source_authority")
    if not site and authority in GUESS_GUARDED_CLASSES and key not in provenance.offered and not _cached(ctx, url):
        # A4 (PR #47): official / importer domains too (production: toyota.co.il /price-list, /warranty, /after-sales
        # and /cars/Corolla were guessed, all 404); the domain's root is allowed once
        domain = _registrable(url)
        if urlparse_path(url).strip("/") == "" and domain not in provenance.roots_allowed:
            provenance.roots_allowed.add(domain)
            return None
        return {"reason": "guessed_url_refused", "site": domain, "url": url, "source_authority": authority,
                "message": (f"{url} was not offered by a search result, a fetched page's links or the site map, so it "
                            f"is not fetched: on the official domain {domain} fetch only offered URLs (search for the "
                            "page, fetch the domain's home page once and follow its links, or use the site map).")}
    if phase != "research" or key not in provenance.search_items or key in provenance.linked:
        return None
    if name == "fetch_pdf" or urlparse_path(url).endswith(".pdf"):
        return None
    if classify_source(url, provenance.manufacturer).get("source_authority") in OFFICIAL_CLASSES:
        return None
    terms = target_terms(ctx)
    if not terms or names_target(f"{url} {provenance.search_items[key]}", terms):
        return None
    return {"reason": "irrelevant_result", "url": url,
            "message": (f"{url} is a search result whose URL, title and snippet name neither the target's make nor "
                        "its model, so it is not fetched. Fetch results about this vehicle.")}


GUESS_GUARDED_CLASSES = ("official_manufacturer", "official_importer", "official_media")


def urlparse_path(url: str) -> str:
    from urllib.parse import urlparse

    return unquote(urlparse(url).path or "").lower()


def url_provenance(ctx) -> UrlProvenance:
    """The run's UrlProvenance (one per tool context, created on first use)."""
    existing = getattr(ctx, "url_provenance", None)
    if existing is None:
        adm = getattr(ctx, "admission", None)
        existing = UrlProvenance(getattr(adm, "manufacturer", None) or (getattr(ctx, "vehicle", None) or {})
                                 .get("manufacturer"))
        try:
            ctx.url_provenance = existing
        except AttributeError:
            pass
    return existing


# --- source priority (scheduling only) ---------------------------------------------------------------------------

TECHNICAL_PRIORITY = {"official_importer": 1, "official_manufacturer": 2, "government": 3, "official_media": 4,
                      "publisher": 5, "aggregator": 6, "marketplace": 7, "unknown": 7}


def source_priority(url: str | None, manufacturer: str | None, target_market: str = "IL") -> dict:
    """Deterministic acquisition order of a URL: 1 = try first. `technical` for specification fields (official importer,
    official manufacturer, official documents (government / an official PDF), official media, publisher, aggregator,
    marketplace / community), `commercial` for market-bound fields (price, licence fee, trim, warranty), where a
    target-market source outranks a foreign manufacturer page. Never an allowlist and never an admission rule: a
    lower-priority source stays fully usable."""
    from .field_recovery import is_target_market

    authority = classify_source(url, manufacturer).get("source_authority") or "unknown"
    market, _ = url_market(url)
    technical = TECHNICAL_PRIORITY.get(authority, 7)
    is_pdf = str(url or "").lower().split("?")[0].endswith(".pdf")
    if authority in ("official_manufacturer", "official_media") and is_pdf:
        technical = 3                         # an official technical PDF / document
    local = is_target_market(market, target_market)
    if authority == "official_importer":
        commercial = 1
    elif local and authority in OFFICIAL_CLASSES:
        commercial = 2
    elif local:
        commercial = 3
    elif authority in OFFICIAL_CLASSES:
        commercial = 4
    else:
        commercial = 5
    return {"source_class": authority, "market": market, "priority_technical": technical,
            "priority_commercial": commercial}


def annotate_search_result(result: Any, manufacturer: str | None, target_market: str = "IL") -> Any:
    """Add the acquisition priority to every result of a search (order unchanged; nothing removed)."""
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        return result
    # copies: a result list may be shared with the search cache, which must keep the provider's results untouched
    result["results"] = [{**item, "acquisition_priority": source_priority(item["url"], manufacturer, target_market)}
                         if isinstance(item, dict) and item.get("url") else item for item in result["results"]]
    return result


# --- acquisition progress ----------------------------------------------------------------------------------------

def in_market_scope(market_sensitivity: Any, target_market_document: bool) -> bool:
    """May a candidate from a (useful) document count for a field? Any market when the field's schema
    market_sensitivity is "low", else only a target-market document (the rule of scoped coverage)."""
    return str(market_sensitivity or "high") == "low" or bool(target_market_document)


# --- document card in contract research fetch results (ACQUISITION_DOCUMENT_CARD; scheduling metadata only) -------

DOCUMENT_CARD_NOTE = "server-computed scheduling metadata, not evidence"
CARD_TOOLS = ("fetch_url", "fetch_pdf")


def document_card(*, adm, cache, specs: list[dict], result: Any, target_market: str) -> dict | None:
    """Identity / scope metadata of a successfully fetched document (None when not applicable): its variant match,
    binding level, market, authority, usability, and per recovery cluster how many applicable fields already have a
    harvested candidate from it that is IN SCOPE (in_market_scope; never for an unusable or other-variant document).
    Reads the cached harvest (the run's harvester already ran); never evidence, never a field state."""
    from .candidate_harvest import harvest_document
    from .field_recovery import is_target_market
    from .fields import normalize_field_name

    if not isinstance(result, dict) or result.get("error") or not result.get("document_id"):
        return None
    status = result.get("status")
    if isinstance(status, int) and not 200 <= status < 300:
        return None
    doc = str(result["document_id"])
    profile = document_profile_for(adm, cache, doc)
    target = is_target_market(profile.get("market"), target_market)
    usable = usable_document(cache, doc) and profile.get("variant_match") != "different"
    clusters: dict[str, int] = {}
    if usable:
        harvested, _ = harvest_document(cache, doc, specs)
        with_value = {normalize_field_name(c.get("field")) for c in harvested}
        for cluster, items in recovery_clusters(specs).items():
            count = sum(1 for spec in items if spec["name"] in with_value
                        and in_market_scope(spec.get("market_sensitivity"), target))
            if count:
                clusters[cluster] = count
    return {"note": DOCUMENT_CARD_NOTE, "variant_match": profile.get("variant_match"),
            "binding_level": profile.get("binding_level"), "market": profile.get("market"),
            "target_market_document": bool(target), "source_authority": profile.get("source_authority"),
            "usable": bool(usable), "clusters": clusters, "candidate_fields_in_scope": sum(clusters.values())}


def _url(cache, doc_id: str) -> str:
    meta = (cache.get(str(doc_id)) if cache is not None else None) or {}
    return str(meta.get("final_url") or meta.get("url") or doc_id).split("#")[0].rstrip("/")


def _search_urls(events: Iterable[dict]) -> set[str]:
    out = set()
    for pair in trace.tool_pairs(events):
        if pair["name"] in trace.SEARCH_TOOLS and isinstance(pair.get("result"), dict):
            for item in pair["result"].get("results") or []:
                if isinstance(item, dict) and item.get("url"):
                    out.add(str(item["url"]).split("#")[0].rstrip("/"))
    return out


def snapshot(*, events: list[dict], documents: Iterable[str], evaluation: list[dict], matrix: dict, adm, cache,
             target_market: str, manufacturer: str | None, sensitivity: dict[str, str] | None = None,
             mode: str) -> dict:
    """What the run has acquired so far (for one before/after comparison around a research turn).
    `sensitivity` ({field: market_sensitivity} from the schema) drives the scope-aware coverage of the safety gate.
    `mode` (contract | legacy) decides what `official_sources` means: contract = official URLs of useful FETCHED
    documents; legacy = those plus official URLs seen in search results (the historical definition)."""
    from .field_recovery import is_target_market

    open_fields = {e["field"] for e in evaluation if e["retry_eligible"]}
    applicable = set(matrix["fields"])
    profiles: dict[str, dict] = {}
    useful_docs, other_variant, target_docs = [], set(), []
    for doc in documents:
        if not usable_document(cache, doc):
            continue
        profile = profiles[doc] = document_profile_for(adm, cache, doc)
        if profile.get("variant_match") == "different":
            other_variant.add(doc)
            continue
        useful_docs.append(doc)
        if is_target_market(profile.get("market"), target_market):
            target_docs.append(doc)
    good = set(useful_docs)
    candidates = {candidate_key(c) for name in open_fields for c in matrix["fields"].get(name) or []
                  if str(c.get("document_id")) in good}
    covered = {name for name in applicable if any(str(c.get("document_id")) in good
                                                  for c in matrix["fields"].get(name) or [])}
    # an evidence-backed ok counts as covered; a self-reported not_applicable never ends acquisition early
    settled = {e["field"] for e in evaluation if e["state"] == "ok" and e["field"] in applicable}
    fetched_official = {_url(cache, d) for d in useful_docs if profiles[d].get("source_authority") in OFFICIAL_CLASSES}
    discovered = fetched_official | {u for u in _search_urls(events)
                                     if classify_source(u, manufacturer).get("source_authority") in OFFICIAL_CLASSES}
    official = fetched_official if mode == "contract" else discovered
    evidence, best = set(), {}
    for item in trace.evidence_items(events):
        if str(item.get("variant_match") or "").lower() in ("different", "unbound"):
            continue                      # bound to another variant / unbound: the evaluator ignores it too
        evidence.add(str(item.get("evidence_id")))
        name = str(item.get("field"))
        best[name] = max(best.get(name, -1), _level(item.get("binding_level")))
    # a field with a candidate from a source its schema market_sensitivity lets it use (any market when "low", else a
    # target-market source)
    target_set = set(target_docs)
    scoped_candidates = {name for name in applicable if any(
        str(c.get("document_id")) in good and in_market_scope((sensitivity or {}).get(name),
                                                               str(c.get("document_id")) in target_set)
        for c in matrix["fields"].get(name) or [])}
    return {"mode": mode, "useful_documents": useful_docs, "useful_urls": {_url(cache, d) for d in useful_docs},
            "target_market_documents": target_docs, "target_market_urls": {_url(cache, d) for d in target_docs},
            "other_variant_documents": sorted(other_variant),
            "official_sources": official, "candidates": candidates, "evidence": evidence, "best_binding": best,
            # telemetry only: official URLs of useful fetched documents / fetched + seen in search results
            "official_fetched": fetched_official, "official_urls_discovered": discovered,
            "covered_fields": covered | settled, "applicable_fields": len(applicable),
            # scope-aware coverage (minimum acquisition base): a field counts only with a candidate from a source in
            # its market scope, or ok
            "scoped_fields": settled | scoped_candidates,
            # the source-category hint (missing_source_categories): open applicable fields / in-scope candidates
            "scoped_candidate_fields": scoped_candidates, "open_fields": open_fields & applicable,
            "fields_with_candidates": len(covered),
            # telemetry only (never read by artifacts() / minimum_base() / sufficient()): every harvested candidate
            "candidate_count_total": matrix.get("candidate_count", 0)}


def artifacts(before: dict, after: dict, *, mode: str) -> list[str]:
    """Meaningful acquisition artifacts of one turn (empty: none). contract: only a new usable document, a new
    target-market document or a new candidate for an open field count; legacy: also a new official source (fetched
    or merely returned by a search), newly admitted evidence and a better binding."""
    reasons = []
    fresh = after["useful_urls"] - before["useful_urls"]      # by URL: another fetch kind of a known page is no news
    if fresh:
        reasons.append(f"new_usable_document:{len(fresh)}")
    target = after["target_market_urls"] - before["target_market_urls"]
    if target:
        reasons.append(f"new_target_market_document:{len(target)}")
    official = after["official_sources"] - before["official_sources"]
    if official and mode != "contract":
        reasons.append(f"new_official_source:{len(official)}")
    cands = after["candidates"] - before["candidates"]
    if cands:
        reasons.append(f"new_candidate:{len(cands)}")
    if mode == "contract":
        return reasons
    evidence = after["evidence"] - before["evidence"]
    if evidence:
        reasons.append(f"new_admitted_evidence:{len(evidence)}")
    better = [f for f, lvl in after["best_binding"].items() if lvl > before["best_binding"].get(f, -1)
              and f in before["best_binding"]]
    if better:
        reasons.append(f"binding_improvement:{len(better)}")
    return reasons


def state_counts(snap: dict) -> dict:
    """Observational counts of one acquisition snapshot (diagnostic telemetry; decides nothing)."""
    fetched = snap.get("official_fetched")
    return {"useful_documents": len(snap.get("useful_documents") or ()),
            # official URLs of useful FETCHED documents (search-result URLs are only "discovered")
            "official_documents": len(fetched if fetched is not None else snap.get("official_sources") or ()),
            "official_urls_discovered": len(snap.get("official_urls_discovered") or ()),
            "target_market_documents": len(snap.get("target_market_documents") or ()),
            "candidates": snap.get("candidate_count_total"),
            "open_field_candidates": len(snap.get("candidates") or ()),
            "candidate_fields": snap.get("fields_with_candidates", 0),
            "scoped_coverage_pct": scoped_coverage_pct(snap),
            "admitted_evidence": len(snap.get("evidence") or ())}


def scoped_coverage_pct(snap: dict) -> float:
    total = snap["applicable_fields"]
    return round(100.0 * len(snap.get("scoped_fields") or ()) / total, 1) if total else 0.0


def minimum_base(snap: dict, min_documents: int, min_scoped_coverage: Any) -> tuple[bool, dict]:
    """The MINIMUM ACQUISITION BASE (fail-safe of the primary-research stop): a no-artifact streak, the optional
    sufficiency transition or the normal turn ceiling may end research only once the run holds at least
    `min_documents` useful documents AND at least `min_scoped_coverage` % of the applicable fields have a candidate
    from a source in their scope (or are already ok). Scheduling only: it reads documents, candidates and the schema's
    market sensitivity, never a model's confidence, a vote, historical yield or an expected answer; it changes no
    field state, evidence, binding or conflict. Both 0 = gate off (always met)."""
    pct = threshold_pct(min_scoped_coverage)
    docs = len(snap["useful_documents"])
    scoped = round(scoped_coverage_pct(snap), 1)
    met = (not min_documents or docs >= int(min_documents)) and (not pct or scoped >= pct)
    return met, {"useful_documents": docs, "min_documents": int(min_documents or 0),
                 "scoped_coverage_pct": scoped, "min_scoped_coverage_pct": pct,
                 "target_market_documents": len(snap["target_market_documents"])}


def coverage_pct(snap: dict) -> float:
    total = snap["applicable_fields"]
    return round(100.0 * len(snap["covered_fields"]) / total, 1) if total else 0.0


def threshold_pct(value: Any) -> float:
    """A coverage threshold given as a fraction (0.6) or a percentage (60). 0 / empty = disabled."""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number * 100 if 0 < number <= 1 else number


def sufficient(snap: dict, min_documents: int, threshold: Any) -> bool:
    """Optional acquisition-sufficiency transition (scheduling only; disabled when either setting is 0)."""
    pct = threshold_pct(threshold)
    if not min_documents or not pct:
        return False
    return len(snap["useful_documents"]) >= int(min_documents) and coverage_pct(snap) >= pct


class AcquisitionTracker:
    """Per-run bookkeeping of primary research acquisition (one before/after snapshot per research turn).
    Logs `primary_research_turn` events; decides nothing about any field."""

    def __init__(self, *, run_log, ctx, specs: list[dict], vehicle: dict | None, cache, config):
        self.run_log, self.ctx, self.specs, self.vehicle, self.cache, self.config = run_log, ctx, specs, vehicle, \
            cache, config
        self.market = config.target_market
        self.manufacturer = getattr(ctx.admission, "manufacturer", None) or (vehicle or {}).get("manufacturer")
        self.sensitivity = {s["name"]: str(s.get("market_sensitivity") or "high") for s in specs}
        self.mode = getattr(config, "acquisition_mode", "contract")
        self.turns: list[dict] = []
        self.streak = 0
        self.max_streak = 0
        self.deferred: list[dict] = []     # stops the minimum acquisition base held back
        self.extended_turns = 0            # turns beyond the normal ceiling (only while under-acquired)
        # contract: consecutive EXTENSION turns (beyond the normal ceiling, base unmet) without a new usable document;
        # starts at 0 at the normal ceiling, so stalls before it never count
        self.extension_idle = 0
        self.extension_exhausted: dict | None = None
        try:
            self.start = self.last = self.take()
        except Exception as exc:     # never costs the run; an empty baseline only makes turn 1 look productive
            run_log.event("primary_research_turn_unmeasured", turn=0, error=f"{type(exc).__name__}: {exc}"[:300])
            self.start = self.last = {"useful_documents": [], "useful_urls": set(), "target_market_documents": [],
                                      "target_market_urls": set(), "other_variant_documents": [],
                                      "official_sources": set(), "candidates": set(), "evidence": set(),
                                      "best_binding": {}, "covered_fields": set(), "applicable_fields": 0,
                                      "scoped_fields": set(), "official_fetched": set(),
                                      "official_urls_discovered": set(), "scoped_candidate_fields": set(),
                                      "open_fields": set(), "mode": self.mode,
                                      "fields_with_candidates": 0}

    def take(self) -> dict:
        from .candidate_harvest import candidate_matrix
        from .field_recovery import current_evaluation
        from .storage.run_log import read_events

        events = read_events(self.run_log.events_path)
        return snapshot(events=events, documents=list(self.ctx.documents_opened),
                        evaluation=current_evaluation(events, self.specs, self.market),
                        matrix=candidate_matrix(events, self.specs, self.vehicle), adm=self.ctx.admission,
                        cache=self.cache, target_market=self.market, manufacturer=self.manufacturer,
                        sensitivity=self.sensitivity, mode=self.mode)

    def after_turn(self, step: int) -> list[str]:
        try:
            now = self.take()
        except Exception as exc:     # a scheduling snapshot must never cost the run: treat the turn as progress
            self.run_log.event("primary_research_turn_unmeasured", turn=step, error=f"{type(exc).__name__}: {exc}"[:300])
            self.streak = 0
            return ["unmeasured"]
        found = artifacts(self.last, now, mode=self.mode)
        before = self.last
        self.last = now
        self.streak = 0 if found else self.streak + 1
        self.max_streak = max(self.max_streak, self.streak)
        met, base = self.base()
        row = {"turn": step, "artifacts": found, "no_artifact_streak": self.streak,
               "useful_documents": len(now["useful_documents"]),
               "fields_with_candidates": now["fields_with_candidates"],
               "candidate_field_coverage_pct": coverage_pct(now),
               "scoped_coverage_pct": base["scoped_coverage_pct"], "minimum_acquisition_met": met}
        try:     # diagnostic telemetry only: the state before and after this turn
            row["state_before"], row["state_after"] = state_counts(before), state_counts(now)
        except Exception as exc:  # noqa: BLE001 - telemetry must never cost the run
            row["state_error"] = f"{type(exc).__name__}: {exc}"[:200]
        self.turns.append(row)
        self.run_log.event("primary_research_turn", **row)
        return found

    def base(self) -> tuple[bool, dict]:
        """Is the minimum acquisition base met (see minimum_base)? Never raises: an unreadable state counts as met,
        so a telemetry problem can never keep research running."""
        try:
            return minimum_base(self.last, self.config.primary_research_min_base_documents,
                                self.config.primary_research_min_base_scoped_coverage)
        except Exception:
            return True, {"error": "base_unmeasured"}

    def defer(self, reason: str, step: int) -> dict:
        """A stop the run wanted but the minimum acquisition base held back: logged, never silent."""
        _, base = self.base()
        entry = {"turn": step, "wanted_stop": reason, **base}
        self.deferred.append(entry)
        if reason == "max_turns":
            self.extended_turns += 1
        self.run_log.event("primary_research_stop_deferred", **entry,
                           note="under-acquired: acquisition continues (hard ceiling still applies)")
        return entry

    def extension_turn(self, step: int, found: list[str], discovery_calls: int) -> str | None:
        """Contract mode, a turn BEYOND the normal ceiling while the minimum base is unmet: is the extension futile?
        1 = the turn executed no search / fetch (blocked, reused or no discovery tool called); 2 = the second
        consecutive extension turn without a new usable document. Returns the reason (and logs it) or None. An
        unmeasured turn counts as progress, so a telemetry problem never ends research."""
        new_document = any(a.startswith("new_usable_document") or a == "unmeasured" for a in found)
        self.extension_idle = 0 if new_document else self.extension_idle + 1
        reason = None
        if not discovery_calls:
            reason = "no_discovery_calls"
        elif self.extension_idle >= 2:
            reason = "no_new_usable_document"
        if reason is None:
            return None
        _, base = self.base()
        self.extension_exhausted = {"turn": step, "reason": reason,
                                    "reason_code": 1 if reason == "no_discovery_calls" else 2,
                                    "discovery_calls": discovery_calls,
                                    "extension_turns_without_new_document": self.extension_idle, **base}
        self.run_log.event("primary_research_extension_exhausted", **self.extension_exhausted,
                           note="under-acquired, but extending further acquires nothing: research ends here; harvest, "
                                "sweep and recovery still run")
        return reason

    def missing_categories(self) -> list[dict]:
        """Source categories (recovery clusters) still missing (a hint for the research model; decides nothing).
        Never raises."""
        try:
            return missing_source_categories(self.last, self.specs, self.market)
        except Exception:  # noqa: BLE001 - a hint must never cost the run
            return []

    def sufficient(self) -> bool:
        return sufficient(self.last, self.config.primary_research_min_useful_documents,
                          self.config.primary_research_candidate_field_coverage_threshold)

    def summary(self, *, stop_reason: str | None, turns: int, model_calls: int, research_s: float | None) -> dict:
        end = self.last
        return {
            "turns": turns,
            "model_calls": model_calls,
            "max_turns": self.config.max_steps,
            "stop_reason": ("hard_max_turns_under_acquired" if stop_reason == "max_steps" and not self.base()[0]
                            else STOP_REASON_MAP.get(stop_reason or "", stop_reason)),
            "run_stop_reason": stop_reason,
            "documents_added": len(end["useful_urls"] - self.start["useful_urls"]),
            "useful_documents": len(end["useful_documents"]),
            "target_market_documents": len(end["target_market_documents"]),
            "other_variant_documents": len(end["other_variant_documents"]),
            # official URLs of useful FETCHED documents; discovered = fetched + seen in search results (telemetry)
            "official_sources": len(end.get("official_fetched", end["official_sources"])),
            "official_urls_discovered": len(end.get("official_urls_discovered") or ()),
            "acquisition_mode": self.mode,
            "extension_exhausted": self.extension_exhausted,
            "candidate_fields": end["fields_with_candidates"],
            "candidate_field_coverage_pct": coverage_pct(end),
            "no_artifact_turns": sum(1 for t in self.turns if not t["artifacts"]),
            "minimum_acquisition_met": self.base()[0],
            "minimum_acquisition": self.base()[1],
            "stop_deferred_count": len(self.deferred),
            # contract: early {"done": true} replies held back once by the minimum base
            "done_deferred_count": sum(1 for d in self.deferred if d.get("wanted_stop") == "model_finished"),
            "stops_deferred": self.deferred,
            "under_acquired_turns": sum(1 for t in self.turns if not t.get("minimum_acquisition_met", True)),
            "extended_turns": self.extended_turns,
            "scoped_coverage_pct": scoped_coverage_pct(end),
            "final_no_artifact_streak": self.streak,
            "max_no_artifact_streak": self.max_streak,
            "turn_artifacts": [{"turn": t["turn"], "artifacts": t["artifacts"]} for t in self.turns],
            "research_s": research_s,
            "settings": {"max_turns": self.config.max_steps,
                         "hard_max_turns": max(self.config.max_steps, self.config.primary_research_hard_max_turns or 0),
                         "min_base_documents": self.config.primary_research_min_base_documents,
                         "min_base_scoped_coverage": threshold_pct(self.config.primary_research_min_base_scoped_coverage),
                         "no_artifact_stop": self.config.primary_research_no_artifact_stop,
                         "min_useful_documents": self.config.primary_research_min_useful_documents,
                         "candidate_field_coverage_threshold":
                             threshold_pct(self.config.primary_research_candidate_field_coverage_threshold)},
            "note": "scheduling telemetry: acquisition progress, never field truth",
        }
