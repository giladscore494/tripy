"""V1-V4: the open-data identity drives the web research.

When the open-data match reaches exact_technical_variant (K1: the EEA type code, or the co2 key), the configuration it
found is a set of identity keys the web research can use:

    research identity   the EEA model name ("530e xDrive iPerformance"), the type code (JP91), the type-approval
                        number(s) (T), power (kW), co2 (WLTP g/km), displacement (cc) and the other type codes of the
                        same model (siblings); recorded on the `open_data_match` event (`identity`), so a binding replay
                        re-reads exactly what the run used
    V1 search terms     a deterministic identity search wave on the domains the effective source policy ALLOWS at run
                        time (the brand's importer domains and its official manufacturer domains; nothing else), the
                        resolver's identity queries, and the terms in the acquisition / reacquire prompts
                        (`queries_with_open_data_identity`)
    V2 page keys        document_binding.open_data_keys_verdict: a page stating the type-approval number, the type code,
                        or kW + co2 + cc binds exact_technical_variant (basis open_data_identity_keys); a page of the same
                        model naming only sibling type codes, or (conventional targets only) stating a sibling
                        configuration's kW and not the target's, is another variant (veto)
    V3 corroboration    a web value equal (D1 rounding) to an `offered` open-data offer identified by every survivor
                        (unique / all_survivors_agree) of the same field is raised to exact_technical_variant (basis
                        open_data_corroborated); both sources are recorded
    V4 report           per run: queries_with_open_data_identity, pages_verified_by_open_data_keys,
                        values_corroborated_by_open_data, and the ok fields gained / lost against the same run without V
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Iterable

IDENTITY_VERSION = "open-data-identity-v1"
EXACT = "exact_technical_variant"
EEA = "eea_co2_cars"
CORROBORATING = ("unique", "all_survivors_agree")


def _level_index(level: str | None) -> int:
    from ..document_binding import level_index

    return level_index(level)


def _common(rows: list[dict], key: str) -> Any:
    values = {json.dumps(r.get(key)) for r in rows if r.get(key) not in (None, "")}
    return json.loads(values.pop()) if len(values) == 1 else None


def _number(value: Any) -> int | float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def research_identity(result: dict) -> dict | None:
    """The research identity of a match result (module docstring), None below exact_technical_variant. Read from the
    lead source's surviving rows (EEA when its type code or co2 key decided): a key is kept only when every survivor
    states the same value."""
    if _level_index(result.get("level")) < _level_index(EXACT):
        return None
    sources = result.get("sources") or {}
    eea = sources.get(EEA) or {}
    eea_decided = eea.get("status") in ("unique", "ambiguous") and (
        (eea.get("type_code") or {}).get("status") == "match" or eea.get("exact_subset"))
    lead = EEA if eea_decided else result.get("lead_source")
    src = sources.get(lead) or {}
    survivors = [r for r in src.get("survivors") or [] if isinstance(r, dict)]
    if not survivors:
        return None
    subset = set((src.get("exact_subset") or {}).get("row_ids") or [])
    if subset:
        survivors = [r for r in survivors if r.get("row_id") in subset] or survivors
    typed = src.get("type_code") or {}
    model = _common(survivors, "model")
    eea_code = _common(survivors, "variant") if typed.get("status") == "match" else None
    out = {"version": IDENTITY_VERSION, "level": result.get("level"), "source": lead,
           "designation": result.get("designation"), "model": str(model).strip() if model else None,
           "type_code": typed.get("code") if typed.get("status") == "match" else None,
           "eea_type_code": str(eea_code).strip() if eea_code else None,
           "type_approvals": sorted({str(r["type_approval"]).strip() for r in survivors if r.get("type_approval")}),
           "power_kw": _number(_common(survivors, "power_kw")),
           "co2_wltp": _number(_common(survivors, "co2_wltp")),
           "displacement_cc": _number(_common(survivors, "displacement_cc")),
           "sibling_type_codes": list(typed.get("siblings") or []),
           "sibling_power_kw": [_number(k) for k in typed.get("sibling_power_kw") or []],
           "row_ids": [r.get("row_id") for r in survivors][:20]}
    out["terms"] = identity_terms(out)
    return out


def identity_terms(identity: dict | None) -> list[str]:
    """The search terms of a research identity, most specific first: the model name, the type code, kW, T."""
    if not identity:
        return []
    terms = [identity.get("model"), identity.get("eea_type_code") or identity.get("type_code"),
             f"{identity['power_kw']} kW" if identity.get("power_kw") else None, *identity.get("type_approvals", [])]
    return list(dict.fromkeys(str(t).strip() for t in terms if t and str(t).strip()))


def corroborating_offers(offers: Iterable[dict] | None) -> dict[str, list[dict]]:
    """{field: [offer]}: the `offered` offers every survivor states (unique / all_survivors_agree) with a value."""
    from ..fields import normalize_field_name

    out: dict[str, list[dict]] = {}
    for offer in offers or []:
        if offer.get("status") == "offered" and offer.get("identified_by") in CORROBORATING \
                and offer.get("value") is not None:
            out.setdefault(normalize_field_name(offer["field"]), []).append(
                {k: offer.get(k) for k in ("field", "source", "column", "value", "definition", "identified_by",
                                           "row_ids", "subset")})
    return out


def apply_to_admission(adm, state: dict | None) -> None:
    """The run's open-data state (live) or its `open_data_match` event (replay) onto the admission context: the
    identity keys (V2) and the corroborating offers (V3)."""
    state = state or {}
    adm.identity.open_data_keys = dict(state.get("identity") or {})
    adm.open_data_offers = corroborating_offers(state.get("offers"))


def matching_offer(adm, name: str, spec: dict, value: Any, stated: dict | None) -> dict | None:
    """V3: the corroborating offer whose value equals the web value under D1 (conflict_normalizer): a number stated in
    the field's own unit only when it is the same number; a converted number (inches, lb) within its conversion's
    rounding (its last stated digit through the conversion, stated_interval) plus half the offer's own last digit."""
    from ..conflict_normalizer import _last_digit, stated_interval
    from ..fields import normalize_field_name

    offers = (getattr(adm, "open_data_offers", None) or {}).get(normalize_field_name(name)) or []
    if not offers:
        return None
    try:
        web = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    item = {"value": value, "stated_number": stated, "typed_value": {"type": "scalar", "value": web}}
    try:
        interval = stated_interval(item, spec)
    except Exception:  # noqa: BLE001 - an unknown conversion: the plain number
        interval = None
    converted = bool(interval and interval.get("converted"))
    center, tolerance = (interval["center"], interval["tolerance"]) if converted else (web, 0.0)
    for offer in offers:
        number = _number(offer.get("value"))
        if number is None:
            continue
        # D1: a number stated in the field's own unit equals only the same number; a converted one (inches, lb)
        # equals within its conversion's rounding (+ half the offer's own last digit)
        half = 0.5 * (_last_digit(str(offer.get("value"))) or 0.0) if converted else 0.0
        if abs(center - float(number)) <= tolerance + half + 1e-9:
            return offer
    return None


def corroborate(binding: dict, offer: dict | None, value: Any) -> dict:
    """V3: a fact bound at body_powertrain or above, without a veto, whose value equals a corroborating offer is
    raised to exact_technical_variant (basis open_data_corroborated); the offer is recorded either way."""
    if offer is None or binding.get("binding_veto") or binding.get("variant_match") == "different":
        return binding
    level = binding.get("binding_level")
    if _level_index(level) < _level_index("body_powertrain"):
        return binding
    out = dict(binding)
    out["open_data_corroboration"] = {"web_value": value, "offer": offer}
    if _level_index(level) < _level_index(EXACT):
        out["without_open_data"] = {k: binding.get(k) for k in ("binding_level", "variant_match", "binding_basis")}
        out["binding_level"], out["binding_basis"] = EXACT, "open_data_corroborated"
        required = binding.get("binding_requirement")
        out["variant_match"] = "exact" if _level_index(EXACT) >= _level_index(required) else "unclear"
        out["binding_rules"] = list(binding.get("binding_rules") or []) + ["open_data_corroborated"]
    return out


# --- V4: the per-run report ------------------------------------------------------------------------------------------------

def _names_identity(query: str, terms: list[str]) -> bool:
    low = " ".join(str(query or "").lower().split())
    return any(t.lower() in low for t in terms if len(t) >= 3)


def research_report(events: list[dict], specs: list[dict] | None, market: str | None) -> dict:
    """V4 over a run's events: the identity queries, the pages verified by the identity keys, the values corroborated,
    and the ok fields gained / lost against the same run without V (each V-changed evidence item restored to the
    binding it had without the open-data identity)."""
    from ..field_recovery import current_evaluation
    from ..storage import trace

    match = next((e for e in events if e.get("kind") == "open_data_match"), {}) or {}
    terms = list((match.get("identity") or {}).get("terms") or [])
    queries = [e.get("query") for e in events if e.get("kind") == "search" and e.get("query")]
    identity_queries = sorted({q for q in queries if terms and _names_identity(q, terms)})
    items = [i for i in trace.evidence_items(events) if i.get("admission_status", "accepted") == "accepted"]
    verified = sorted({str(i.get("document_id")) for i in items
                       if (i.get("open_data_keys") or {}).get("status") == "target"})
    vetoed = sorted({str(i.get("document_id")) for i in items
                     if (i.get("open_data_keys") or {}).get("status") == "other_variant"})
    corroborated = [i for i in items if i.get("open_data_corroboration")]
    out = {"version": IDENTITY_VERSION, "identity_terms": terms,
           "queries_with_open_data_identity": len(identity_queries), "identity_queries": identity_queries[:20],
           "pages_verified_by_open_data_keys": len(verified), "verified_documents": verified[:20],
           "pages_vetoed_by_open_data_keys": len(vetoed),
           "values_corroborated_by_open_data": len(corroborated),
           "corroborated": [{"field": i.get("field"), "value": i.get("value"), "evidence_id": i.get("evidence_id"),
                             "offer": (i.get("open_data_corroboration") or {}).get("offer")}
                            for i in corroborated][:20]}
    if specs:
        ok_now = {e["field"] for e in current_evaluation(events, specs, market) if e["state"] == "ok"}
        ok_without = {e["field"] for e in current_evaluation(without_open_data(events), specs, market)
                      if e["state"] == "ok"}
        out["fields_gained"] = sorted(ok_now - ok_without)
        out["fields_lost"] = sorted(ok_without - ok_now)
    return out


def without_open_data(events: list[dict]) -> list[dict]:
    """A copy of the events with every evidence item V changed restored to its binding without the open-data identity."""
    copied = copy.deepcopy(events)
    for event in copied:
        item = event.get("evidence") if event.get("kind") == "evidence" else None
        if isinstance(item, dict) and isinstance(item.get("without_open_data"), dict):
            item.update(item["without_open_data"])
    return copied


# --- V1: the identity search wave ------------------------------------------------------------------------------------------

def identity_domains(manufacturer: str | None) -> tuple[list[str], list[dict]]:
    """(the domains the identity wave may search: the brand's importer domains first, then its official manufacturer
    domains, ONLY those the effective source policy allows now; every domain considered with its policy)."""
    from ..source_authority import policy_of
    from ..tools.search import official_domain_defaults

    allowed, considered = [], []
    for domain in official_domain_defaults(manufacturer):
        policy = policy_of(f"https://{domain}/")["policy"]
        considered.append({"domain": domain, "policy": policy})
        if policy == "allowed":
            allowed.append(domain)
    return allowed, considered


def identity_queries(identity: dict | None, domains: list[str], config: dict | None = None) -> list[tuple[str, str]]:
    """(domain, query) of the identity wave: every template on every allowed domain (template-major), at most
    max_searches; a template whose placeholder has no value is skipped."""
    from . import datasets as ds

    if not identity or not domains:
        return []
    cfg = (ds.config() if config is None else config).get("research_identity") or {}
    values = {"model": identity.get("model"), "type_code": identity.get("eea_type_code") or identity.get("type_code"),
              "power_kw": identity.get("power_kw"),
              "type_approval": (identity.get("type_approvals") or [None])[0]}
    out: list[tuple[str, str]] = []
    for template in cfg.get("templates") or []:
        for domain in domains[:int(cfg.get("max_domains") or 3)]:
            needed = re.findall(r"\{(\w+)\}", template)
            if any(key != "domain" and not values.get(key) for key in needed):
                continue
            query = template
            for key, value in {"domain": domain, **values}.items():
                query = query.replace("{" + key + "}", str(value or ""))
            query = " ".join(query.split())
            if (domain, query) not in out:
                out.append((domain, query))
    return out[:int(cfg.get("max_searches") or 4)]


def identity_search_wave(ctx, run_log=None, *, identity: dict | None, manufacturer: str | None,
                         search=None) -> dict:
    """V1: the deterministic identity searches (identity_queries) on the allowed domains, before research turn 1; the
    result URLs are offered to the acquisition (never fetched here). Never raises; event `open_data_identity_search`."""
    out: dict[str, Any] = {"version": IDENTITY_VERSION, "searches": 0, "queries": [], "results": []}
    try:
        if not identity:
            out["skipped"] = "no_identity (open-data match below exact_technical_variant)"
            return out
        allowed, considered = identity_domains(manufacturer)
        out.update(terms=identity.get("terms") or [], allowed_domains=allowed,
                   blocked_domains=[c["domain"] for c in considered if c["policy"] != "allowed"])
        if not allowed:
            out["skipped"] = "no_allowed_domain (the source policy allows none of the brand's importer / manufacturer " \
                             "domains)"
            return out
        if search is None:
            from ..il_version_pages import _default_search

            def search(query, domain):
                return _default_search(ctx, query, domain)
        from ..il_version_pages import _offer, _search_items

        for domain, query in identity_queries(identity, allowed):
            out["searches"] += 1
            out["queries"].append({"domain": domain, "query": query})
            result = search(query, domain)
            _offer(ctx, search_result=result)
            for item in _search_items(result)[:5]:
                if item.get("url"):
                    out["results"].append({"query": query, "url": item["url"], "title": item.get("title")})
    except Exception as exc:  # noqa: BLE001 - the wave never costs the run
        out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    finally:
        counters = getattr(ctx, "counters", None)
        if counters is not None:
            counters["open_data_identity_searches"] += out["searches"]
        if run_log is not None:
            run_log.event("open_data_identity_search", **out)
    return out


def research_note(identity: dict | None, wave: dict | None = None) -> str:
    """V1: the identity terms for the acquisition / reacquire prompts (and the domains the policy allows)."""
    terms = identity_terms(identity)
    if not terms:
        return ""
    keys = ", ".join(f"{k} {identity[k]}" for k in ("power_kw", "co2_wltp", "displacement_cc") if identity.get(k))
    text = (f"Identity of this exact configuration (open data, {identity.get('source')}): search for "
            + ", ".join(f'"{t}"' for t in terms) + f" ({keys}). A page that states these keys (the type code, the "
            "type-approval number, or the same kW, WLTP co2 and displacement) describes this exact version; a page "
            "of the model stating only another kW or another type code describes another version.")
    if wave is not None:
        allowed = wave.get("allowed_domains") or []
        text += (" Allowed official domains for these searches: " + ", ".join(allowed) + "." if allowed else
                 " The source policy allows none of the brand's importer / manufacturer domains.")
        urls = [r["url"] for r in wave.get("results") or []][:8]
        if urls:
            text += " Identity search results (offered): " + ", ".join(urls) + "."
    return text
