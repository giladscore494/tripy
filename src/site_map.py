"""Importer site map for source acquisition (Part D): real URLs on the official sites instead of guessed ones.

    official domains of the target (search.default_domains: importer first)
            ↓  robots.txt -> its Sitemap: entries, else /sitemap.xml and /sitemap_index.xml
            ↓  XML sitemaps and sitemap indexes (also .xml.gz), standard library only (xml.etree, gzip);
            ↓  nested indexes to depth 3, <= 50 sitemap files and <= 20,000 URLs per domain
            ↓  cached per domain under the cache root for 7 days (single flight), shared by every vehicle; a crawl the
            ↓  deadline cut short is cached as `partial: true` for 1 hour only (a URL / sitemap count cap is not partial)
    rank_urls   model / family tokens (Latin and Hebrew), model year, intent keywords per recovery cluster, .pdf;
                URLs of another model family dropped; robots.txt Disallow respected; top 15 with reasons

Discovery and routing metadata only: a sitemap is never stored as a document, never counts as an acquisition
artifact, and nothing here is evidence or decides a field. Every entry point is bounded and never raises into the run:
the overall deadline is checked while a response body streams in (a slow server is cut), each file is capped at
15 MB on the wire and 50 MB decompressed (a gzip bomb is stopped), and the per-domain single-flight wait times out at
the remaining deadline (the caller then goes on without that domain's site map).
"""

from __future__ import annotations

import gzip
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import unquote, urlparse
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

SITE_MAP_VERSION = "site-map-v2"  # old cache entries cannot distinguish deadline-partial from complete crawls
MAX_DEPTH = 3
MAX_SITEMAPS = 50
MAX_URLS = 20000
MAX_FILE_BYTES = 15 * 1024 * 1024           # one sitemap / robots.txt file as received
MAX_SITEMAP_BYTES = 50 * 1024 * 1024        # a decompressed (gzip) sitemap file
TTL_S = 7 * 24 * 3600
PARTIAL_TTL_S = 3600                        # a crawl the deadline cut short
TOP_N = 15
DEFAULT_DEADLINE_S = 20.0
FALLBACK_PATHS = ("/sitemap.xml", "/sitemap_index.xml")

# intent keywords per recovery cluster (URL path / file name words, English and Hebrew)
BROCHURE = ("brochure", "catalog", "catalogue", "קטלוג", "חוברת", "ברושור")
INTENTS: dict[str, tuple[str, ...]] = {
    "technical_spec": ("spec", "specs", "specification", "specifications", "technical", "data", "מפרט", "מפרט-טכני"),
    "performance": ("spec", "specs", "specification", "specifications", "technical", "performance", "מפרט"),
    "charging_ev": ("spec", "specification", "specifications", "charging", "technical", "טעינה", "מפרט"),
    "tires_wheels": ("spec", "specification", "specifications", "technical", "wheels", "מפרט"),
    "multimedia": ("equipment", "features", "multimedia", "אבזור", "spec", "specifications", "מפרט"),
    "equipment": ("equipment", "features", "trim", "trims", "versions", "אבזור", "גרסאות", "spec", "מפרט"),
    "commercial": ("price", "prices", "pricelist", "price-list", "מחירון", "מחיר", "מחירים"),
    "warranty": ("warranty", "אחריות", "service", "שירות"),
}
ALL_INTENTS = tuple(dict.fromkeys(k for words in INTENTS.values() for k in words)) + BROCHURE


class SiteMapDeadline(TimeoutError):
    """The site map's overall deadline passed (while a body was streaming in, or before a crawl step)."""


class SitemapTooLarge(ValueError):
    """A sitemap file decompresses beyond MAX_SITEMAP_BYTES."""


# --- fetching --------------------------------------------------------------------------------------------------------

def http_fetch(ctx, url: str, deadline: float) -> tuple[int, bytes]:
    """(status, body) through the run's HTTP session with the fetch tools' user agent; timeouts are the tool
    timeouts, never beyond the deadline. The body streams in chunks: past the deadline the read is aborted
    (SiteMapDeadline); past MAX_FILE_BYTES (or the tools' response cap) it is cut. Raises on a network error (the
    caller records it)."""
    from .tools.fetch import USER_AGENT

    remaining = max(0.5, deadline - time.monotonic())
    cfg = ctx.config
    cap = min(int(getattr(cfg, "max_response_bytes", MAX_FILE_BYTES) or MAX_FILE_BYTES), MAX_FILE_BYTES)
    resp = ctx.session.get(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Language": "he,en;q=0.9"},
                           timeout=(min(cfg.connect_timeout_s, remaining), min(cfg.read_timeout_s, remaining)),
                           stream=True, allow_redirects=True)
    chunks, size = [], 0
    try:
        for chunk in resp.iter_content(64 * 1024):
            if time.monotonic() > deadline:
                raise SiteMapDeadline(f"site map deadline passed while reading {url}")
            if not chunk:
                continue
            size += len(chunk)
            if size > cap:
                break
            chunks.append(chunk)
    finally:
        resp.close()
    return resp.status_code, b"".join(chunks)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_sitemap(body: bytes) -> tuple[str, list[str]]:
    """("urlset" | "sitemapindex" | "unknown", <loc> values) of one sitemap file (gzip or plain XML). Documents
    declaring entities are refused (no entity expansion). A gzip file is decompressed in chunks and refused
    (SitemapTooLarge) once it exceeds MAX_SITEMAP_BYTES, so a gzip bomb never expands further."""
    if body[:2] == b"\x1f\x8b":
        parts, size = [], 0
        with gzip.GzipFile(fileobj=__import__("io").BytesIO(body)) as handle:
            while True:
                part = handle.read(1024 * 1024)
                if not part:
                    break
                size += len(part)
                if size > MAX_SITEMAP_BYTES:
                    raise SitemapTooLarge(f"decompressed sitemap exceeds {MAX_SITEMAP_BYTES} bytes")
                parts.append(part)
        body = b"".join(parts)
    head = body[:4096].lower()
    if b"<!entity" in head or b"<!doctype" in head:
        return "unknown", []
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return "unknown", []
    kind = _local(root.tag)
    locs = []
    for node in root.iter():
        if _local(node.tag) == "loc" and node.text and node.text.strip():
            locs.append(node.text.strip())
    return (kind if kind in ("urlset", "sitemapindex") else "unknown"), locs


def crawl_domain(domain: str, fetch: Callable[[str], tuple[int, bytes]], *, deadline: float,
                 max_depth: int = MAX_DEPTH, max_sitemaps: int = MAX_SITEMAPS, max_urls: int = MAX_URLS) -> dict:
    """{domain, robots, sitemaps, urls, errors, truncated, partial} for one domain. `fetch(url) -> (status, body)` may
    raise; every failure is recorded and the crawl goes on with what it has. Bounded by the deadline and the caps.
    `truncated`: a cap or the deadline stopped the crawl; `partial`: the DEADLINE did (a short-lived cache entry)."""
    out: dict[str, Any] = {"domain": domain, "robots": "", "sitemaps": [], "urls": [], "errors": [],
                           "truncated": False, "partial": False}
    base = f"https://{domain}"
    roots: list[str] = []
    try:
        status, body = fetch(base + "/robots.txt")
        if 200 <= status < 300:
            out["robots"] = body.decode("utf-8", errors="replace")[:200000]
            parser = RobotFileParser()
            parser.parse(out["robots"].splitlines())
            roots = list(parser.site_maps() or [])
    except SiteMapDeadline:
        out["truncated"] = out["partial"] = True
        out["errors"].append("robots.txt: deadline")
        return out
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"robots.txt: {type(exc).__name__}: {str(exc)[:120]}")
    if not roots:
        roots = [base + p for p in FALLBACK_PATHS]
    queue = [(u, 0) for u in roots]
    seen_maps: set[str] = set()
    urls: list[str] = []
    seen_urls: set[str] = set()
    while queue:
        url, depth = queue.pop(0)
        if url in seen_maps:
            continue
        if time.monotonic() > deadline:
            out["truncated"] = out["partial"] = True
            break
        if len(seen_maps) >= max_sitemaps:
            out["truncated"] = True
            break
        seen_maps.add(url)
        try:
            status, body = fetch(url)
        except SiteMapDeadline:
            out["truncated"] = out["partial"] = True
            out["errors"].append(f"{url}: deadline")
            break
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"{url}: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        if not 200 <= status < 300:
            if url not in (base + p for p in FALLBACK_PATHS) or status != 404:
                out["errors"].append(f"{url}: HTTP {status}")
            continue
        try:
            kind, locs = parse_sitemap(body)
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"{url}: {type(exc).__name__}")
            continue
        out["sitemaps"].append(url)
        if kind == "sitemapindex":
            if depth + 1 <= max_depth:
                queue.extend((loc, depth + 1) for loc in locs)
            else:
                out["truncated"] = True
            continue
        for loc in locs:
            if loc not in seen_urls:
                seen_urls.add(loc)
                urls.append(loc)
                if len(urls) >= max_urls:
                    out["truncated"] = True
                    break
        if len(urls) >= max_urls:
            break
    out["urls"] = urls
    return out


# --- cache -------------------------------------------------------------------------------------------------------------

def _cache_path(cache, domain: str) -> Path:
    safe = re.sub(r"[^a-z0-9.\-]", "_", domain.lower())
    return Path(cache.root) / "sitemaps" / f"{safe}.json"


def cached_domain(cache, domain: str, crawl: Callable[[], dict], *, now: Callable[[], float] = time.time,
                  ttl_s: float = TTL_S, partial_ttl_s: float = PARTIAL_TTL_S,
                  hold_timeout: float | None = None) -> tuple[dict, bool]:
    """(record, cache_hit): the parsed URL list of a domain, shared by every vehicle of that importer for `ttl_s`
    (`partial_ttl_s` for a crawl the deadline cut short: `partial: true`). Single flight per domain (cache.hold); a
    wait longer than `hold_timeout` raises TimeoutError (the caller goes on without this domain). A crawl that found
    nothing at all is not cached."""
    from .storage.atomic import atomic_write_text

    path = _cache_path(cache, domain)

    def read() -> dict | None:
        try:
            record = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        ttl = partial_ttl_s if record.get("partial") else ttl_s
        if record.get("version") != SITE_MAP_VERSION or now() - float(record.get("stored_at") or 0) > ttl:
            return None
        return record

    record = read()
    if record is not None:
        return record, True
    with cache.hold(f"sitemap:{domain}", **({"timeout": hold_timeout} if hold_timeout is not None else {})):
        record = read()
        if record is not None:
            return record, True
        record = {**crawl(), "version": SITE_MAP_VERSION, "stored_at": now()}
        if record.get("urls") or record.get("sitemaps"):
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, json.dumps(record, ensure_ascii=False))
        return record, False


# --- ranking (pure) ------------------------------------------------------------------------------------------------------

def _path_tokens(url: str) -> list[str]:
    parsed = urlparse(url)
    text = unquote(parsed.path + " " + parsed.query).lower()
    return [t for t in re.split(r"[^\w]+", text) if t]


def _phrase_in(tokens: list[str], phrase: list[str]) -> bool:
    n = len(phrase)
    return n > 0 and any(tokens[i:i + n] == phrase for i in range(len(tokens) - n + 1))


def _phrases(names: Iterable[str]) -> list[list[str]]:
    out = []
    for name in names:
        phrase = [t for t in re.split(r"[^\w]+", str(name or "").lower()) if t]
        if phrase and phrase not in out:
            out.append(phrase)
    return out


def rank_urls(urls: Iterable[str], *, model_names: Iterable[str], year: Any = None, cluster: str | None = None,
              other_families: Iterable[str] = (), allowed: Callable[[str], bool] | None = None,
              exclude: Iterable[str] = (), limit: int = TOP_N) -> list[dict]:
    """The site's URLs ranked for acquisition: [{url, score, reasons}], top `limit`, only URLs with at least one
    reason. Model / family tokens (+5), model year (+2), intent keywords (+3 each, the cluster's own when given,
    brochure words always), .pdf (+2). A URL whose path names ANOTHER model family (one that is not part of the
    target's own names, e.g. "corolla cross" for a corolla) is dropped, as is one robots.txt disallows (`allowed`)
    or one already fetched (`exclude`). Deterministic: ties keep the site map's order."""
    model = _phrases(model_names)
    own = {" ".join(p) for p in model}
    others = [p for p in _phrases(other_families) if " ".join(p) not in own]
    intents = tuple(INTENTS.get(cluster or "", ())) + BROCHURE if cluster else ALL_INTENTS
    year = str(year or "").strip()
    skip = {str(u).split("#")[0].rstrip("/") for u in exclude}
    scored = []
    seen: set[str] = set()
    for index, url in enumerate(urls):
        url = str(url).strip()
        key = url.split("#")[0].rstrip("/")
        if not url.lower().startswith(("http://", "https://")) or key in seen or key in skip:
            continue
        seen.add(key)
        tokens = _path_tokens(url)
        if any(_phrase_in(tokens, p) for p in others) and not any(
                _phrase_in(tokens, p) and len(p) >= max(len(o) for o in others if _phrase_in(tokens, o))
                for p in model):
            continue
        if allowed is not None and not allowed(url):
            continue
        reasons, score = [], 0
        named = [" ".join(p) for p in model if _phrase_in(tokens, p)]
        if named:
            score += 5
            reasons.append(f"model:{named[0]}")
        if year and year in tokens:
            score += 2
            reasons.append(f"year:{year}")
        words = [w for w in intents if w in tokens or ("-" in w and _phrase_in(tokens, w.split("-")))]
        for word in dict.fromkeys(words):
            score += 3
            reasons.append(f"intent:{word}")
        if urlparse(url).path.lower().endswith(".pdf"):
            score += 2
            reasons.append("pdf")
        if score and (named or words):
            scored.append((-score, index, {"url": url, "score": score, "reasons": reasons}))
    return [item for _, _, item in sorted(scored, key=lambda x: (x[0], x[1]))[:max(0, int(limit))]]


def robots_allowed(robots_text: str, user_agent: str = "*") -> Callable[[str], bool]:
    parser = RobotFileParser()
    parser.parse((robots_text or "").splitlines())
    return lambda url: parser.can_fetch(user_agent, url)


# --- the run-level entry point -------------------------------------------------------------------------------------------

def target_names(adm, payload: dict | None, vehicle: dict | None) -> tuple[list[str], list[str], Any]:
    """(model names incl. Hebrew transliterations, other model families, model year) of the run's target, from the
    admission identity, the Level 1.5 record and the identity vocabulary (data/identity_vocabulary.json)."""
    from .document_binding import vocabulary

    vocab = vocabulary()
    families: dict = (vocab or {}).get("model_families") or {}
    identity = getattr(adm, "identity", None)
    family = str(getattr(identity, "family", None) or "").lower()
    record = (payload or {}).get("identity") or {}
    names = list(families.get(family) or []) + ([family] if family else [])
    for raw in (record.get("model"), (vehicle or {}).get("model")):
        if raw:
            names.append(str(raw).lower())
    names = list(dict.fromkeys(n for n in names if n))
    others = [variant for key, variants in families.items() if key != family for variant in [key] + list(variants)]
    year = getattr(identity, "year", None) or record.get("year") or (vehicle or {}).get("year")
    return names, others, year


def build_site_map(ctx, domains: list[str], *, deadline_s: float = DEFAULT_DEADLINE_S,
                   fetch: Callable[[str, float], tuple[int, bytes]] | None = None) -> dict:
    """{domains: [{domain, cache_hit, sitemaps, urls, truncated, errors}], urls: [(url, domain)], robots: {domain:
    text}, duration_ms}. Domains in the given order (importer first) until the deadline. Never raises."""
    t0 = time.monotonic()
    deadline = t0 + max(1.0, float(deadline_s))
    getter = fetch or (lambda url, dl: http_fetch(ctx, url, dl))
    out: dict[str, Any] = {"domains": [], "urls": [], "robots": {}, "errors": []}
    for domain in domains:
        domain = str(domain or "").strip().lower()
        if not domain:
            continue
        if time.monotonic() > deadline:
            out["domains"].append({"domain": domain, "skipped": "deadline"})
            continue
        try:
            record, hit = cached_domain(ctx.cache, domain, lambda: crawl_domain(
                domain, lambda u: getter(u, deadline), deadline=deadline),
                hold_timeout=max(0.0, deadline - time.monotonic()))
        except Exception as exc:  # noqa: BLE001 - discovery never costs the run (a timed-out single-flight wait too)
            out["domains"].append({"domain": domain, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            out["errors"].append(f"{domain}: {type(exc).__name__}")
            continue
        out["domains"].append({"domain": domain, "cache_hit": hit, "sitemaps": len(record.get("sitemaps") or []),
                               "urls": len(record.get("urls") or []), "truncated": bool(record.get("truncated")),
                               "partial": bool(record.get("partial")),
                               "errors": list(record.get("errors") or [])[:5]})
        out["robots"][domain] = record.get("robots") or ""
        out["urls"] += [(u, domain) for u in record.get("urls") or []]
    out["duration_ms"] = int((time.monotonic() - t0) * 1000)
    return out


def offer(site: dict, *, model_names: list[str], other_families: list[str], year: Any, cluster: str | None = None,
          exclude: Iterable[str] = (), limit: int = TOP_N) -> list[dict]:
    """The ranked offer over a built site map, robots.txt respected per domain."""
    from .tools.fetch import USER_AGENT

    checks = {d: robots_allowed(text, USER_AGENT) for d, text in (site.get("robots") or {}).items()}
    domain_of = {u: d for u, d in site.get("urls") or []}

    def allowed(url: str) -> bool:
        check = checks.get(domain_of.get(url, ""))
        return True if check is None else check(url)

    return rank_urls([u for u, _ in site.get("urls") or []], model_names=model_names, year=year, cluster=cluster,
                     other_families=other_families, allowed=allowed, exclude=exclude, limit=limit)


def acquisition_section(offered: list[dict]) -> str:
    """The acquisition message section listing the offered URLs (empty when there are none)."""
    if not offered:
        return ""
    lines = ["Known pages on the official site (from its sitemap; fetch these instead of guessing URLs):"]
    lines += [f"- {o['url']}" for o in offered]
    return "\n".join(lines)


def official_domains(ctx, target_market: str = "IL") -> list[str]:
    """The target's official domains (search.default_domains), target-market importer domains first."""
    from .acquisition import source_priority
    from .tools.search import default_domains

    adm = getattr(ctx, "admission", None)
    maker = getattr(adm, "manufacturer", None) or (ctx.vehicle or {}).get("manufacturer")
    domains = default_domains(ctx.vehicle or {})
    return sorted(domains, key=lambda d: source_priority(f"https://{d}/", maker, target_market)["priority_commercial"])


def run_site_map(ctx, run_log, *, payload: dict | None, vehicle: dict | None, target_market: str = "IL",
                 cluster: str | None = None, exclude: Iterable[str] = (), deadline_s: float = DEFAULT_DEADLINE_S,
                 site: dict | None = None, stage: str = "acquisition") -> dict:
    """Build (or reuse `site`) and rank the site map for one acquisition episode; log `site_map`. Never raises:
    a failure logs `site_map_failed` and offers nothing. Returns {site, offered, error}."""
    t0 = time.monotonic()
    reused = site is not None
    try:
        if site is None:
            site = build_site_map(ctx, official_domains(ctx, target_market), deadline_s=deadline_s)
        names, others, year = target_names(getattr(ctx, "admission", None), payload, vehicle)
        offered = offer(site, model_names=names, other_families=others, year=year, cluster=cluster, exclude=exclude)
    except Exception as exc:  # noqa: BLE001 - discovery never costs the run
        error = f"{type(exc).__name__}: {str(exc)[:300]}"
        try:
            run_log.event("site_map_failed", stage=stage, cluster=cluster, error=error)
        except Exception:  # noqa: BLE001
            pass
        return {"site": site, "offered": [], "error": error}
    domains = site.get("domains") or []
    try:
        run_log.event("site_map", stage=stage, cluster=cluster, version=SITE_MAP_VERSION,
                      domains=domains, sitemap_count=sum(d.get("sitemaps") or 0 for d in domains),
                      url_count=len(site.get("urls") or []), offered_urls=offered,
                      duration_ms=int((time.monotonic() - t0) * 1000), site_map_reused=reused,
                      cache_hit=reused or (bool(domains) and all(d.get("cache_hit") for d in domains
                                                                  if "cache_hit" in d)),
                      errors=list(site.get("errors") or []) + [e for d in domains for e in d.get("errors") or []][:10],
                      note="discovery metadata: sitemaps are not documents and never evidence")
    except Exception:  # noqa: BLE001
        pass
    return {"site": site, "offered": offered, "error": None}


def usage(offered: Iterable[dict], tool_calls: Iterable[dict], *, cache, adm, target_market: str,
          phase: str = "research") -> dict:
    """D4 telemetry: of the offered URLs, how many the phase fetched, how many became useful documents (usable, not
    bound to another variant) and how many target-market documents. Observational only; never raises."""
    from .field_recovery import is_target_market
    from .storage import trace
    from .tail_planner import document_profile_for, usable_document

    wanted = {str(o.get("url") or "").split("#")[0].rstrip("/") for o in offered or [] if o.get("url")}
    out = {"offered": len(wanted), "fetched": 0, "useful": 0, "target_market": 0}
    if not wanted:
        return out
    docs: dict[str, str | None] = {}
    for call in tool_calls or []:
        if call.get("phase") != phase or call.get("name") not in trace.FETCH_TOOLS or call.get("blocked"):
            continue
        url = str(trace.parse_args(call.get("arguments")).get("url") or "").split("#")[0].rstrip("/")
        if url in wanted:
            docs.setdefault(url, call.get("document_id"))
    out["fetched"] = len(docs)
    for doc in docs.values():
        try:
            if not doc or not usable_document(cache, doc):
                continue
            profile = document_profile_for(adm, cache, doc)
            if profile.get("variant_match") == "different":
                continue
            out["useful"] += 1
            if is_target_market(profile.get("market"), target_market):
                out["target_market"] += 1
        except Exception:  # noqa: BLE001
            continue
    return out
