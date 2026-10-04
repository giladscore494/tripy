"""Document Variant Map (DVM): which technical variant each REGION of a document describes, computed once per document.

Spec documents state a variant's identity once per column, card or page, not next to every value. Per-value binding
(src/document_binding.bind) reads only the text around a value, so a value in the "AWD 486 hp" column of a table whose
header row the table parser could not recognise, or a wheelbase stated once on an importer page covering RWD and AWD,
never binds. The DVM binds the REGIONS once and lets values inherit:

    inventory      every variant identity the document states about the target family (identity zone, table column
                   headers and identity rows, DOM groups, section headings, identity-labelled lines; page chrome and
                   lines that name only another model family are ignored)
    regions        table columns (`table:<t>:col:<j>`), variant-labelled table rows (`table:<t>:row:<r>`), DOM pair
                   groups (`group:<n>`), variant-labelled lines (`line:<i>`), sections under a variant heading
                   (`section:<i>`) and the whole document (`document`), each with an identity vector built from ALL its
                   identity rows / pairs (drivetrain, power, battery kWh, displacement, catalog trim, model code)
    catalog        each region is matched against the government catalog entries of the target's manufacturer / family /
                   model year (data/catalog_trim_index.json, document_binding.catalog_family_entries): drivetrain, power
                   (the discriminating tolerance of R3), displacement, catalog trim. A region without a power is
                   narrowed by the powers the rest of the inventory states for compatible regions (elimination); a
                   power the inventory states that matches no candidate, an incomplete catalog entry the region could
                   match, or more than one remaining candidate leaves the region unresolved (never a guess)

Per fact (fact_region), the most specific region holding the value decides: `target` (assigned to the target's
technical variant), `other_variant` (its identity contradicts the target: the fact is `different`), `shared` (a table
row identical in every variant column, or a value stated once at document level, without a variant qualifier or a
hedge, that the document never states with another value) or `unresolved`. Pure and deterministic, no model calls; the
binding consequences live in document_binding.bind and the safety gates in evidence_admission.fact_binding.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .candidate_harvest import NUMBER, normalize_text, parse_number
from .document_binding import (TargetIdentity, _close, _family_status, _other_family_pattern, _veto_dims,
                               catalog_family_entries, mentions, power_bucket, vocabulary)

VARIANT_MAP_VERSION = "variant-map-v1"
SECTION_MAX_LINES = 40
HEADING_MAX_CHARS = 60
BATTERY = re.compile(r"(?<![\d.,])(\d{2,3}(?:[.,]\d)?)\s*(?:kwh|קוט\"ש|קוט\"שׁ)(?![a-z])")
# a value whose own words hedge it is never shared across variants ("up to 451 kW", "החל מ-", "עד", footnote markers)
HEDGE_BEFORE = re.compile(r"(?:(?<![\wא-ת])(?:עד|החל\s*מ-?|מ-|up\s+to|from|starting(?:\s+at|\s+from)?|approx\.?|"
                          r"approximately|about|ca\.|כ-|כ־|~)\s*)$")
HEDGE_AFTER = re.compile(r"^\s*(?:[a-zא-ת\"'/%°.\d]{0,8}\s*)?(?:\*|[¹²³⁴⁵⁶⁷⁸⁹⁰]|\(\d\)|\[\d\])")
DISCRIMINATING = ("drivetrain", "power", "displacement", "trim", "model_code")
# numbers a variant heading may hold: power, battery, model year ("AWD 486 כ"ס", "87.5 kWh", "G6 2026")
IDENTITY_NUMBERS = re.compile(r"(?<![\d.,])\d{2,4}(?:[.,]\d)?\s*(?:kwh|kw|hp|ps|bhp|כ\"ס|קוט\"ש)(?![a-z])"
                              r"|(?<!\d)20[0-3]\d(?!\d)")


# --- identity vectors -------------------------------------------------------------------------------------------------

def _empty() -> dict:
    return {"drivetrain": [], "power": [], "battery": [], "displacement": [], "trim": [], "body": [], "propulsion": [],
            "model_code": False}


def _catalog_trims(entries: list[dict]) -> list[str]:
    return sorted({t for e in entries for t in e["trims"] if t})


def _trim_pattern(trim: str, identity: TargetIdentity, vocab: dict):
    """A catalog trim as a phrase. A trim made only of generic words (MAX, PRO, BASE EDITION) only qualified by the
    family ("G6 MAX"), as the #34 qualified-phrase rule; any other trim as its whole phrase."""
    words = [w for w in re.split(r"[^\wא-ת]+", normalize_text(trim)) if w]
    if not words:
        return None
    generic = {w.lower() for w in vocab.get("generic_trim_words") or []}
    body = r"[\s\-]*".join(re.escape(w) for w in words)
    if all(w in generic for w in words):
        aliases = (vocab.get("model_families") or {}).get(identity.family or "") or [identity.family or ""]
        family = "|".join(r"[\s\-]*".join(re.escape(p) for p in normalize_text(a).split()) for a in aliases if a)
        if not family:
            return None
        body = rf"(?:{family})[\s\-]+{body}"
    return re.compile(rf"(?<![\wא-ת]){body}(?![\wא-ת])")


def identity_vector(text: str, identity: TargetIdentity, trims: list[str]) -> dict:
    """What variant a text names: drivetrain keys, powers (hp), battery kWh, displacements, catalog trims (generic-only
    trims only qualified by the family), body / strong propulsion keys and whether it names the target's model code."""
    vec = _empty()
    norm = normalize_text(text or "")
    if not norm.strip():
        return vec
    found = mentions(norm, identity)
    vec["drivetrain"] = sorted(found["drivetrain"])
    vec["power"] = sorted(found["power"])
    vec["displacement"] = sorted(found["displacement"])
    vec["body"] = sorted(found["body"])
    vec["propulsion"] = sorted(k for k in found["propulsion"] if not str(k).startswith("weak:"))
    vec["model_code"] = bool(found["model_code"])
    vec["battery"] = sorted({parse_number(m.group(1)) or 0.0 for m in BATTERY.finditer(norm)} - {0.0})
    vocab = vocabulary()
    vec["trim"] = sorted(t for t in trims if (p := _trim_pattern(t, identity, vocab)) is not None and p.search(norm))
    return vec


def discriminating(vec: dict) -> bool:
    return any(vec.get(k) for k in DISCRIMINATING)


def _union(vectors: list[dict]) -> dict:
    out = _empty()
    for vec in vectors:
        for key, value in vec.items():
            if key == "model_code":
                out[key] = out[key] or bool(value)
            else:
                out[key] = sorted(set(out[key]) | set(value))
    return out


# --- catalog assignment (family level, target-independent) ------------------------------------------------------------

def _entry_tolerance(entry: dict, entries: list[dict]) -> float:
    """R3 for a catalog entry: min(3 %, half the relative gap to the nearest other power of its body / propulsion /
    drivetrain siblings)."""
    p = entry["parts"]
    power = p["power"]
    if not power:
        return 0.03
    others = {e["parts"]["power"] for e in entries if e["parts"]["power"] and e["parts"]["power"] != power
              and (e["parts"]["body"], e["parts"]["propulsion"], e["parts"]["drivetrain"])
              == (p["body"], p["propulsion"], p["drivetrain"])}
    if not others:
        return 0.03
    return min(0.03, 0.5 * min(abs(o - power) for o in others) / power)


def _compatible(entry: dict, vec: dict, entries: list[dict]) -> bool:
    """Can this catalog entry be the variant the identity vector names? (An empty key part is compatible.)"""
    p = entry["parts"]
    if vec["drivetrain"] and p["drivetrain"] and p["drivetrain"] not in vec["drivetrain"]:
        return False
    if vec["power"] and p["power"]:
        tol = _entry_tolerance(entry, entries)
        if not any(_close(pw, p["power"], tol) for pw in vec["power"]):
            return False
    if vec["displacement"] and p["displacement_l"]:
        if not any(abs(d - float(p["displacement_l"])) <= 0.06 for d in vec["displacement"]):
            return False
    if vec["trim"] and entry["trims"] and not set(vec["trim"]) & set(entry["trims"]):
        return False
    if vec["body"] and p["body"] and p["body"] not in vec["body"]:
        return False
    if vec["propulsion"] and p["propulsion"] and p["propulsion"] not in vec["propulsion"]:
        return False
    return True


def _vectors_compatible(a: dict, b: dict) -> bool:
    """Two identity vectors can describe the same variant (no stated dimension contradicts)."""
    for key in ("drivetrain", "trim"):
        if a[key] and b[key] and not set(a[key]) & set(b[key]):
            return False
    if a["displacement"] and b["displacement"] and not any(abs(x - y) <= 0.06 for x in a["displacement"]
                                                           for y in b["displacement"]):
        return False
    return True


def assign(vec: dict, entries: list[dict], inventory: list[dict]) -> dict:
    """Catalog candidates of a region before / after elimination with the document inventory, and its assignment."""
    complete = [e for e in entries if e["complete"]]
    before = [e for e in complete if _compatible(e, vec, entries)]
    blocking = [e["key"] for e in entries if not e["complete"] and _compatible(e, vec, entries)]
    after, unexplained = list(before), []
    if not vec["power"] and len({e["parts"]["power"] for e in before}) > 1:
        # elimination: the powers the document states for regions that may be this one
        powers = sorted({p for item in inventory if _vectors_compatible(item, vec) for p in item["power"]})
        if powers:
            after = [e for e in before if e["parts"]["power"]
                     and any(_close(p, e["parts"]["power"], _entry_tolerance(e, entries)) for p in powers)]
            unexplained = [p for p in powers if not any(
                e["parts"]["power"] and _close(p, e["parts"]["power"], _entry_tolerance(e, entries))
                for e in complete if _compatible(e, {**vec, "power": [p]}, entries))]
    status = "unresolved"
    if not entries:
        status = "no_catalog"
    elif len(after) == 1 and not blocking and not unexplained:
        status = "assigned"
    return {"candidates_before": [e["key"] for e in before], "candidates_after": [e["key"] for e in after],
            "incomplete_candidates": blocking, "unexplained_powers": unexplained, "status": status,
            "assignment": after[0]["key"] if status == "assigned" else None}


# --- building the map -------------------------------------------------------------------------------------------------

def _cells(rows: list[list]) -> list[list[str]]:
    return [[str(c or "").strip() for c in row] for row in rows]


def _identity_row(label: str, row_terms) -> bool:
    norm = normalize_text(label)
    return bool(label) and len(label) <= 60 and row_terms is not None and bool(row_terms.search(norm))


def _region(rid: str, kind: str, text: str, vec: dict, entries: list[dict], **location: Any) -> dict:
    return {"id": rid, "kind": kind, "text": re.sub(r"\s+", " ", text).strip()[:300], "identity": vec, **location}


def build_variant_map(*, text: str, identity: TargetIdentity, tables: list[dict] | None = None,
                      html: str | None = None, body_text: str | None = None, zone: str = "",
                      dictionary=None, index: dict | None = None) -> dict:
    """The document's variant map for the target's manufacturer / family / model year. Deterministic: the same inputs
    always give the same map (regions in document order, sorted keys)."""
    vocab = vocabulary()
    entries = catalog_family_entries(identity, index)
    trims = _catalog_trims(entries)
    row_terms = getattr(dictionary, "column_identity_rows", None)
    trim_header = getattr(dictionary, "trim_header", None)
    any_alias = getattr(dictionary, "any_alias", None)
    other = _other_family_pattern(identity)

    def about_other(chunk: str) -> bool:
        norm = normalize_text(chunk)
        return bool(other is not None and other.search(norm)
                    and "target" not in _family_status(norm, identity, vocab))

    def vec_of(chunk: str) -> dict:
        return _empty() if about_other(chunk) else identity_vector(chunk, identity, trims)

    regions: list[dict] = []
    inventory: list[dict] = []
    # identity zone
    zone_vec = vec_of(zone)
    if discriminating(zone_vec):
        inventory.append({"source": "identity_zone", **zone_vec})
    # tables: variant columns and variant rows
    out_tables: list[dict] = []
    for t, table in enumerate(tables or []):
        rows = _cells(table.get("rows") or [])
        width = max((len(r) for r in rows), default=0)
        entry = {"index": t, "rows": rows, "columns": {}, "row_regions": {}}
        if width >= 3:
            for j in range(1, width):
                parts = []
                for r, row in enumerate(rows):
                    if len(row) != width or j >= len(row) or not row[j]:
                        continue
                    label = row[0]
                    if (r == 0 and (not label or _contains(trim_header, label))) or _identity_row(label, row_terms):
                        parts.append(f"{label} {row[j]}".strip())
                chunk = " | ".join(parts)
                vec = vec_of(chunk)
                if discriminating(vec):
                    rid = f"table:{t}:col:{j}"
                    regions.append(_region(rid, "table_column", chunk, vec, entries, table_index=t, column_index=j))
                    entry["columns"][str(j)] = rid
                    inventory.append({"source": rid, **vec})
        for r, row in enumerate(rows):
            label = row[0] if row else ""
            if not label or _identity_row(label, row_terms) or (any_alias is not None
                                                               and any_alias.search(normalize_text(label))):
                continue
            vec = vec_of(label)
            if discriminating(vec):
                rid = f"table:{t}:row:{r}"
                regions.append(_region(rid, "table_row", label, vec, entries, table_index=t, row_index=r))
                entry["row_regions"][str(r)] = rid
                inventory.append({"source": rid, **vec})
        if entry["columns"] or entry["row_regions"]:
            out_tables.append(entry)
    # DOM pair groups
    pairs: list[dict] = []
    if html:
        pairs = _dom_groups(html, vec_of, row_terms, regions, inventory, entries)
    # lines and sections (the document's own text lines, as admission indexes them)
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    visible = None
    if body_text is not None:
        visible = {normalize_text(ln.strip()) for ln in body_text.splitlines() if ln.strip()}
    line_regions: dict[str, str] = {}
    sections: list[dict] = []
    heads: list[tuple[int, dict]] = []
    # an HTML table's cells are lines of the flattened text: they belong to their table's regions, never to a line /
    # section of their own
    cell_lines = {normalize_text(c) for table in tables or [] for row in _cells(table.get("rows") or []) for c in row
                  if c} if html else set()
    # HTML: a section starts only at a real heading element (a <p> "G6 AWD 486 hp" is a fact, not a heading)
    dom_headings = _dom_headings(html) if html else None
    aliases = (vocab.get("model_families") or {}).get(identity.family or "") or [identity.family or ""]
    family = "|".join(r"[\s\-]*".join(re.escape(x) for x in normalize_text(a).split()) for a in aliases if a)
    qualified = re.compile(rf"(?<![\wא-ת])(?:{family})[\s\-]+[a-zא-ת][\w-]*") if family else None
    for i, line in enumerate(lines):
        norm = normalize_text(line)
        if visible is not None and norm not in visible:
            continue                                         # page chrome (navigation, footer, menus)
        if norm in cell_lines:
            continue
        vec = vec_of(line)
        label = re.split(r"(?<![\w.])\d", norm, maxsplit=1)[0]
        if not discriminating(vec):
            if vec["battery"]:
                inventory.append({"source": f"line:{i}", **vec})
            continue
        inventory.append({"source": f"line:{i}", **vec})     # every variant mention about the target family
        has_alias = any_alias is not None and bool(any_alias.search(norm))
        if len(line) <= HEADING_MAX_CHARS and not has_alias and not NUMBER.search(IDENTITY_NUMBERS.sub(" ", norm)) \
                and (dom_headings is None or norm.strip() in dom_headings):
            heads.append((i, vec))
        label_vec = vec_of(label)
        if discriminating(label_vec) and not _identity_row(label.strip(" :|-"), row_terms):
            rid = f"line:{i}"
            regions.append(_region(rid, "line", label, label_vec, entries, line_index=i))
            line_regions[str(i)] = rid
    head_lines = {i for i, _ in heads}
    for n, (i, vec) in enumerate(heads):
        end = min(len(lines), i + 1 + SECTION_MAX_LINES)
        own = {m.group(0) for m in qualified.finditer(normalize_text(lines[i]))} if qualified else set()
        for k in range(i + 1, end):
            named = {m.group(0) for m in qualified.finditer(normalize_text(lines[k]))} if qualified else set()
            # the next variant heading, or a line naming the family with another word ("G6 PRO"), ends the section
            if k in head_lines or named - own:
                end = k
                break
        inner = [vec_of(lines[k]) for k in range(i + 1, end)]
        mixed = any(discriminating(v) and not _vectors_compatible(v, vec) for v in inner)
        if mixed or end <= i + 1:
            continue
        rid = f"section:{i}"
        regions.append(_region(rid, "section", lines[i], vec, entries, line_start=i + 1, line_end=end))
        sections.append({"region": rid, "start": i + 1, "end": end})
        inventory.append({"source": rid, **vec})
    # the whole document: its region identity is the inventory's union (one variant, or unresolved)
    doc_vec = _union([{k: v for k, v in item.items() if k != "source"} for item in inventory])
    regions.append(_region("document", "document", zone, doc_vec, entries))
    clean_inventory = [{k: v for k, v in item.items() if k != "source"} for item in inventory]
    for region in regions:
        vec = region["identity"]
        if region["kind"] == "document" and (len(vec["drivetrain"]) > 1 or len(vec["power"]) > 1
                                             or len(vec["displacement"]) > 1):
            region.update({"candidates_before": [], "candidates_after": [], "incomplete_candidates": [],
                           "unexplained_powers": [], "status": "mixed", "assignment": None})
            continue
        region.update(assign(vec, entries, clean_inventory) if discriminating(vec) else
                      {"candidates_before": [], "candidates_after": [], "incomplete_candidates": [],
                       "unexplained_powers": [], "status": "no_identity", "assignment": None})
    return {"version": VARIANT_MAP_VERSION,
            "family_key": "|".join(str(x) for x in (identity.manufacturer, identity.family, identity.year)),
            "catalog": {"entries": len(entries), "incomplete": [e["key"] for e in entries if not e["complete"]]},
            "inventory": inventory, "regions": regions, "tables": out_tables, "pairs": pairs,
            "line_regions": line_regions, "sections": sections}


def _dom_headings(html: str) -> set[str]:
    from .structure_harvest import HEADINGS, clean_soup

    try:
        soup = clean_soup(html)
    except Exception:  # noqa: BLE001
        return set()
    return {normalize_text(re.sub(r"\s+", " ", h.get_text(" ", strip=True))).strip() for h in soup.find_all(HEADINGS)}


def _contains(pattern, text: str) -> bool:
    return bool(pattern is not None and text and pattern.search(normalize_text(text)))


def _dom_groups(html: str, vec_of, row_terms, regions: list[dict], inventory: list[dict],
                entries: list[dict]) -> list[dict]:
    """DOM pair groups (src/structure_harvest): a repeated group / definition list whose heading or identity pairs name
    a variant becomes a region; every pair of the group points to it."""
    from .structure_harvest import REPEAT_MIN, _heading_before, clean_soup, raw_pairs

    try:
        found = raw_pairs(clean_soup(html))
    except Exception:  # noqa: BLE001 - a broken page has no DOM regions
        return []
    groups: dict[tuple, list[dict]] = {}
    for p in found:
        groups.setdefault((p["parent"], p["signature"]), []).append(p)
    out: list[dict] = []
    n = 0
    for members in groups.values():
        if not (members[0]["kind"] == "definition_list" or len(members) >= REPEAT_MIN):
            continue
        heading = _heading_before(members[0]["node"]) or ""
        idents = [f"{p['label']} {p['value']}" for p in members if _identity_row(p["label"], row_terms)]
        chunk = " | ".join([heading, *idents]) if heading else " | ".join(idents)
        vec = vec_of(chunk)
        if not discriminating(vec):
            continue
        rid = f"group:{n}"
        n += 1
        regions.append(_region(rid, "dom_group", chunk, vec, entries))
        inventory.append({"source": rid, **vec})
        out += [{"label": p["label"], "value": p["value"], "group": rid} for p in members]
    return out


def map_key(identity: TargetIdentity, dictionary=None, index: dict | None = None) -> str:
    """Derived-cache name part: the map version, the target family / year, its catalog entries, the identity vocabulary
    and the identity row terms (any change gives a new map)."""
    entries = catalog_family_entries(identity, index)
    blob = json.dumps([VARIANT_MAP_VERSION, identity.manufacturer, identity.family, identity.year,
                       [[e["key"], e["trims"], e["complete"]] for e in entries], vocabulary(),
                       getattr(getattr(dictionary, "column_identity_rows", None), "pattern", None),
                       getattr(getattr(dictionary, "trim_header", None), "pattern", None),
                       identity.model_code_tokens], ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def document_variant_map(cache, doc, identity: TargetIdentity, dictionary=None, zone: str = "") -> dict:
    """The cached variant map of one document (derived_variant_map_<key>.json; built once per document and target
    family). `doc` is evidence_admission.DocumentText."""
    from .tools.extract import document_tables

    meta = doc.meta or {}
    is_html = meta.get("doc_type") == "html" or meta.get("kind") == "rendered"

    def compute() -> dict:
        html = cache.read_body(doc.document_id).decode("utf-8", errors="replace") if is_html else None
        try:
            tables = document_tables(cache, doc.document_id, meta, html)
        except Exception:  # noqa: BLE001 - a broken table extraction leaves the other regions
            tables = []
        return build_variant_map(text=doc.text, identity=identity, tables=tables, html=html,
                                 body_text=doc.body_text, zone=zone, dictionary=dictionary)

    return cache.derived(doc.document_id, f"variant_map_{map_key(identity, dictionary)}", compute)[0]


# --- the verdict of one region for the target -------------------------------------------------------------------------

def _target_parts(identity: TargetIdentity) -> dict:
    return {"drivetrain": identity.drivetrain, "power": power_bucket(identity.power_hp), "body": identity.body,
            "propulsion": identity.propulsion,
            "displacement_l": None if identity.displacement_l is None else f"{float(identity.displacement_l):.1f}"}


def region_verdict(region: dict, identity: TargetIdentity, doc_statuses: dict | None = None) -> dict:
    """{status: target | other_variant | unresolved | none, contradicts?, reason} of one region for the target."""
    from .document_binding import power_tolerance

    vec = region["identity"]
    if region.get("status") in ("no_identity",) or not discriminating(vec):
        return {"status": "none", "reason": "no_identity"}
    if region.get("status") == "mixed":
        return {"status": "unresolved", "reason": "several_variants"}
    veto_dims = _veto_dims(identity)
    # explicit contradiction by the region's own words
    if vec["drivetrain"] and identity.drivetrain:
        if identity.drivetrain not in vec["drivetrain"]:
            return {"status": "other_variant", "contradicts": "drivetrain", "reason": "drivetrain"}
        if len(vec["drivetrain"]) > 1:
            return {"status": "unresolved", "reason": "drivetrain_mixed"}
    if vec["power"] and identity.power_hp and "power" in veto_dims:
        tol = power_tolerance(identity)["tolerance"]
        hits = [p for p in vec["power"] if _close(p, identity.power_hp, tol)]
        if not hits:
            return {"status": "other_variant", "contradicts": "power", "reason": "power"}
        if len(hits) != len(vec["power"]):
            return {"status": "unresolved", "reason": "power_mixed"}
    if vec["displacement"] and identity.displacement_l:
        target = identity.displacement_cc / 1000 if identity.displacement_cc else identity.displacement_l
        if not any(abs(d - target) <= 0.06 for d in vec["displacement"]):
            return {"status": "other_variant", "contradicts": "displacement", "reason": "displacement"}
    if vec["model_code"]:
        return {"status": "target", "reason": "model_code"}
    if region.get("status") == "no_catalog":
        full = bool(vec["power"]) and "power" in veto_dims and (not identity.drivetrain or vec["drivetrain"])
        if identity.displacement_l is not None:
            full = full and bool(vec["displacement"])
        return {"status": "target", "reason": "full_identity_no_catalog"} if full else \
            {"status": "unresolved", "reason": "no_catalog_partial_identity"}
    if region.get("status") != "assigned":
        return {"status": "unresolved", "reason": "catalog_" + ("incomplete" if region.get("incomplete_candidates")
                                                                else "unexplained_power"
                                                                if region.get("unexplained_powers")
                                                                else f"{len(region.get('candidates_after') or [])}"
                                                                     "_candidates")}
    from .document_binding import parse_catalog_key

    parts = parse_catalog_key(region["assignment"]) or {}
    target = _target_parts(identity)
    for dim, key in (("drivetrain", "drivetrain"), ("power", "power"), ("displacement", "displacement_l")):
        if target[key] not in (None, "") and parts.get(key) not in (None, "") and parts[key] != target[key]:
            return {"status": "other_variant", "contradicts": dim, "reason": f"catalog_{dim}"}
    for key in ("body", "propulsion"):
        if target[key] not in (None, "") and parts.get(key) not in (None, "") and parts[key] != target[key]:
            return {"status": "unresolved", "reason": f"catalog_{key}"}
    return {"status": "target", "reason": "catalog_assignment"}


# --- one fact ---------------------------------------------------------------------------------------------------------

def _value_numbers(value: Any) -> list[float]:
    from .typed_values import numbers_in

    return [float(n) for n in numbers_in(value)]


def _cell_has(cell: str, value: Any) -> bool:
    from .evidence_admission import squash

    wanted = _value_numbers(value)
    if wanted:
        got = [parse_number(m.group(1)) for m in NUMBER.finditer(normalize_text(cell))]
        return all(any(g is not None and abs(g - w) <= max(1e-6, 1e-6 * abs(w)) for g in got) for w in wanted)
    probe = squash(value)
    return bool(probe) and f" {probe} " in f" {squash(cell)} "


def hedged(text: str, value: Any) -> bool:
    """Is the (first) number of the value hedged in this text ("עד 451 kW", "from 199,990", "550 km*")?"""
    norm = normalize_text(text or "")
    wanted = _value_numbers(value)[:1]
    for m in NUMBER.finditer(norm):
        n = parse_number(m.group(1))
        if n is None or (wanted and abs(n - wanted[0]) > max(1e-6, 1e-6 * abs(wanted[0]))):
            continue
        if HEDGE_BEFORE.search(norm[max(0, m.start() - 14):m.start()]) or HEDGE_AFTER.match(norm[m.end():m.end() + 12]):
            return True
    return False


def fact_region(vmap: dict | None, identity: TargetIdentity, *, value: Any, fragment: str, clause: str,
                source_lines: list[str], line_index: int | None, matching: list[dict],
                field_values: int, doc_statuses: dict | None = None) -> dict | None:
    """The DVM decision of one fact: {status, region_id, region_kind, identity, candidates_before, candidates_after,
    reason, ...} (status target | other_variant | shared | unresolved | none), or None without a map.
    `field_values`: how many materially different values the document's own harvest has for this field."""
    from .evidence_admission import squash

    if not vmap:
        return None
    regions = {r["id"]: r for r in vmap.get("regions") or []}
    verdicts: dict[str, dict] = {}

    def verdict(rid: str) -> dict:
        if rid not in verdicts:
            verdicts[rid] = region_verdict(regions[rid], identity, doc_statuses)
        return verdicts[rid]

    frag = " " + squash(fragment) + " "
    levels: list[tuple[str, list[str]]] = []
    shared_row = None
    disagree = False
    # 1. table cells
    cells: list[tuple[dict, int, int]] = []
    tables = {t["index"]: t for t in vmap.get("tables") or []}
    for cand in matching:
        t, r = cand.get("table_index"), cand.get("row_index")
        table = tables.get(t)
        if table is None or r is None or r >= len(table["rows"]):
            continue
        row = table["rows"][r]
        cells += [(table, r, j) for j in range(1, len(row)) if row[j] and _cell_has(row[j], value)]
    if not cells:
        for table in tables.values():
            for r, row in enumerate(table["rows"]):
                if row and row[0] and f" {squash(row[0])} " in frag:
                    cells += [(table, r, j) for j in range(1, len(row)) if row[j] and _cell_has(row[j], value)]
    cell_regions: list[str] = []
    for t, r, j in dict.fromkeys((table["index"], r, j) for table, r, j in cells):
        table = tables[t]
        columns = table["columns"]
        if str(j) in columns:
            cell_regions.append(columns[str(j)])
        if str(r) in table["row_regions"]:
            cell_regions.append(table["row_regions"][str(r)])
        if columns:
            row = table["rows"][r]
            # Fail closed: a row is shared only when every non-empty data column is a DVM-recognised variant
            # column and every one states this value. An unrecognised extra column may be another variant, so it
            # must block "shared" rather than being silently ignored.
            data_columns = [j2 for j2 in range(1, len(row)) if row[j2]]
            mapped_columns = [int(k) for k in columns if int(k) < len(row) and row[int(k)]]
            if len(mapped_columns) == len(data_columns) and len(mapped_columns) > 1 \
                    and all(_cell_has(row[j2], value) for j2 in data_columns):
                shared_row = f"{table['index']}:{r}"
            # another column of the target with a different value in the same row: no silent pick
            targets = [k for k in columns if verdict(columns[k])["status"] == "target"]
            if str(j) in targets and any(int(k) < len(row) and not _cell_has(row[int(k)], value) for k in targets):
                disagree = True
    levels.append(("cell", list(dict.fromkeys(cell_regions))))
    # 2. DOM groups
    groups = [p["group"] for p in vmap.get("pairs") or []
              if f" {squash(p['label'])} " in frag and _cell_has(p["value"], value)]
    levels.append(("group", list(dict.fromkeys(groups))))
    # 3. the line, 4. its section
    line_regions = vmap.get("line_regions") or {}
    levels.append(("line", [line_regions[str(line_index)]] if line_index is not None
                   and str(line_index) in line_regions else []))
    levels.append(("section", [s["region"] for s in vmap.get("sections") or []
                               if line_index is not None and s["start"] <= line_index < s["end"]]))
    levels.append(("document", ["document"] if "document" in regions else []))
    decision: dict = {"status": "none", "reason": "no_region"}
    for level, rids in levels:
        judged = [(rid, verdict(rid)) for rid in rids]
        judged = [(rid, v) for rid, v in judged if v["status"] != "none"]
        if not judged:
            continue
        kinds = {v["status"] for _, v in judged}
        rid, v = judged[0]
        if kinds == {"target"}:
            status = "target"
        elif kinds == {"other_variant"} and len({v2.get("contradicts") for _, v2 in judged}) >= 1:
            status = "other_variant"
        else:
            status = "unresolved"
            rid, v = next(((r2, v2) for r2, v2 in judged if v2["status"] == "unresolved"), judged[0])
        if level == "document" and status != "target":
            break                                   # the document's own verdict is only a positive proof
        region = regions[rid]
        decision = {"status": status, "region_id": rid, "region_kind": region["kind"], "level": level,
                    "reason": v.get("reason"), "contradicts": v.get("contradicts"),
                    "identity": region["identity"], "identity_text": region.get("text"),
                    "candidates_before": region.get("candidates_before"),
                    "candidates_after": region.get("candidates_after"),
                    "assignment": region.get("assignment"), "regions": [r for r, _ in judged]}
        break
    if disagree and decision.get("status") == "target":
        decision.update({"status": "unresolved", "reason": "target_regions_disagree"})
    inventory_target = any(verdict(rid)["status"] == "target" for rid in regions)
    in_region = any(verdict(r)["status"] != "none" for level, rids in levels if level != "document" for r in rids)
    if decision.get("status") in ("none", "unresolved") or shared_row:
        context = " ".join([clause or "", fragment or "", *(source_lines or [])])
        qualifier = discriminating(identity_vector(f"{clause or ''} {fragment or ''}", identity, []))
        if shared_row and not hedged(context, value):
            decision = {"status": "shared", "region_id": f"table:{shared_row}", "region_kind": "table_shared_row",
                        "reason": "identical_in_every_variant_column", "inventory_contains_target": inventory_target}
        elif not in_region and field_values == 1 and not qualifier and not hedged(context, value) \
                and not cells and not groups:
            decision = {"status": "shared", "region_id": "document", "region_kind": "document_shared",
                        "reason": "stated_once_without_variant_qualifier",
                        "inventory_contains_target": inventory_target}
        if decision.get("status") == "shared" and not inventory_target:
            decision.update({"status": "unresolved", "reason": "inventory_without_target"})
    decision["map_version"] = vmap.get("version")
    return decision
