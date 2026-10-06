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
                     "marketplace", "publisher", "unknown",
                     # PR #44 (P1): the government vehicle registry index (src/gov_registry.py), never a URL rule
                     "government_registry")
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
    generic = len(parts[-1]) == 2 and parts[-2] in (r.get("second_level_labels") or [])   # toyota.com.tw, .co.th
    if (two in (r.get("two_level_public_suffixes") or []) or generic) and len(parts) >= 3:
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


def _is_brand_label(label: str, slugs: list[str], rule_set: dict | None = None) -> bool:
    """The registrable label IS the brand (toyota) or the brand with a listed suffix (toyota-europe, cadillaceurope);
    toyota-forum or mini-storage are not."""
    suffixes = (rule_set if rule_set is not None else rules()).get("brand_label_suffixes") or []
    for slug in (x.lower() for x in slugs):
        if label == slug:
            return True
        rest = label[len(slug):] if label.startswith(slug) else None
        if rest is not None and rest.lstrip("-") in suffixes:
            return True
    return False


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
    if slugs and _is_brand_label(label, slugs, r):
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
                  if entry is not brand and _is_brand_label(label, entry.get("slugs") or [], r)), None)
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
    for match in LOCALE.finditer(path):
        language, country = match.group(1), match.group(2)
        countries = {k.lower() for k in (r.get("market_by_tld") or {})} | {"gb", "us"}
        if language in (r.get("locale_languages") or []) and country in countries:
            market = normalize_market(country, r)
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


# --- production source policy (data/source_policy.json + the operator's overlay in the data volume) --------------------
#
# A domain or dataset is fetched only when the policy lists it as `allowed` or `identity_only`; its documents are
# evidence only when `allowed`. Everything else, an unlisted domain included, is `blocked`. There is no research mode:
# the only way to unblock a domain is the policy file or the operator's overlay (the Data page), and every overlay change
# is logged. Tests replace the policy through `_TEST_OVERRIDE` (tests/conftest.py), never through configuration.

POLICY_PATH = Path(__file__).resolve().parent.parent / "data" / "source_policy.json"
POLICY_VERSION = "source-policy-v1"
POLICIES = ("allowed", "identity_only", "blocked")
OVERLAY_NAME = "source_policy_overlay.json"
CHANGES_NAME = "source_policy_changes.jsonl"
_POLICY_STATE: dict[str, Any] = {"overlay_dir": None}
_POLICY_CACHE: dict[str, tuple[tuple, dict]] = {}
# tests/conftest.py sets this to "allowed" for the tests written before the policy (their fixture domains are not in
# the production policy); tests of the policy itself run with the real policy (marker `real_source_policy`)
_TEST_OVERRIDE: str | None = None


def set_policy_overlay_dir(path: Path | str | None) -> None:
    """Where the operator's overlay and change log live (<TRIPY_DATA_DIR>/derived; set by the RunManager)."""
    _POLICY_STATE["overlay_dir"] = Path(path) if path else None
    _POLICY_CACHE.clear()


def overlay_dir() -> Path | None:
    return _POLICY_STATE.get("overlay_dir")


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _mtime(path: Path | None) -> float:
    try:
        return path.stat().st_mtime if path else 0.0
    except OSError:
        return 0.0


def _normalize_domain(domain: Any) -> str:
    host = host_of(domain if "://" in str(domain or "") else f"https://{domain}")
    return host.strip(".")


def source_policy(path: Path | str | None = None) -> dict:
    """The effective policy: {version, unlisted, entries: [...], overlay: {...}} = the repository file with the
    operator's overlay applied (an overlay entry replaces the policy / terms of the listed domain or adds one)."""
    base_path = Path(path or POLICY_PATH)
    over = overlay_dir()
    over_path = over / OVERLAY_NAME if over else None
    key = (str(base_path), _mtime(base_path), str(over_path), _mtime(over_path))
    cached = _POLICY_CACHE.get("policy")
    if cached and cached[0] == key:
        return cached[1]
    base = _read_json(base_path)
    entries = [dict(e) for e in base.get("entries") or [] if isinstance(e, dict)]
    overlay = _read_json(over_path) if over_path else {}
    for domain, change in sorted((overlay.get("domains") or {}).items()):
        domain = _normalize_domain(domain)
        if not domain or not isinstance(change, dict) or change.get("policy") not in ("allowed", "blocked"):
            continue
        # the overlay's domain becomes its own entry (the most specific match wins), so switching bmw.co.il never
        # switches the other importer domains listed with it
        entries = [e for e in entries if not (e.get("kind") == "domain" and e.get("domains") == [domain]
                                              and e.get("overlay"))]
        listed = next((e for e in entries if domain in (e.get("domains") or [])), None)
        entries.append({**({k: v for k, v in listed.items() if k not in ("id", "domains")} if listed else
                           {"kind": "domain", "licence": None, "attribution": None, "bulk_store": False}),
                        "id": domain, "domains": [domain], "policy": change["policy"],
                        "terms_clause": change.get("terms_clause"), "terms_clause_source": "operator overlay",
                        "checked_at": change.get("checked_at"), "changed_at": change.get("changed_at"),
                        "changed_by": change.get("changed_by"), "overlay": True})
    out = {"version": base.get("version") or POLICY_VERSION, "unlisted": "blocked", "entries": entries,
           "overlay_path": str(over_path) if over_path else None}
    _POLICY_CACHE["policy"] = (key, out)
    return out


def _entry_for_host(host: str, policy: dict) -> tuple[dict | None, str | None]:
    best, best_domain = None, None
    for entry in policy.get("entries") or []:
        for domain in entry.get("domains") or []:
            domain = str(domain).lower()
            if (host == domain or host.endswith("." + domain)) and (best_domain is None
                                                                     or len(domain) > len(best_domain)
                                                                     or (len(domain) == len(best_domain)
                                                                         and entry.get("overlay"))):
                best, best_domain = entry, domain
    return best, best_domain


def policy_of(url: str | None, policy: dict | None = None) -> dict:
    """{policy, entry, domain, matched, licence, attribution, bulk_store}: the production policy of a URL (or a bare
    host). Unlisted -> blocked. Deterministic; no network."""
    host = _normalize_domain(url)
    if _TEST_OVERRIDE is not None:
        return {"policy": _TEST_OVERRIDE, "entry": "test_override", "domain": host, "matched": None,
                "licence": None, "attribution": None, "bulk_store": False}
    policy = source_policy() if policy is None else policy
    entry, matched = _entry_for_host(host, policy) if host else (None, None)
    if entry is None or entry.get("policy") not in POLICIES:
        return {"policy": "blocked", "entry": None, "domain": host, "matched": None, "licence": None,
                "attribution": None, "bulk_store": False, "reason": "unlisted"}
    return {"policy": entry["policy"], "entry": entry.get("id"), "domain": host, "matched": matched,
            "licence": entry.get("licence"), "attribution": entry.get("attribution"),
            "bulk_store": bool(entry.get("bulk_store")), "reason": "listed"}


def dataset_policy(dataset_id: str, policy: dict | None = None) -> dict:
    """The policy entry of an open dataset by its id (blocked when not listed)."""
    policy = source_policy() if policy is None else policy
    entry = next((e for e in policy.get("entries") or [] if e.get("id") == dataset_id), None)
    if entry is None:
        return {"policy": "blocked", "entry": None, "licence": None, "attribution": None, "bulk_store": False}
    return {"policy": entry.get("policy"), "entry": entry.get("id"), "licence": entry.get("licence"),
            "attribution": entry.get("attribution"), "bulk_store": bool(entry.get("bulk_store"))}


def fetch_allowed(url: str | None) -> bool:
    """May this URL be fetched at all (allowed, or identity_only for identity keys)?"""
    return policy_of(url)["policy"] in ("allowed", "identity_only")


def evidence_allowed(url: str | None) -> bool:
    """May a document of this URL be evidence (only `allowed`; identity_only never yields a value)?"""
    return policy_of(url)["policy"] == "allowed"


def policy_refusal(url: str, stage: str) -> dict:
    """The tool result of a refused fetch / render (`policy_blocked`)."""
    verdict = policy_of(url)
    return {"error": "policy_blocked", "url": url, "domain": verdict["domain"], "policy": verdict["policy"],
            "stage": stage, "message": f"{verdict['domain']} is not allowed by the production source policy "
                                       "(data/source_policy.json); use other sources."}


def note_policy_block(ctx, url: str, stage: str) -> None:
    """Count one dropped / refused URL per domain (counters policy_blocked and policy_blocked:<domain>)."""
    domain = _normalize_domain(url)
    try:
        ctx.counters["policy_blocked"] += 1
        ctx.counters[f"policy_blocked:{domain}"] += 1
    except Exception:  # noqa: BLE001 - a context without counters
        pass


def update_policy(domain: str, policy: str, *, terms_clause: str | None, checked_at: str | None,
                  changed_by: str = "operator", now: str | None = None) -> dict:
    """The operator's switch of one domain between `blocked` and `allowed` (the Data page): written to the overlay in
    the data volume and appended to the change log. ValueError for anything else (an unknown policy, a missing terms
    clause / date when allowing, no overlay directory)."""
    from datetime import datetime, timezone

    domain = _normalize_domain(domain)
    if not domain or "." not in domain:
        raise ValueError("a domain is required")
    if policy not in ("allowed", "blocked"):
        raise ValueError("policy must be allowed or blocked")
    if policy == "allowed" and not ((terms_clause or "").strip() and (checked_at or "").strip()):
        raise ValueError("allowing a domain needs the terms clause it rests on and the date it was checked")
    folder = overlay_dir()
    if folder is None:
        raise ValueError("no data volume: the policy overlay cannot be stored")
    now = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / OVERLAY_NAME
    overlay = _read_json(path)
    domains = overlay.setdefault("domains", {})
    before = policy_of(domain)["policy"]
    domains[domain] = {"policy": policy, "terms_clause": (terms_clause or "").strip() or None,
                       "checked_at": (checked_at or "").strip() or None, "changed_at": now, "changed_by": changed_by}
    from .storage.atomic import atomic_write_json

    atomic_write_json(path, overlay, durable=True)
    change = {"at": now, "domain": domain, "from": before, "to": policy, "terms_clause": domains[domain]["terms_clause"],
              "checked_at": domains[domain]["checked_at"], "changed_by": changed_by}
    with (folder / CHANGES_NAME).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(change, ensure_ascii=False) + "\n")
    _POLICY_CACHE.clear()
    return change


def policy_changes(limit: int = 200) -> list[dict]:
    folder = overlay_dir()
    if folder is None:
        return []
    try:
        lines = (folder / CHANGES_NAME).read_text("utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def policy_table() -> dict:
    """The Data page's policy table: every entry with its effective policy, plus the change log."""
    policy = source_policy()
    return {"version": policy["version"], "unlisted": "blocked", "overlay_path": policy.get("overlay_path"),
            "entries": [{k: e.get(k) for k in ("id", "kind", "domains", "policy", "licence", "attribution",
                                               "bulk_store", "terms_clause", "terms_clause_source", "checked_at",
                                               "changed_at", "changed_by", "overlay", "note")}
                        for e in policy["entries"]],
            "changes": policy_changes()}


def attribution_for(entry_id: str | None, **values: Any) -> str | None:
    """The exact attribution string a licence requires ({year} / {date} filled), None when it requires none."""
    entry = next((e for e in source_policy().get("entries") or [] if e.get("id") == entry_id), None)
    text = (entry or {}).get("attribution")
    if not text:
        return None
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text
