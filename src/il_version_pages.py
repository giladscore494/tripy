"""Israeli version pages (PR #44, P2 / P4): which single-version publisher page describes the target, and the resolver
that finds one before the agentic acquisition.

Israeli publishers (data/source_rules.json `il_version_sites`: cartube.co.il, icar.co.il, auto.co.il) publish one page
per technical version: the title, H1 and URL slug name the version ("אודי Q3 2.0 40TFSI 4X4 2024"), its spec rows state
the engine ("נפח מנוע | 1,984 סמ"ק", "הספק | 190 כ"ס", "הנעה | 4X4"). Everything here is deterministic and fail-closed:

    page_identity(material, identity)     what ONE version page is about, read from its title / H1 / URL words and its
                                          identity spec rows (`identity_row_terms`; "label | value", label above value,
                                          "label / (unit) / description / value" blocks): years, displacements, powers,
                                          drivetrain / body / propulsion keys, version designations, the target trim
    version_page_verdict(material, ...)   for an Israeli publisher / aggregator page (or an il_version_sites domain) that
                                          names the target family:
                                              single_version  exactly one version (the page identity AND the document's
                                                              full text name at most one designation / displacement /
                                                              drivetrain / body, and a non-hybrid at most one power)
                                              accepted        single_version, and year, displacement (+-0.06 l),
                                                              drivetrain, propulsion and body match; the power matches
                                                              (+-3 %), or the catalog has exactly ONE complete entry for
                                                              (manufacturer, family, year, propulsion, drivetrain)
                                                              (P4.2: a hybrid's government power is the engine's; the
                                                              page states the system power)
                                          src/document_binding.bind reads the verdict: an accepted page binds every value
                                          at exact_technical_variant (binding_basis il_version_page); a single-version
                                          page's identity replaces the full-text statuses of the dimensions it names; a
                                          hybrid single-version page whose power is another one is system_power_unmapped
    resolve(ctx, run_log, ...)            the first acquisition wave: <= max_searches site-restricted searches, version
                                          links of the site's model / price-list pages, <= max_fetches candidate URLs,
                                          robots.txt respected, <= 1 request / min_interval_s per domain, the shared
                                          document cache; counters acq_il_version_searches / _fetches / _pages_found

Nothing here is evidence: an accepted page's values still pass the admission gate value by value.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

from .candidate_harvest import compile_terms, normalize_text
from .document_binding import (HYBRID_PROPULSIONS, TargetIdentity, catalog_family_entries, designations,
                               dimension_status, distinct_powers, mentions, merge_year_contexts, normalize_catalog_trim,
                               vocabulary, year_context, zone_names_target, zone_year_context)
from .source_authority import host_of, rules

VERSION_PAGE_VERSION = "il-version-page-v1"
IL_AUTHORITIES = ("publisher", "aggregator")
DIMENSIONS = ("year", "body", "propulsion", "displacement", "power", "drivetrain")
POWER_TOLERANCE = 0.03
UNIT_LINE = re.compile(r"\(\s*[^()\d]{1,25}\s*\)")
LABEL_MAX, VALUE_MAX, DESCRIPTION_MIN, LOOKAHEAD = 40, 40, 40, 5
_TERMS: dict[str, Any] = {}


def settings() -> dict:
    """data/source_rules.json `il_version_sites` (sites, templates, budgets, identity row terms)."""
    return rules().get("il_version_sites") or {}


def site_domains() -> list[str]:
    return [str(s.get("domain") or "").lower() for s in settings().get("sites") or [] if s.get("domain")]


def site_of(url: str | None) -> str | None:
    host = host_of(url)
    return next((d for d in site_domains() if host == d or host.endswith("." + d)), None)


def _row_terms():
    terms = settings().get("identity_row_terms") or []
    key = "|".join(terms)
    if key not in _TERMS:
        _TERMS.clear()
        _TERMS[key] = compile_terms(terms)
    return _TERMS[key]


def _with_unit(label: str, unit: str | None, value: str) -> str:
    """'נפח מנוע' (סמ"ק) 2995 -> "נפח מנוע 2995 סמ"ק": a bare number takes the unit its label states."""
    m = re.search(r"\(\s*([^()\d]{1,25}?)\s*\)\s*$", f"{label} {unit or ''}".strip())
    if m and re.fullmatch(r"\s*\d{1,3}(?:,\d{3})*(?:[.,]\d+)?\s*", value):
        return f"{label} {value.strip()} {m.group(1)}"
    return f"{label} {value}"


def identity_rows(text: str) -> list[str]:
    """The identity spec rows of a page text ("label value [unit]"), in page order. A row is a short line naming an
    identity_row_terms label with its value on the same line ("label | value", "label: value") or below it (after an
    optional unit line, a repetition of the label and description prose)."""
    pattern = _row_terms()
    if pattern is None:
        return []
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    norms = [normalize_text(ln) for ln in lines]
    out: list[str] = []
    for i, norm in enumerate(norms):
        if len(norm) > 2 * LABEL_MAX or not pattern.search(norm):
            continue
        split = re.split(r"\s*[|:]\s*", lines[i], maxsplit=1)
        if len(split) == 2 and split[1].strip() and len(norm) <= LABEL_MAX + VALUE_MAX:
            out.append(_with_unit(split[0], None, split[1]))
            continue
        if len(norm) > LABEL_MAX:
            continue
        unit, j = None, i + 1
        while j < len(norms) and j <= i + LOOKAHEAD:
            if UNIT_LINE.fullmatch(norms[j]) and unit is None:
                unit = lines[j]
            elif norms[j] == norm or len(norms[j]) >= DESCRIPTION_MIN:
                pass
            else:
                break
            j += 1
        if j < len(norms) and len(norms[j]) <= VALUE_MAX and not pattern.fullmatch(norms[j]):
            out.append(_with_unit(lines[i], unit, lines[j]))
    return out


def _url_words(url: str | None) -> str:
    path = unquote(urlparse(str(url or "")).path)
    return re.sub(r"[/_\-]+", " ", path)


def page_identity(material, identity: TargetIdentity) -> dict:
    """What a page is about, from its title, H1s, URL slug and identity spec rows (see the module doc). Pure."""
    doc = material.doc
    title = str(doc.meta.get("title") or "")
    head = "\n".join([title, *(doc.headings or []), _url_words(doc.url)])
    rows = identity_rows(doc.body_text if doc.body_text is not None else doc.text)
    text = head + "\n" + "\n".join(rows)
    found = mentions(text, identity)
    # the whole title of a version page is one identity segment ("cartube - מחירון רכב ומפרט טכני | 2024 | אודי Q3 2.0
    # 40TFSI 4X4": the 2024 is the version's model year); dates, URLs and copyrights stay ignored (year_context)
    whole_title = year_context(title, identity, segment=True) if zone_names_target(normalize_text(title), identity) \
        else {"years": set(), "statements": [], "ignored": []}
    years = merge_year_contexts(zone_year_context(title=title, url=doc.url, identity=identity, headings=doc.headings,
                                                  text=doc.text),
                                year_context("\n".join(rows), identity), whole_title)
    found["year"] = set(years["years"])
    trim_named = False
    if identity.trim_words:
        from .variant_map import catalog_trim_matches

        own = normalize_catalog_trim(" ".join(identity.trim_words))
        trim_named = bool(catalog_trim_matches(head, [own], identity)) or found["trim"] == "match"
    return {"rows": rows[:30], "found": found, "statuses": {d: dimension_status(d, found[d], identity) for d in DIMENSIONS},
            "trim_named": trim_named}


def _versions(page: dict, profile: dict, identity: TargetIdentity) -> dict:
    """How many versions the page names, per identity part (the page identity and the document's full text)."""
    found = page["found"]
    full = profile.get("mentions") or {}
    hybrid = identity.propulsion in HYBRID_PROPULSIONS
    counts = {
        "designation": len(set(found["designation"]) | set((profile.get("powertrain_versions") or {})
                                                           .get("designations") or [])),
        "displacement": len(set(found["displacement"]) | set(full.get("displacement") or [])),
        "drivetrain": len(found["drivetrain"]),
        "body": len(found["body"]),
        "power": 0 if hybrid else len(distinct_powers(set(found["power"])
                                                      | set((profile.get("powertrain_versions") or {})
                                                            .get("powers") or []))),
    }
    return counts


def single_catalog_entry(identity: TargetIdentity, index: dict | None = None) -> dict | None:
    """{key, records} when the catalog has exactly ONE entry for the target's (manufacturer, family, year, propulsion,
    drivetrain), complete, and it is the target's own; None otherwise (an entry whose propulsion or drivetrain the
    catalog left unknown could be a sibling: never single)."""
    if not identity.propulsion or not identity.drivetrain:
        return None
    same = [e for e in catalog_family_entries(identity, index)
            if e["parts"]["propulsion"] in (identity.propulsion, "") and e["parts"]["drivetrain"] in (identity.drivetrain, "")]
    if len(same) != 1 or not same[0]["complete"]:
        return None
    parts = same[0]["parts"]
    if parts["propulsion"] != identity.propulsion or parts["drivetrain"] != identity.drivetrain:
        return None
    return {"key": same[0]["key"], "records": same[0]["records"][:20]}


def _power_match(found: Iterable[float], target: float | None) -> str:
    powers = list(found or [])
    if not powers or target is None:
        return "absent"
    hits = [p for p in powers if abs(p - target) <= max(0.051, POWER_TOLERANCE * target)]
    return "match" if hits and len(hits) == len(powers) else "mixed" if hits else "mismatch"


def version_page_verdict(material, identity: TargetIdentity, index: dict | None = None) -> dict | None:
    """The page's version-page verdict (module doc), or None when the page is not an Israeli publisher / aggregator /
    il_version_sites page naming the target family. Pure and deterministic."""
    doc = material.doc
    authority = (material.authority or {}).get("source_authority")
    site = site_of(doc.url)
    if doc.market != "IL" or not (site or authority in IL_AUTHORITIES):
        return None
    head = "\n".join([str(doc.meta.get("title") or ""), *(doc.headings or []), _url_words(doc.url)])
    if not zone_names_target(normalize_text(head), identity):
        return None
    page = page_identity(material, identity)
    counts = _versions(page, material.profile, identity)
    single = all(n <= 1 for n in counts.values())
    statuses = dict(page["statuses"])
    power = _power_match(page["found"]["power"], identity.power_hp)
    statuses["power"] = power
    out = {"version": VERSION_PAGE_VERSION, "site": site, "single_version": single, "version_counts": counts,
           "page_statuses": statuses, "trim_named": page["trim_named"], "rows": page["rows"][:12],
           "page_powers": sorted(page["found"]["power"])}
    if not single:
        return {**out, "status": "rejected", "reason": "several_versions"}
    needed = ["year", "body", "propulsion", "drivetrain"]
    if identity.propulsion != "battery_electric":
        needed.append("displacement")
    if identity.propulsion == "conventional" and statuses.get("propulsion") == "absent":
        statuses["propulsion"] = "match"   # a combustion page names no hybrid / electric term (as binding's ladder reads it)
    bad = [d for d in needed if statuses.get(d) != "match"]
    if bad:
        return {**out, "status": "rejected", "reason": f"{bad[0]}_{statuses.get(bad[0])}"}
    catalog = single_catalog_entry(identity, index)
    if catalog is not None:
        out["catalog_single_entry"] = catalog["key"]
    if power == "match":
        return {**out, "status": "accepted", "reason": "power_match"}
    if catalog is not None and (identity.propulsion in HYBRID_PROPULSIONS or power == "absent"):
        return {**out, "status": "accepted", "reason": "single_catalog_entry"}
    return {**out, "status": "rejected", "reason": f"power_{power}"}


# --- the resolver (first acquisition wave) ------------------------------------------------------------------------------

class DomainPacer:
    """At most one request per `interval` seconds per domain (shared by every vehicle of the process)."""

    def __init__(self, interval: float, clock=time.monotonic, sleep=time.sleep):
        self.interval, self.clock, self.sleep = float(interval), clock, sleep
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, domain: str) -> float:
        with self._lock:
            now = self.clock()
            at = max(now, self._next.get(domain, now))
            self._next[domain] = at + self.interval
        delay = at - now
        if delay > 0:
            self.sleep(delay)
        return delay


_PACER: DomainPacer | None = None


def pacer() -> DomainPacer:
    global _PACER
    interval = float(settings().get("min_interval_s") or 1.0)
    if _PACER is None or _PACER.interval != interval:
        _PACER = DomainPacer(interval)
    return _PACER


def target_terms(payload: dict | None, identity: TargetIdentity) -> dict:
    """Placeholders of the search templates from the Level 1.5 record, the catalog identity and the vocabularies."""
    vocab = vocabulary()
    record = (payload or {}).get("identity") or {}
    makers = (vocab.get("manufacturers") or {}).get(str(identity.manufacturer or ""), [])
    make_he = next((m for m in makers if re.search(r"[א-ת]", m) and m != identity.manufacturer), None) \
        or identity.manufacturer or ""
    make_en = next((m for m in makers if re.fullmatch(r"[a-z0-9 \-]+", m)), "")
    model = str(record.get("commercial_name") or identity.family or "")
    engine_l = f"{identity.displacement_l:.1f}" if identity.displacement_l else ""
    hp = str(int(identity.power_hp)) if identity.power_hp and identity.propulsion not in HYBRID_PROPULSIONS else ""
    return {"make_he": make_he, "make_en": make_en, "model": model, "year": str(identity.year or ""),
            "engine_l": engine_l, "hp": hp}


def search_queries(terms: dict, limit: int | None = None) -> list[tuple[str, str]]:
    """(domain, query) in template-major order (the first template on every site first), at most `limit`."""
    cfg = settings()
    limit = int(cfg.get("max_searches") or 3) if limit is None else limit
    out = []
    for template in cfg.get("search_templates") or []:
        for domain in site_domains():
            query = template
            for key, value in {"domain": domain, **terms}.items():
                query = query.replace("{" + key + "}", str(value or ""))
            query = re.sub(r"\s+", " ", query).strip()
            if (domain, query) not in out:
                out.append((domain, query))
    return out[:max(0, limit)]


def candidate_links(links: Iterable[dict], page_url: str, terms: dict, identity: TargetIdentity) -> list[str]:
    """Version-page links of a site's model / price-list page: same site, the model named in the URL or anchor, and a
    version path hint or the model year; ranked by acquisition.rank_links."""
    from .acquisition import model_tokens, rank_links

    site = site_of(page_url)
    if site is None:
        return []
    hints = next((s.get("version_path_hints") or [] for s in settings().get("sites") or [] if s.get("domain") == site),
                 [])
    model = normalize_text(str(terms.get("model") or identity.family or ""))
    year = str(identity.year or "")
    ranked = rank_links(links, page_url, model_tokens(terms.get("model"), identity.family, identity.year), limit=40)
    # the target's own engine / power / drivetrain words in the link first ("2-0", "190", "4x4"), page order otherwise
    wanted = [t for t in (terms.get("engine_l"), str(terms.get("engine_l") or "").replace(".", "-"), terms.get("hp"))
              if t]
    wanted += list((vocabulary().get("drivetrain_terms") or {}).get(identity.drivetrain or "", []))
    wanted = [normalize_text(t) for t in wanted]

    def score(link: dict) -> int:
        hay = normalize_text(unquote(link["url"]) + " " + str(link.get("text") or ""))
        return -sum(1 for t in wanted if re.search(rf"(?<![\w.]){re.escape(t)}(?![\w])", hay))

    ranked = sorted(ranked, key=score)
    out = []
    for link in ranked:
        url = link["url"]
        if site_of(url) != site:
            continue
        hay = normalize_text(unquote(url) + " " + str(link.get("text") or ""))
        if model and not re.search(rf"(?<![\w]){re.escape(model)}(?![\w])", hay):
            continue
        if not (any(normalize_text(h) in hay for h in hints) or (year and year in hay)):
            continue
        out.append(url)
    return out


def _search_urls(result: Any) -> list[str]:
    items = result.get("results") if isinstance(result, dict) else None
    return [str(i.get("url")) for i in items or [] if isinstance(i, dict) and i.get("url")]


def resolve(ctx, run_log=None, *, payload: dict | None = None, search=None, fetch=None, robots=None,
            pace: DomainPacer | None = None) -> dict:
    """The first acquisition wave (module doc). `search(query, domain) -> {results: [{url}]}`, `fetch(url) -> {document_id,
    status, ...}` and `robots(url) -> bool` default to the run's tools; tests pass fakes. Never raises; returns
    {pages: [{url, document_id, verdict}], searches, fetches, found, skipped: [...]} and sets the run counters."""
    cfg = settings()
    adm = getattr(ctx, "admission", None)
    identity = getattr(adm, "identity", None)
    out: dict[str, Any] = {"version": VERSION_PAGE_VERSION, "searches": 0, "fetches": 0, "found": 0, "pages": [],
                           "queries": [], "skipped": []}
    if identity is None or not site_domains():
        return out
    max_searches, max_fetches = int(cfg.get("max_searches") or 3), int(cfg.get("max_fetches") or 6)
    pace = pace or pacer()
    search = search or (lambda q, d: _default_search(ctx, q, d))
    fetch = fetch or (lambda u: _default_fetch(ctx, u))
    robots = robots or (lambda u: _default_robots(ctx, u))
    terms = target_terms(payload, identity)
    queue: list[str] = []
    seen: set[str] = set()
    try:
        for domain, query in search_queries(terms, max_searches):
            out["searches"] += 1
            out["queries"].append(query)
            try:
                result = search(query, domain)
            except Exception as exc:  # noqa: BLE001 - a failed search costs only its results
                out["skipped"].append({"query": query, "error": f"{type(exc).__name__}"})
                continue
            for url in _search_urls(result):
                if site_of(url) == domain and url.split("#")[0] not in seen:
                    seen.add(url.split("#")[0])
                    queue.append(url.split("#")[0])
        while queue and out["fetches"] < max_fetches:
            url = queue.pop(0)
            domain = site_of(url)
            if domain is None:
                continue
            if not robots(url):
                out["skipped"].append({"url": url, "reason": "robots_disallow"})
                continue
            pace.wait(domain)
            out["fetches"] += 1
            try:
                result = fetch(url) or {}
            except Exception as exc:  # noqa: BLE001
                out["skipped"].append({"url": url, "error": type(exc).__name__})
                continue
            doc = result.get("document_id")
            status = result.get("status")
            if not doc or result.get("error") or (isinstance(status, int) and not 200 <= status < 300):
                out["skipped"].append({"url": url, "reason": result.get("error") or f"status_{status}"})
                continue
            material = adm.material(ctx.cache, doc, None, [doc])
            verdict = getattr(material, "version_page", None) if material is not None else None
            page = {"url": url, "document_id": doc, "status": (verdict or {}).get("status") or "not_a_version_page",
                    "reason": (verdict or {}).get("reason")}
            out["pages"].append(page)
            if verdict and verdict.get("status") == "accepted":
                out["found"] += 1
                break
            if verdict is None or not verdict.get("single_version"):
                # a model / price-list page of the site: follow its version links (never past the fetch budget)
                links = [link for link in candidate_links(_links(ctx, doc), url, terms, identity) if link not in seen]
                seen.update(links)
                queue[0:0] = links
    except Exception as exc:  # noqa: BLE001 - the resolver never costs the run
        out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    counters = getattr(ctx, "counters", None)
    if counters is not None:
        counters["acq_il_version_searches"] += out["searches"]
        counters["acq_il_version_fetches"] += out["fetches"]
        counters["acq_il_version_pages_found"] += out["found"]
    if run_log is not None:
        try:
            run_log.event("il_version_pages", **out)
        except Exception:  # noqa: BLE001
            pass
    return out


def _links(ctx, doc: str) -> list[dict]:
    from .tools.extract import html_links

    meta = ctx.cache.get(doc) or {}
    if meta.get("doc_type") != "html":
        return []
    try:
        return html_links(ctx.cache.read_body(doc).decode("utf-8", errors="replace"),
                          meta.get("final_url") or meta.get("url") or "")
    except Exception:  # noqa: BLE001
        return []


def _default_search(ctx, query: str, domain: str | None) -> Any:
    """A domain-filtered search: the template's "site:<domain>" becomes the search backend's own domain filter."""
    from .tools.search import search_web

    return search_web(ctx, re.sub(r"\bsite:\S+\s*", "", query).strip(), domain=domain)


def _default_fetch(ctx, url: str) -> dict:
    from .tools.fetch import fetch_url

    return fetch_url(ctx, url)


_ROBOTS: dict[str, Any] = {}
_ROBOTS_LOCK = threading.Lock()


def _default_robots(ctx, url: str) -> bool:
    """robots.txt of the URL's host (fetched once per process and host); an unreachable robots.txt allows."""
    from .site_map import robots_allowed
    from .tools.fetch import USER_AGENT

    parsed = urlparse(url)
    host = f"{parsed.scheme or 'https'}://{parsed.netloc}"
    with _ROBOTS_LOCK:
        check = _ROBOTS.get(host)
    if check is None:
        try:
            resp = ctx.session.get(host + "/robots.txt", headers={"User-Agent": USER_AGENT},
                                   timeout=(ctx.config.connect_timeout_s, ctx.config.read_timeout_s))
            text = resp.text if resp.status_code == 200 else ""
        except Exception:  # noqa: BLE001
            text = ""
        check = robots_allowed(text, USER_AGENT)
        with _ROBOTS_LOCK:
            _ROBOTS[host] = check
    return bool(check(url))
