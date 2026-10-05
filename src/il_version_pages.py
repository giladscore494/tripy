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

PR #45 (il-version-page-v2), deterministic and fail-closed:

    R1  candidates      only a URL matching the site's `version_url_patterns` (data/source_rules.json) is a candidate,
                        ranked by slug_score (engine litres, power, designation, drivetrain, year; a contradicting litres
                        figure, drivetrain or body sub-variant ranks it down); a site's home page is never fetched; any
                        other page of the site (a listing) is fetched after the version candidates, only for its version
                        links. A `site:` search with nothing useful is retried once with the backend's domain filter (no
                        `site:` prefix) inside max_searches. The il_version_pages event logs every search result (url,
                        title, rank, decision, reason) and every candidate decision (fetched, robots, budget, ...)
    R2  titles          the title / H1 are read HTML-decoded (a site that escaped "&#x27;" twice), "TFSI 45" is the
                        designation 45tfsi, a bare "45" title segment a designation number, and a spec value written
                        without a thousands separator ("1984") takes its label's unit
    R3  sub-variants    a body sub-variant (Sportback, Avant, ...) in the title / H1 / URL the target's names lack, or the
                        target's own sub-variant missing there, is a body mismatch (document_binding.subvariant_status)
    R5  inconsistent    a single-version combustion page whose one displacement and one power contradict each other
                        against the catalog is rejected `page_inconsistent`; bind keeps its values <= body_powertrain

Nothing here is evidence: an accepted page's values still pass the admission gate value by value.
"""

from __future__ import annotations

import html
import json
import re
import threading
import time
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

from .candidate_harvest import compile_terms, normalize_text
from .document_binding import (HYBRID_PROPULSIONS, TargetIdentity, body_subvariants, catalog_family_entries,
                               designations, dimension_status, distinct_powers, mentions, merge_year_contexts,
                               normalize_catalog_trim, subvariant_status, vocabulary, year_context, zone_names_target,
                               zone_year_context)
from .source_authority import host_of, rules

VERSION_PAGE_VERSION = "il-version-page-v2"
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
    """'נפח מנוע' (סמ"ק) 2995 -> "נפח מנוע 2995 סמ"ק": a bare number takes the unit its label states (with or without a
    thousands separator: "1984" as "1,984"; PR #45 R2: the A4 530178 page's "1984" lost its unit)."""
    m = re.search(r"\(\s*([^()\d]{1,25}?)\s*\)\s*$", f"{label} {unit or ''}".strip())
    if m and re.fullmatch(r"\s*(?:\d{1,3}(?:,\d{3})+|\d+)(?:[.,]\d+)?\s*", value):
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
            elif norms[j] == norm or len(norms[j]) >= DESCRIPTION_MIN \
                    or (norms[j].startswith(norm) and UNIT_LINE.fullmatch(norms[j][len(norm):].strip())):
                pass                            # the label repeated (with its unit), or its description prose
            else:
                break
            j += 1
        if j < len(norms) and len(norms[j]) <= VALUE_MAX and not pattern.fullmatch(norms[j]):
            out.append(_with_unit(lines[i], unit, lines[j]))
    return out


def _url_words(url: str | None) -> str:
    path = unquote(urlparse(str(url or "")).path)
    return re.sub(r"[/_\-]+", " ", path)


def unescape(text: Any) -> str:
    """A title / heading as the page shows it: HTML entities decoded, also when the site escaped them twice
    ("2.0 ל&#x27; טורבו" -> "2.0 ל' טורבו", PR #45 R2)."""
    out = str(text or "")
    for _ in range(2):
        decoded = html.unescape(out)
        if decoded == out:
            break
        out = decoded
    return out


def page_head(doc) -> str:
    """The identity zone of a version page: its (decoded) title, H1s and URL words."""
    return "\n".join([unescape(doc.meta.get("title")), *(unescape(h) for h in doc.headings or []),
                      _url_words(doc.url)])


# a version title's bare designation number ("45, אוט', 2.0 ל' טורבו, S-Line, 4x4"; "אודי A4 2020 45, ...")
BARE_DESIGNATION = re.compile(r"^(?:.*(?<!\d)(?:19|20)\d{2}\s+)?([2-9]\d)$")


def title_designations(title: str, headings: Iterable[str] = ()) -> set[str]:
    """R2: the bare designation numbers of a version title / H1: a comma-separated segment that is just a two-digit
    number, or ends with one right after the model year ("אודי A4 2020 45")."""
    out: set[str] = set()
    for text in [title, *headings]:
        for segment in re.split(r"\s*[,|]\s*", unescape(text)):
            m = BARE_DESIGNATION.match(segment.strip())
            if m:
                out.add(m.group(1))
    return out


def designation_count(names: Iterable[str]) -> int:
    """Distinct designations, a bare number ("45") counted only when no designation of that number ("45tfsi") is named."""
    names = set(names or [])
    full = {n for n in names if not n.isdigit()}
    bare = {n for n in names if n.isdigit() and not any(f.startswith(n) and not f[len(n):len(n) + 1].isdigit()
                                                         for f in full)}
    return len(full) + len(bare)


def page_identity(material, identity: TargetIdentity) -> dict:
    """What a page is about, from its title, H1s, URL slug and identity spec rows (see the module doc). Pure."""
    doc = material.doc
    title = unescape(doc.meta.get("title"))
    headings = [unescape(h) for h in doc.headings or []]
    head = page_head(doc)
    rows = identity_rows(doc.body_text if doc.body_text is not None else doc.text)
    text = head + "\n" + "\n".join(rows)
    found = mentions(text, identity)
    found["designation"] = set(found["designation"]) | title_designations(title, headings)
    # the whole title of a version page is one identity segment ("cartube - מחירון רכב ומפרט טכני | 2024 | אודי Q3 2.0
    # 40TFSI 4X4": the 2024 is the version's model year); dates, URLs and copyrights stay ignored (year_context)
    whole_title = year_context(title, identity, segment=True) if zone_names_target(normalize_text(title), identity) \
        else {"years": set(), "statements": [], "ignored": []}
    years = merge_year_contexts(zone_year_context(title=title, url=doc.url, identity=identity, headings=headings,
                                                  text=doc.text),
                                year_context("\n".join(rows), identity), whole_title)
    found["year"] = set(years["years"])
    trim_named = False
    if identity.trim_words:
        from .variant_map import catalog_trim_matches

        own = normalize_catalog_trim(" ".join(identity.trim_words))
        trim_named = bool(catalog_trim_matches(head, [own], identity)) or found["trim"] == "match"
    statuses = {d: dimension_status(d, found[d], identity) for d in DIMENSIONS}
    # R3 (PR #45): a body sub-variant is identity: the title / H1 / URL names one the target's names lack, or the
    # target's names contain one the page does not name (Q3 Sportback page for a Q3, A1 page for an A1 SPORTBACK)
    subvariant = subvariant_status(head, identity, reverse=True)
    if subvariant and subvariant["status"] == "mismatch":
        statuses["body"] = "mismatch"
    return {"rows": rows[:30], "found": found, "statuses": statuses, "trim_named": trim_named,
            "subvariant": subvariant}


def _versions(page: dict, profile: dict, identity: TargetIdentity) -> dict:
    """How many versions the page names, per identity part (the page identity and the document's full text)."""
    found = page["found"]
    full = profile.get("mentions") or {}
    hybrid = identity.propulsion in HYBRID_PROPULSIONS
    counts = {
        "designation": designation_count(set(found["designation"]) | set((profile.get("powertrain_versions") or {})
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


def page_inconsistency(page: dict, identity: TargetIdentity, index: dict | None = None) -> dict | None:
    """R5: {displacement_l, power_hp, catalog_powers} when a combustion page states ONE displacement and ONE power that
    contradict each other against the catalog (every catalog entry of the target's manufacturer / family / model year
    with that displacement states a power, and the page's power lies outside their range by more than the power
    tolerance: 1984 cc next to 341 hp while every 2.0 l entry is <= 265 hp); None otherwise (no entry at that
    displacement, a hybrid / electric target or page, more than one displacement or power)."""
    found = page["found"]
    if identity.propulsion != "conventional" or {k.removeprefix("weak:") for k in found.get("propulsion") or []} \
            - {"conventional"}:
        return None
    litres, powers = sorted(found.get("displacement") or []), distinct_powers(found.get("power") or [])
    if len(litres) != 1 or len(powers) != 1:
        return None
    same = [e["parts"]["power"] for e in catalog_family_entries(identity, index)
            if e["parts"].get("displacement_l") not in (None, "")
            and abs(float(e["parts"]["displacement_l"]) - litres[0]) <= 0.06]
    if not same or any(p is None for p in same):
        return None
    low, high, power = min(same), max(same), powers[0]
    if (1 - POWER_TOLERANCE) * low <= power <= (1 + POWER_TOLERANCE) * high:
        return None
    return {"displacement_l": litres[0], "power_hp": power, "catalog_powers": sorted(set(same))}


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
    head = page_head(doc)
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
           "page_powers": sorted(page["found"]["power"]),
           "page_identity": {"displacements": sorted(page["found"]["displacement"]),
                             "designations": sorted(page["found"]["designation"]),
                             "drivetrains": sorted(page["found"]["drivetrain"])}}
    if page.get("subvariant"):
        out["body_subvariant"] = page["subvariant"]
    if not single:
        return {**out, "status": "rejected", "reason": "several_versions"}
    inconsistent = page_inconsistency(page, identity, index)
    if inconsistent is not None:
        # R5: the page mixes two versions (its displacement row and its power row disagree): never above body_powertrain
        return {**out, "status": "rejected", "reason": "page_inconsistent", "page_inconsistent": inconsistent}
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


def _site_config(site: str | None) -> dict:
    return next((dict(s) for s in settings().get("sites") or [] if s.get("domain") == site), {})


_URL_PATTERNS: dict[str, Any] = {}


def _version_patterns(site: str | None) -> list:
    """The site's version-page URL patterns (`version_url_patterns`, over the decoded, lower-case URL path)."""
    patterns = _site_config(site).get("version_url_patterns") or []
    key = json.dumps(patterns, ensure_ascii=False)
    if key not in _URL_PATTERNS:
        compiled = []
        for pattern in patterns:
            try:
                compiled.append(re.compile(pattern))
            except re.error:
                continue
        _URL_PATTERNS[key] = compiled
    return _URL_PATTERNS[key]


def url_path(url: str | None) -> str:
    return unquote(urlparse(str(url or "")).path).lower()


def classify_url(url: str | None) -> tuple[str | None, str]:
    """(site, kind) of a result URL: kind `version` (a version_url_patterns match), `home` (the site's root), `listing`
    (any other page of the site) or `off_site`. R1: only `version` URLs are candidates; a listing only lends links."""
    site = site_of(url)
    if site is None:
        return None, "off_site"
    path = url_path(url)
    if path.strip("/") in ("", "index.html", "index.php", "he", "he-il"):
        return site, "home"
    if any(p.search(path) for p in _version_patterns(site)):
        return site, "version"
    return site, "listing"


def _payload_designations(payload: dict | None) -> set[str]:
    record = (payload or {}).get("identity") or {}
    text = " ".join(str(record.get(k) or "") for k in ("commercial_name", "trim", "model_code"))
    return designations(normalize_text(text))


def slug_score(url: str, title: str | None, terms: dict, identity: TargetIdentity,
               payload: dict | None = None) -> dict:
    """R1: how well a candidate URL (its decoded path) and its result title name the target: +1 per target engine
    litres / power / designation / drivetrain / model-year token, -2 per contradicting one (another litres figure,
    another drivetrain, a body sub-variant the target's names lack). {score, matched, contradicted}."""
    path = url_path(url)
    last = path.rstrip("/").rsplit("/", 1)[-1]
    hay = normalize_text(f"{path} {title or ''}")
    slug = normalize_text(f"{last} {title or ''}")
    matched, contradicted = [], []
    engine = str(terms.get("engine_l") or "")
    litres = {f"{a}.{b}" for a, b in re.findall(r"(?<![\w.])(\d)[.\-](\d)(?![\w])", slug)}
    if engine and engine in litres:
        matched.append(f"engine:{engine}")
    if engine and litres - {engine}:
        contradicted.append(f"engine:{sorted(litres - {engine})[0]}")
    hp = str(terms.get("hp") or "")
    if hp and re.search(rf"(?<!\d){re.escape(hp)}(?!\d)", slug):
        matched.append(f"hp:{hp}")
    named = designations(slug)
    own = _payload_designations(payload)
    if own and named & own:
        matched.append(f"designation:{sorted(named & own)[0]}")
    drive = vocabulary().get("drivetrain_terms") or {}

    def names(key: str) -> bool:
        return any(re.search(rf"(?<![\w]){re.escape(normalize_text(t))}(?![\w])", hay) for t in drive.get(key) or [])
    if identity.drivetrain and names(identity.drivetrain):
        matched.append(f"drivetrain:{identity.drivetrain}")
    if any(names(k) for k in drive if k != identity.drivetrain) and identity.drivetrain:
        contradicted.append("drivetrain")
    if identity.year and re.search(rf"(?<!\d){identity.year}(?!\d)", slug):
        matched.append(f"year:{identity.year}")
    extra = body_subvariants(hay) - set(identity.body_subvariants or [])
    if extra:
        contradicted.append(f"body_subvariant:{sorted(extra)[0]}")
    return {"score": len(matched) - 2 * len(contradicted), "matched": matched, "contradicted": contradicted}


def candidate_links(links: Iterable[dict], page_url: str, terms: dict, identity: TargetIdentity,
                    payload: dict | None = None) -> list[str]:
    """Version-page links of a site's model / price-list page: same site, a version_url_patterns path, the model named
    in the URL or anchor; ranked by slug_score (page order on ties)."""
    site = site_of(page_url)
    if site is None:
        return []
    model = normalize_text(str(terms.get("model") or identity.family or ""))
    out = []
    for order, link in enumerate(links or []):
        url = str(link.get("url") or "").split("#")[0]
        if not url or classify_url(url) != (site, "version"):
            continue
        hay = normalize_text(unquote(url) + " " + str(link.get("text") or ""))
        if model and not re.search(rf"(?<![\w]){re.escape(model)}(?![\w])", hay):
            continue
        score = slug_score(url, link.get("text"), terms, identity, payload)["score"]
        out.append((-score, order, url))
    return list(dict.fromkeys(url for _, _, url in sorted(out)))


def _search_items(result: Any) -> list[dict]:
    items = result.get("results") if isinstance(result, dict) else None
    return [i for i in items or [] if isinstance(i, dict)]


def _strip_site(query: str) -> str:
    return re.sub(r"\bsite:\S+\s*", "", query).strip()


def resolve(ctx, run_log=None, *, payload: dict | None = None, search=None, fetch=None, robots=None,
            pace: DomainPacer | None = None) -> dict:
    """The first acquisition wave (module doc). `search(query, domain) -> {results: [{url, title}]}` (a query with
    "site:" is sent as written, without the domain filter; a query without it uses the domain filter), `fetch(url) ->
    {document_id, status, ...}` and `robots(url) -> bool` default to the run's tools; tests pass fakes. Never raises;
    returns {pages: [{url, role, document_id, status, reason}], results: [every search result and its decision],
    candidates: [every candidate URL and what happened to it], searches, fetches, found, skipped} and sets the run
    counters.

    R1 (PR #45): a result URL matching the site's version_url_patterns is a candidate, ranked by slug_score; a home page
    is never fetched; another page of the site (a listing) is fetched only after the version candidates and only to
    collect version links (it is never a candidate itself); a `site:` search that yields nothing useful is retried
    once with the search backend's domain filter and no `site:` prefix, inside the same max_searches budget."""
    cfg = settings()
    adm = getattr(ctx, "admission", None)
    identity = getattr(adm, "identity", None)
    out: dict[str, Any] = {"version": VERSION_PAGE_VERSION, "searches": 0, "fetches": 0, "found": 0, "pages": [],
                           "queries": [], "results": [], "candidates": [], "skipped": []}
    if identity is None or not site_domains():
        return out
    max_searches, max_fetches = int(cfg.get("max_searches") or 3), int(cfg.get("max_fetches") or 6)
    pace = pace or pacer()
    search = search or (lambda q, d: _default_search(ctx, q, d))
    fetch = fetch or (lambda u: _default_fetch(ctx, u))
    robots = robots or (lambda u: _default_robots(ctx, u))
    terms = target_terms(payload, identity)
    model = normalize_text(str(terms.get("model") or identity.family or ""))
    versions: list[dict] = []          # candidates: {url, score, order, source}
    listings: list[dict] = []          # pages fetched only for their version links
    seen: set[str] = set()

    def names_model(text: str) -> bool:
        return not model or bool(re.search(rf"(?<![\w]){re.escape(model)}(?![\w])", normalize_text(text)))

    def triage(items: list[dict], domain: str, query: str, mode: str) -> bool:
        """Records every result with its decision; True when one is useful (a version URL or a model listing)."""
        useful = False
        for rank, item in enumerate(items, start=1):
            url = str(item.get("url") or "").split("#")[0]
            row = {"query": query, "mode": mode, "domain": domain, "rank": rank, "url": url,
                   "title": str(item.get("title") or "")[:160]}
            site, kind = classify_url(url) if url else (None, "no_url")
            if not url:
                row.update(decision="dropped", reason="no_url")
            elif site != domain:
                row.update(decision="dropped", reason="off_site" if site is None else f"other_site:{site}")
            elif url in seen:
                row.update(decision="dropped", reason="duplicate")
            elif kind == "home":
                row.update(decision="dropped", reason="home_page")
            elif kind == "version":
                seen.add(url)
                scored = slug_score(url, row["title"], terms, identity, payload)
                versions.append({"url": url, "score": scored["score"], "order": len(versions) + len(listings),
                                 "source": "search"})
                row.update(decision="version_candidate", score=scored["score"], matched=scored["matched"],
                           contradicted=scored["contradicted"])
                useful = True
            elif names_model(unquote(url) + " " + row["title"]):
                seen.add(url)
                listings.append({"url": url, "order": len(versions) + len(listings)})
                row.update(decision="listing_for_links")
                useful = True
            else:
                row.update(decision="dropped", reason="listing_without_model")
            out["results"].append(row)
        return useful

    def run_search(query: str, domain: str, mode: str) -> bool:
        out["searches"] += 1
        out["queries"].append(query)
        try:
            result = search(query, domain)
        except Exception as exc:  # noqa: BLE001 - a failed search costs only its results
            out["skipped"].append({"query": query, "mode": mode, "error": f"{type(exc).__name__}"})
            return False
        _offer(ctx, search_result=result)
        return triage(_search_items(result), domain, query, mode)

    try:
        for domain, query in search_queries(terms, limit=10 ** 6):
            if out["searches"] >= max_searches:
                break
            if not run_search(query, domain, "site") and out["searches"] < max_searches:
                # nothing useful from the `site:` query: once more with the backend's domain filter, no `site:` prefix
                run_search(_strip_site(query), domain, "domain_filter")
        while out["fetches"] < max_fetches and (versions or listings):
            versions.sort(key=lambda c: (-c["score"], c["order"]))
            if versions:
                candidate, role = versions.pop(0), "version"
            else:
                candidate, role = listings.pop(0), "listing"
            url = candidate["url"]
            decision = {"url": url, "role": role, "score": candidate.get("score"), "source": candidate.get("source")}
            out["candidates"].append(decision)
            domain = site_of(url)
            if not robots(url):
                out["skipped"].append({"url": url, "reason": "robots_disallow"})
                decision["decision"] = "robots_disallow"
                continue
            pace.wait(domain)
            out["fetches"] += 1
            try:
                result = fetch(url) or {}
            except Exception as exc:  # noqa: BLE001
                out["skipped"].append({"url": url, "error": type(exc).__name__})
                decision["decision"] = f"fetch_error:{type(exc).__name__}"
                continue
            doc = result.get("document_id")
            status = result.get("status")
            if not doc or result.get("error") or (isinstance(status, int) and not 200 <= status < 300):
                reason = result.get("error") or f"status_{status}"
                out["skipped"].append({"url": url, "reason": reason})
                decision["decision"] = f"fetch_failed:{reason}"
                continue
            _offer(ctx, urls=[url, result.get("final_url")], links=_links(ctx, doc))
            material = adm.material(ctx.cache, doc, None, [doc])
            verdict = getattr(material, "version_page", None) if material is not None else None
            page = {"url": url, "role": role, "document_id": doc,
                    "status": (verdict or {}).get("status") or "not_a_version_page",
                    "reason": (verdict or {}).get("reason")}
            out["pages"].append(page)
            decision["decision"] = f"fetched:{page['status']}"
            if role == "version" and verdict and verdict.get("status") == "accepted":
                out["found"] += 1
                break
            if role == "listing" or verdict is None or not verdict.get("single_version"):
                # a model / price-list page of the site: its version links become candidates (never past the budget)
                links = [link for link in candidate_links(_links(ctx, doc), url, terms, identity, payload)
                         if link not in seen]
                seen.update(links)
                for link in links:
                    scored = slug_score(link, None, terms, identity, payload)
                    versions.append({"url": link, "score": scored["score"], "order": len(out["candidates"]) * 1000
                                     + len(versions), "source": f"links_of:{url}"})
                decision["links"] = len(links)
        for candidate in versions + listings:
            out["candidates"].append({"url": candidate["url"], "role": "version" if "score" in candidate else "listing",
                                      "score": candidate.get("score"), "source": candidate.get("source"),
                                      "decision": "not_fetched:" + ("found_earlier" if out["found"]
                                                                    else "fetch_budget")})
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


def _offer(ctx, *, search_result: Any = None, urls: Iterable[Any] = (), links: Iterable[dict] = ()) -> None:
    """PR #46 (P2): what the resolver saw is offered to the run's URL provenance (its search results, the pages it
    fetched and their links), so the model may fetch them later without a guessed-URL refusal. Never raises."""
    try:
        from .acquisition import url_provenance

        provenance = url_provenance(ctx)
        if search_result is not None:
            provenance.observe_search(search_result)
        provenance.offer([u for u in urls if u] + [link.get("url") for link in links or [] if isinstance(link, dict)])
    except Exception:  # noqa: BLE001
        pass


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
    """R1: a query carrying "site:<domain>" is sent as written with no backend domain filter; the retry (no "site:"
    prefix) uses the search backend's own domain filter."""
    from .tools.search import search_web

    if re.search(r"\bsite:\S+", query):
        return search_web(ctx, query, domain=None)
    return search_web(ctx, query, domain=domain)


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
