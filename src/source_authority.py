"""Deterministic source authority and source market (configured in data/source_rules.json).

    URL ──► source_authority   WHO publishes it: government | official_manufacturer | official_importer |
                               official_media | aggregator | marketplace | publisher | unknown
        ──► source market      WHICH market it describes (domain TLD, regional site, path locale, Hebrew text)

Authority is not identity: an official manufacturer page can describe another trim, engine or market,
and a rich aggregator can describe the exact target. Whether a document describes the target variant
is decided separately by src/document_binding.py. Neither is a truth score.

Python holds generic primitives only; brands, domains and markets live in the JSON rules.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

RULES_PATH = Path(__file__).resolve().parent.parent / "data" / "source_rules.json"
AUTHORITY_CLASSES = ("government", "official_manufacturer", "official_importer", "official_media", "aggregator",
                     "marketplace", "publisher", "unknown")
OFFICIAL_CLASSES = ("government", "official_manufacturer", "official_importer", "official_media")
_CACHE: dict[str, tuple[float, dict]] = {}
HEBREW = re.compile(r"[א-ת]")
LATIN = re.compile(r"[A-Za-z]")
LOCALE = re.compile(r"/([a-z]{2})[-_]([a-z]{2})(?=/|$|\?)", re.I)


def rules(path: Path | str | None = None) -> dict:
    path = Path(path or os.environ.get("SOURCE_RULES_PATH") or RULES_PATH)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cached = _CACHE.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    data = json.loads(path.read_text("utf-8"))
    _CACHE[str(path)] = (mtime, data)
    return data


def host_of(url: str | None) -> str:
    host = urlparse(str(url or "")).netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def split_host(host: str, rule_set: dict | None = None) -> tuple[str, str, str]:
    """(subdomain, registrable label, public suffix): 'newsroom.toyota.eu' -> ('newsroom', 'toyota', 'eu')."""
    r = rule_set if rule_set is not None else rules()
    parts = [p for p in host.split(".") if p]
    if len(parts) < 2:
        return "", host, ""
    two = ".".join(parts[-2:])
    if two in (r.get("two_level_public_suffixes") or []) and len(parts) >= 3:
        return ".".join(parts[:-3]), parts[-3], two
    return ".".join(parts[:-2]), parts[-2], parts[-1]


def _domain_match(host: str, domains: list[str]) -> str | None:
    for domain in domains or []:
        domain = domain.lower()
        if host == domain or host.endswith("." + domain):
            return domain
    return None


def _brand(rule_set: dict, manufacturer: str | None) -> dict:
    brands = rule_set.get("brands") or {}
    key = str(manufacturer or "").strip()
    if key in brands:
        return brands[key]
    low = key.lower()
    for name, entry in brands.items():   # an English manufacturer name matching a brand slug
        if low and low in [s.lower() for s in entry.get("slugs") or []]:
            return entry
    return {}


def _is_brand_label(label: str, slugs: list[str]) -> bool:
    return any(label == s or label.startswith(s + "-") or label.startswith(s) and label[len(s):] in ("europe", "eu")
               for s in (x.lower() for x in slugs))


def classify_source(url: str | None, manufacturer: str | None = None, rule_set: dict | None = None) -> dict:
    """{source_authority, authority_basis, source_domain}. Official classes are relative to the TARGET brand:
    another brand's official site is not authoritative for this vehicle."""
    r = rule_set if rule_set is not None else rules()
    host = host_of(url)
    if not host:
        return {"source_authority": "unknown", "authority_basis": "no_url", "source_domain": None}
    sub, label, suffix = split_host(host, r)
    out = {"source_domain": host}
    gov = next((s for s in r.get("government_suffixes") or [] if host == s or host.endswith("." + s)), None)
    if gov:
        return {**out, "source_authority": "government", "authority_basis": f"government_suffix:{gov}"}
    brand = _brand(r, manufacturer)
    media = _domain_match(host, brand.get("media_domains") or [])
    if media:
        return {**out, "source_authority": "official_media", "authority_basis": f"brand_media_domain:{media}"}
    slugs = brand.get("slugs") or []
    if slugs and _is_brand_label(label, slugs):
        first = sub.split(".")[-1] if sub else ""
        if first in (r.get("media_host_prefixes") or []) or any(t in host for t in r.get("media_label_terms") or []):
            return {**out, "source_authority": "official_media", "authority_basis": f"brand_media_host:{host}"}
        tld = suffix.split(".")[-1]
        if tld in (r.get("importer_tlds") or []):
            return {**out, "source_authority": "official_importer", "authority_basis": f"brand_domain_importer_tld:{host}"}
        return {**out, "source_authority": "official_manufacturer", "authority_basis": f"brand_domain:{host}"}
    for cls in ("aggregator", "marketplace", "publisher"):
        hit = _domain_match(host, r.get(cls) or [])
        if hit:
            return {**out, "source_authority": cls, "authority_basis": f"rule:{cls}:{hit}"}
    other = next((name for name, entry in (r.get("brands") or {}).items()
                  if entry is not brand and _is_brand_label(label, entry.get("slugs") or [])), None)
    if other:
        return {**out, "source_authority": "unknown", "authority_basis": "official_domain_of_another_brand"}
    return {**out, "source_authority": "unknown", "authority_basis": "no_rule"}


def normalize_market(value: Any, rule_set: dict | None = None) -> str | None:
    """Canonical market code ('IL', 'UK', 'EU', ...) or None for unknown / unstated."""
    r = rule_set if rule_set is not None else rules()
    text = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    if text in ("", "unknown", "n/a", "na", "none", "unclear", "?", "global", "various", "multiple"):
        return None
    for code, aliases in (r.get("market_aliases") or {}).items():
        if text == code.lower() or text in aliases:
            return code
    code = text.upper()
    aliases = r.get("country_code_aliases") or {}
    if code in aliases:
        return aliases[code]
    return code if re.fullmatch(r"[A-Z]{2,3}", code) else str(value).strip()


def url_market(url: str | None, rule_set: dict | None = None) -> tuple[str | None, str | None]:
    """(market, basis) from the URL alone, or (None, None)."""
    r = rule_set if rule_set is not None else rules()
    host = host_of(url)
    if not host:
        return None, None
    _, label, suffix = split_host(host, r)
    tld = suffix.split(".")[-1]
    by_tld = r.get("market_by_tld") or {}
    if tld in by_tld:
        return by_tld[tld], f"domain_tld:.{tld}"
    for hint, market in (r.get("market_by_registrable_hint") or {}).items():
        if hint in label:
            return market, f"regional_domain:{label}"
    path = urlparse(str(url)).path.lower()
    match = LOCALE.search(path)
    if match:
        market = normalize_market(match.group(2), r)
        if market:
            return market, f"path_locale:{match.group(0).strip('/')}"
    return None, None


def text_market(text: str | None, rule_set: dict | None = None) -> tuple[str | None, str | None]:
    """IL when the document is predominantly Hebrew (letters, not markup)."""
    r = rule_set if rule_set is not None else rules()
    sample = (text or "")[:20000]
    hebrew, latin = len(HEBREW.findall(sample)), len(LATIN.findall(sample))
    if hebrew + latin < 40:
        return None, None
    share = hebrew / (hebrew + latin)
    if share >= float(r.get("hebrew_text_min_share") or 0.3):
        return r.get("hebrew_text_market") or "IL", f"hebrew_text:{share:.2f}"
    return None, None


def source_market(url: str | None, text: str | None, rule_set: dict | None = None) -> tuple[str | None, str | None]:
    """Server-side market of a source: URL first (domain, regional site, locale path), then Hebrew text."""
    market, basis = url_market(url, rule_set)
    if market:
        return market, basis
    return text_market(text, rule_set)
