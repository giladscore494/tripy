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

variant-map-v2 (PR #42, importer pages), each recorded on the region / fact:

    F1 charging power   a kW stated in a charging context (טעינה, DC / AC, a charger, V2L) is never a power identity
                        (document_binding.charging_context); a power a region states that no catalog entry explains
                        blocks that region only (`unexplained_powers`); in another region's elimination it is left
                        out (`ignored_powers`)
    F2 tab labels       a heading, then exactly N short variant labels (<= TAB_LABEL_MAX_TOKENS words, no digit),
                        then the N repeated groups under it with one label set: label i is group i's identity
                        (`tab_label`, rule `tab_labels`); the heading stays in the text, never an identity key
    F3 trim aliases     catalog trims match abbreviated (catalog_trim_matches: prefix / join / the vocabulary's
                        catalog_trim_aliases, at least one token whole, the longest match wins); a trim no catalog
                        entry has at the text's drivetrain is dropped (`trim_inconsistent`); a trim at several
                        technical variants narrows to their union (never a pick)
    F5 model codes      the regulatory "קוד דגם | תיאור דגם" table: a row region per code (`gov_model_code`); a
                        region with the target's degem_cd is the target's market trim (rule `gov_model_code`); a
                        complete table that lists the target's technical variant under other codes only gives the
                        document `market_trim_not_offered` (market_trim_offer)
    groups              a label / value stated in several repeated groups is the target's when a target group states
                        it and no target group states another value (`region_statuses`)

variant-map-v3 (PR #43, combustion / hybrid identity):

    designation         version designations ("55 TFSI e", "40TFSI", "4xe", "P400"; identity vocabulary
                        `variant_designations`) are part of a region's identity; regions with different designations are
                        never the same variant; the litres written before one ("2.0 40TFSI") are a displacement, and a
                        designation's digits are part of a line's label / heading, never a value
    gearbox             a gearbox named with the version (DSG, S tronic, ידני) that contradicts the target's government
                        transmission leaves the region unresolved
    designation link    a fact no region decides whose clause / quote / section heading names exactly one designation
                        takes the verdict of that designation's regions: target (`designation_region`) when one is assigned
                        to the target by the catalog with its own power and none is another variant
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .candidate_harvest import NUMBER, normalize_text, parse_number
from .document_binding import (TargetIdentity, _close, _family_status, _other_family_pattern, _veto_dims,
                               catalog_family_entries, catalog_key, designation_spans, designations, mentions,
                               power_bucket, vocabulary)

VARIANT_MAP_VERSION = "variant-map-v3"
SECTION_MAX_LINES = 40
HEADING_MAX_CHARS = 60
BATTERY = re.compile(r"(?<![\d.,])(\d{2,3}(?:[.,]\d)?)\s*(?:kwh|קוט\"ש|קוט\"שׁ)(?![a-z])")
# a value whose own words hedge it is never shared across variants ("up to 451 kW", "החל מ-", "עד", footnote markers)
HEDGE_BEFORE = re.compile(r"(?:(?<![\wא-ת])(?:עד|החל\s*מ-?|מ-|up\s+to|from|starting(?:\s+at|\s+from)?|approx\.?|"
                          r"approximately|about|ca\.|כ-|כ־|~)\s*)$")
HEDGE_AFTER = re.compile(r"^\s*(?:[a-zא-ת\"'/%°.\d]{0,8}\s*)?(?:\*|[¹²³⁴⁵⁶⁷⁸⁹⁰]|\(\d\)|\[\d\])")
DISCRIMINATING = ("drivetrain", "power", "displacement", "trim", "model_code", "gov_model_code", "designation")
# F2: a tab label of a repeated spec group is short and states no value
TAB_LABEL_MAX_TOKENS = 6
# F3: document words for catalog trim matching ("+" is the word "plus": "Core+" is CORE PLUS)
TRIM_TOKEN = re.compile(r"[a-z0-9א-ת]+|\+")
# numbers a variant heading may hold: power, battery, model year ("AWD 486 כ"ס", "87.5 kWh", "G6 2026")
IDENTITY_NUMBERS = re.compile(r"(?<![\d.,])\d{2,4}(?:[.,]\d)?\s*(?:kwh|kw|hp|ps|bhp|כ\"ס|קוט\"ש)(?![a-z])"
                              r"|(?<!\d)20[0-3]\d(?!\d)")


# --- identity vectors -------------------------------------------------------------------------------------------------

def _empty() -> dict:
    return {"drivetrain": [], "power": [], "battery": [], "displacement": [], "trim": [], "body": [], "propulsion": [],
            "model_code": False, "gov_model_code": [], "designation": [], "gearbox": []}


def _catalog_trims(entries: list[dict]) -> list[str]:
    return sorted({t for e in entries for t in e["trims"] if t})


def _trim_pattern(trim: str, identity: TargetIdentity, vocab: dict):
    """A catalog trim made only of generic words (MAX, PRO, BASE EDITION) as a phrase qualified by the family ("G6
    MAX"), as the #34 qualified-phrase rule. None for any other trim (catalog_trim_matches reads those)."""
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
    return None


def _words(text: str) -> list[tuple[str, int, int]]:
    """(word, start, end) of a normalized text for trim matching: letters / digits runs, "+" as "plus"."""
    return [("plus" if m.group(0) == "+" else m.group(0), m.start(), m.end())
            for m in TRIM_TOKEN.finditer(normalize_text(text or ""))]


def _catalog_tokens(trim: str) -> list[str]:
    return [w for w, _, _ in _words(trim)]


def _token_ends(token: str, doc: list[str], p: int, aliases: dict, first: bool,
                catalog_words: set[str]) -> list[tuple[int, bool]]:
    """(end, whole) of each way a catalog trim token can match from doc word p: the word itself (whole), a word it is
    a prefix of (not whole; never a number; a 2-letter non-first token may prefix only another token that
    exists in this model family's catalog, so BUSINESS ED ~ BUSINESS EDI but PR !~ Price), the join of 2-3 words
    ("BLACKEDITION" = "Black Edition", whole) or a listed alias
    (catalog_trim_aliases: token "LR" = "Long Range" or full trim "LR PR" = "Long Range Pro", whole)."""
    if p >= len(doc):
        return []
    word = doc[p]
    ends: set[tuple[int, bool]] = set()
    if token == word:
        ends.add((p + 1, True))
    elif not token.isdigit() and not word.isdigit() and word.startswith(token) \
            and (len(token) >= 3 or (len(token) == 2 and not first and word in catalog_words)):
        ends.add((p + 1, False))
    joined = word
    for m in (2, 3):
        if p + m > len(doc) or not joined.isalpha():
            break
        joined += doc[p + m - 1]
        if joined == token:
            ends.add((p + m, True))
    for alias in aliases.get(token) or ():
        words = alias.split()
        if words and doc[p:p + len(words)] == words:
            ends.add((p + len(words), True))
    return sorted(ends)


def _match_from(tokens: list[str], doc: list[str], p: int, aliases: dict,
                catalog_words: set[str]) -> int | None:
    """The longest end of the catalog tokens matched, in order, from doc word p, with at least one token matched
    whole (a word, a join or an alias): a prefix alone never names a trim ("SUN" is not "sunroof"). None: no match."""
    states = {(p, False)}
    for i, token in enumerate(tokens):
        states = {(end, whole or seen) for start, seen in states
                  for end, whole in _token_ends(token, doc, start, aliases, first=i == 0,
                                                      catalog_words=catalog_words)}
        if not states:
            return None
    ends = [end for end, seen in states if seen]
    return max(ends) if ends else None


def catalog_trim_matches(text: str, trims: list[str], identity: TargetIdentity,
                         vocab: dict | None = None) -> list[dict]:
    """F3: the catalog trims a text names: [{trim, start, end, form}] (word positions). A trim of generic words only
    (MAX, PRO) needs the family before it ("G6 MAX", unchanged); any other trim matches when every catalog token
    is, in order, a prefix of the next document words ("CORE PERF" = "Core Performance", "STAND RANGE" = "Standard
    Range", "PERF TB" = "Performance TB"), a join of them ("BLACKEDITION" = "Black Edition") or an alias of
    catalog_trim_aliases ("LR PR" = "Long Range Pro"), and at least one token matches whole (never by prefixes
    alone: "SUN" is not "sunroof"). The LONGEST match wins: a trim whose words lie inside a longer
    trim's match is dropped ("Core Performance" is CORE PERF, never also CORE). Deterministic; no value is read."""
    vocab = vocabulary() if vocab is None else vocab
    norm = normalize_text(text or "")
    words = _words(norm)
    doc = [w for w, _, _ in words]
    generic = {w.lower() for w in vocab.get("generic_trim_words") or []}
    aliases = {k.lower(): [normalize_text(a) for a in v] for k, v in (vocab.get("catalog_trim_aliases") or {}).items()
               if not k.startswith("_") and isinstance(v, list)}
    catalog_words = {token for trim in trims for token in _catalog_tokens(trim)}
    found: list[dict] = []
    for trim in trims:
        tokens = _catalog_tokens(trim)
        if not tokens:
            continue
        if all(t in generic for t in tokens):
            pattern = _trim_pattern(trim, identity, vocab)
            for m in pattern.finditer(norm) if pattern is not None else ():
                inside = [i for i, (_, a, b) in enumerate(words) if a >= m.start() and b <= m.end()]
                if len(inside) >= len(tokens):
                    found.append({"trim": trim, "start": inside[-len(tokens)], "end": inside[-1] + 1,
                                  "form": "qualified_phrase"})
            continue
        # A full-trim alias is exact and scoped to this complete catalog trim. This is safer than teaching a short
        # token such as PR to prefix-match every word beginning with "pr" across every model family.
        for alias in aliases.get(normalize_text(trim)) or ():
            alias_words = alias.split()
            for p in range(len(doc) - len(alias_words) + 1):
                if alias_words and doc[p:p + len(alias_words)] == alias_words:
                    found.append({"trim": trim, "start": p, "end": p + len(alias_words), "form": "phrase_alias"})
        for p in range(len(doc)):
            end = _match_from(tokens, doc, p, aliases, catalog_words)
            if end is not None:
                exact = doc[p:end] == tokens
                found.append({"trim": trim, "start": p, "end": end, "form": "exact" if exact else "alias_prefix"})
    kept = [m for m in found if not any(o["start"] <= m["start"] and o["end"] >= m["end"]
                                        and o["end"] - o["start"] > m["end"] - m["start"] for o in found)]
    return sorted({(m["start"], m["end"], m["trim"]): m for m in kept}.values(),
                  key=lambda m: (m["start"], m["end"], m["trim"]))


def _gov_codes(norm: str, vocab: dict) -> list[int]:
    """Government model codes a text states with their label ("קוד דגם 35", "קוד דגם: 31")."""
    heads = (vocab.get("model_code_table") or {}).get("code_headers") or []
    alternation = "|".join(re.escape(normalize_text(h)) for h in sorted(heads, key=len, reverse=True) if h)
    if not alternation:
        return []
    return sorted({int(m.group(1)) for m in re.finditer(rf"(?:{alternation})\s*[:|\-–]?\s*(\d{{1,4}})(?![\d.,])",
                                                         norm)})


def identity_vector(text: str, identity: TargetIdentity, trims: list[str], entries: list[dict] | None = None) -> dict:
    """What variant a text names: drivetrain keys, powers (hp; never a charging power), battery kWh, displacements,
    catalog trims (catalog_trim_matches; with `entries`, a trim no catalog entry of the text's drivetrain has is
    dropped: "Core" with AWD names no trim when CORE exists only as two-wheel drive), body / strong propulsion keys,
    whether it names the target's model code, and the government model codes it states ("קוד דגם 35")."""
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
    vec["designation"] = sorted(found["designation"])          # PR #43 (H4): "55tfsie", "40tfsi"
    vec["gearbox"] = sorted(found["gearbox"])
    vec["battery"] = sorted({parse_number(m.group(1)) or 0.0 for m in BATTERY.finditer(norm)} - {0.0})
    vocab = vocabulary()
    named = sorted({m["trim"] for m in catalog_trim_matches(norm, trims, identity, vocab)})
    if entries and vec["drivetrain"]:
        consistent = {t for e in entries for t in e["trims"]
                      if not e["parts"]["drivetrain"] or e["parts"]["drivetrain"] in vec["drivetrain"]}
        dropped = [t for t in named if t not in consistent]
        named = [t for t in named if t in consistent]
        if dropped:
            vec["trim_inconsistent"] = dropped        # telemetry: named, but not at the text's drivetrain
    vec["trim"] = named
    vec["gov_model_code"] = _gov_codes(norm, vocab)
    return vec


def discriminating(vec: dict) -> bool:
    return any(vec.get(k) for k in DISCRIMINATING)


def _union(vectors: list[dict]) -> dict:
    out = _empty()
    for vec in vectors:
        for key, value in vec.items():
            if key == "model_code":
                out[key] = out[key] or bool(value)
            elif key in out:
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
    for key in ("drivetrain", "trim", "designation", "gearbox"):
        if a.get(key) and b.get(key) and not set(a[key]) & set(b[key]):
            return False
    if a["displacement"] and b["displacement"] and not any(abs(x - y) <= 0.06 for x in a["displacement"]
                                                           for y in b["displacement"]):
        return False
    return True


def _explained(power: float, vec: dict, entries: list[dict], complete: list[dict]) -> bool:
    """Does a complete catalog entry compatible with the region (at this power) explain the power?"""
    return any(e["parts"]["power"] and _close(power, e["parts"]["power"], _entry_tolerance(e, entries))
               for e in complete if _compatible(e, {**vec, "power": [power]}, entries))


def assign(vec: dict, entries: list[dict], inventory: list[dict]) -> dict:
    """Catalog candidates of a region before / after elimination with the document inventory, and its assignment.

    A power the region ITSELF states that no compatible catalog entry explains blocks the region
    (`unexplained_powers`). A power another region states that no candidate explains only stays out of this region's
    elimination (`ignored_powers`, telemetry): an unexplained power blocks the region it occurs in, never the page."""
    complete = [e for e in entries if e["complete"]]
    before = [e for e in complete if _compatible(e, vec, entries)]
    blocking = [e["key"] for e in entries if not e["complete"] and _compatible(e, vec, entries)]
    after = list(before)
    unexplained = [p for p in vec["power"] if not _explained(p, vec, entries, complete)]
    ignored: list[float] = []
    if not vec["power"] and len({e["parts"]["power"] for e in before}) > 1:
        # elimination: the powers the document states for regions that may be this one
        powers = sorted({p for item in inventory if _vectors_compatible(item, vec) for p in item["power"]})
        explained = [p for p in powers if _explained(p, vec, entries, complete)]
        ignored = [p for p in powers if p not in explained]
        if explained:
            after = [e for e in before if e["parts"]["power"]
                     and any(_close(p, e["parts"]["power"], _entry_tolerance(e, entries)) for p in explained)]
    status = "unresolved"
    if not entries:
        status = "no_catalog"
    elif len(after) == 1 and not blocking and not unexplained:
        status = "assigned"
    out = {"candidates_before": [e["key"] for e in before], "candidates_after": [e["key"] for e in after],
           "incomplete_candidates": blocking, "unexplained_powers": unexplained, "status": status,
           "assignment": after[0]["key"] if status == "assigned" else None}
    if ignored:
        out["ignored_powers"] = ignored
    return out


# --- building the map -------------------------------------------------------------------------------------------------

def _cells(rows: list[list]) -> list[list[str]]:
    return [[str(c or "").strip() for c in row] for row in rows]


LABEL_UNIT = re.compile(r"[(\[)\]]\s*([^()\[\]\d]{1,12}?)\s*[)\](\[]\s*$")
BARE_NUMBER = re.compile(r"\s*\d{1,3}(?:,\d{3})*(?:[.,]\d+)?\s*")


def _with_label_unit(label: str, cell: str) -> str:
    """A bare number cell of a row whose label carries the unit ('הספק מרבי (כ"ס)' | 476) with that unit ("476 כ"ס"),
    so the identity reads it as the label states it; any other cell as is."""
    m = LABEL_UNIT.search(label or "")
    return f"{cell} {m.group(1).strip()}" if m and BARE_NUMBER.fullmatch(cell or "") else cell


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
        return _empty() if about_other(chunk) else identity_vector(chunk, identity, trims, entries)

    regions: list[dict] = []
    inventory: list[dict] = []
    # identity zone
    zone_vec = vec_of(zone)
    if discriminating(zone_vec):
        inventory.append({"source": "identity_zone", **zone_vec})
    # tables: variant columns and variant rows
    out_tables: list[dict] = []
    model_codes: list[dict] = []
    for t, table in enumerate(tables or []):
        rows = _cells(table.get("rows") or [])
        width = max((len(r) for r in rows), default=0)
        entry = {"index": t, "rows": rows, "columns": {}, "row_regions": {}}
        codes = _code_table(rows, vocab)
        if codes is not None:
            # F5: the regulatory "קוד דגם | תיאור דגם" table: one region per row, its identity = the description and
            # the government model code
            code_col, desc_col = codes
            listed = {"table_index": t, "complete": True, "rows": []}
            for r, row in enumerate(rows[1:], start=1):
                code = row[code_col] if code_col < len(row) else ""
                desc = row[desc_col] if desc_col < len(row) else ""
                if not re.fullmatch(r"\d{1,4}", code) or not desc:
                    listed["complete"] = False
                    continue
                vec = vec_of(desc)
                vec["gov_model_code"] = [int(code)]
                rid = f"table:{t}:row:{r}"
                regions.append(_region(rid, "model_code_row", f"{code} | {desc}", vec, entries, table_index=t,
                                       row_index=r, gov_model_code=int(code)))
                entry["row_regions"][str(r)] = rid
                inventory.append({"source": rid, **{k: v for k, v in vec.items() if k != "trim_inconsistent"}})
                listed["rows"].append({"code": int(code), "region": rid})
            listed["complete"] = listed["complete"] and len(listed["rows"]) >= 2
            model_codes.append(listed)
            out_tables.append(entry)
            continue
        if width >= 3:
            for j in range(1, width):
                parts = []
                for r, row in enumerate(rows):
                    if len(row) != width or j >= len(row) or not row[j]:
                        continue
                    label = row[0]
                    if (r == 0 and (not label or _contains(trim_header, label))) or _identity_row(label, row_terms):
                        parts.append(f"{label} {_with_label_unit(label, row[j])}".strip())
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
        label = _label_part(norm)
        if not discriminating(vec):
            if vec["battery"]:
                inventory.append({"source": f"line:{i}", **vec})
            continue
        inventory.append({"source": f"line:{i}", **vec})     # every variant mention about the target family
        has_alias = any_alias is not None and bool(any_alias.search(norm))
        if len(line) <= HEADING_MAX_CHARS and not has_alias and not NUMBER.search(_without_identity_numbers(norm)) \
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
                                             or len(vec["displacement"]) > 1 or len(vec["designation"]) > 1):
            region.update({"candidates_before": [], "candidates_after": [], "incomplete_candidates": [],
                           "unexplained_powers": [], "status": "mixed", "assignment": None})
            continue
        region.update(assign(vec, entries, clean_inventory) if discriminating(vec) else
                      {"candidates_before": [], "candidates_after": [], "incomplete_candidates": [],
                       "unexplained_powers": [], "status": "no_identity", "assignment": None})
    by_id = {r["id"]: r for r in regions}
    for listed in model_codes:
        for row in listed["rows"]:
            row["assignment"] = by_id[row["region"]].get("assignment")
    return {"version": VARIANT_MAP_VERSION,
            "family_key": "|".join(str(x) for x in (identity.manufacturer, identity.family, identity.year)),
            "catalog": {"entries": len(entries), "incomplete": [e["key"] for e in entries if not e["complete"]]},
            "inventory": inventory, "regions": regions, "tables": out_tables, "pairs": pairs,
            "line_regions": line_regions, "sections": sections, "model_codes": model_codes}


# litres written with a version name: "2.0 40TFSI", "3.0 TFSI", "1.5 ליטר"
VERSION_LITRES = re.compile(r"(?<![\d.,])[1-9]\.\d(?=\s*(?:\d{2}\s?-?(?:tfsi|tsi|tdi)|tfsi|tsi|tdi|ליטר|l\b))")


def _identity_spans(norm: str) -> list[tuple[int, int]]:
    """Spans of a normalized text that are part of a version name, not a value: designations ("55 tfsi e") and the
    litres written with them ("2.0 40tfsi")."""
    return [(a, b) for _, a, b in designation_spans(norm)] + [m.span() for m in VERSION_LITRES.finditer(norm)]


def _without_identity_numbers(norm: str) -> str:
    out = IDENTITY_NUMBERS.sub(" ", norm)
    for a, b in _identity_spans(out):
        out = out[:a] + " " * (b - a) + out[b:]
    return out


def _label_part(norm: str) -> str:
    """The label of a line: the text before its first value number (a number inside a version name such as "Q7 55
    TFSIe" or "2.0 40TFSI" is part of the label, never a value)."""
    spans = _identity_spans(norm)
    for m in re.finditer(r"(?<![\w.])\d", norm):
        if not any(a <= m.start() < b for a, b in spans):
            return norm[:m.start()]
    return norm


def _code_table(rows: list[list[str]], vocab: dict) -> tuple[int, int] | None:
    """(code column, description column) of a regulatory model-code table: a header row naming a model-code column
    and a model-description column (identity vocabulary `model_code_table`); None otherwise."""
    terms = vocab.get("model_code_table") or {}
    if not rows:
        return None

    def column(key: str) -> int | None:
        wanted = {normalize_text(t).strip() for t in terms.get(key) or []}
        hits = [j for j, cell in enumerate(rows[0]) if normalize_text(cell).strip() in wanted]
        return hits[0] if len(hits) == 1 else None
    code, desc = column("code_headers"), column("description_headers")
    return (code, desc) if code is not None and desc is not None and code != desc else None


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
    """DOM pair groups (src/structure_harvest): a repeated group / definition list whose heading, tab label or identity
    pairs name a variant becomes a region; every pair of the group points to it.

    F2 (tabbed / repeated spec groups): a heading followed by exactly N short variant labels ("Core RWD", "Core
    Performance AWD": <= TAB_LABEL_MAX_TOKENS words, no digit) and then by the N repeated groups under it, all with the
    same label set: label i is part of group i's identity, in document order. Otherwise nothing is inferred. The heading
    text stays in the region text but is no identity key of a labelled group."""
    from .structure_harvest import REPEAT_MIN, _heading_before, clean_soup, raw_pairs

    try:
        soup = clean_soup(html)
        found = raw_pairs(soup)
    except Exception:  # noqa: BLE001 - a broken page has no DOM regions
        return []
    groups: dict[tuple, list[dict]] = {}
    for p in found:
        groups.setdefault((p["parent"], p["signature"]), []).append(p)
    qualifying = [members for members in groups.values()
                  if members[0]["kind"] == "definition_list" or len(members) >= REPEAT_MIN]
    tabs = _tab_labels(qualifying)
    out: list[dict] = []
    n = 0
    for index, members in enumerate(qualifying):
        heading = _heading_before(members[0]["node"]) or ""
        idents = [f"{p['label']} {p['value']}" for p in members if _identity_row(p["label"], row_terms)]
        label = tabs.get(index)
        if label is not None:
            chunk = " | ".join([label, *idents])
            text = " | ".join([t for t in (heading, label) if t] + idents)
        else:
            chunk = text = " | ".join([heading, *idents]) if heading else " | ".join(idents)
        vec = vec_of(chunk)
        if not discriminating(vec):
            continue
        rid = f"group:{n}"
        n += 1
        extra = {"tab_label": label, "tab_heading": heading, "rule": "tab_labels"} if label is not None else {}
        regions.append(_region(rid, "dom_group", text, vec, entries, **extra))
        inventory.append({"source": rid, **vec})
        out += [{"label": p["label"], "value": p["value"], "group": rid} for p in members]
    return out


def _tab_labels(qualifying: list[list[dict]]) -> dict[int, str]:
    """{group index: its tab label} for the groups F2 applies to (see _dom_groups)."""
    from bs4 import NavigableString, Tag

    from .structure_harvest import HEADINGS

    by_heading: dict[int, tuple[Any, list[int]]] = {}
    for index, members in enumerate(qualifying):
        heading = members[0]["node"].find_previous(HEADINGS)
        if heading is not None:
            by_heading.setdefault(id(heading), (heading, []))[1].append(index)
    out: dict[int, str] = {}
    for heading, indexes in by_heading.values():
        if len(indexes) < 2:
            continue
        label_sets = [{normalize_text(p["label"]).strip() for p in qualifying[i]} for i in indexes]
        if any(ls != label_sets[0] for ls in label_sets[1:]):
            continue                                    # not repeated groups of one spec section
        stop = qualifying[indexes[0]][0]["node"]
        labels: list[str] = []
        for element in heading.next_elements:
            if element is stop:
                break
            if isinstance(element, Tag) and element.name in HEADINGS:
                labels = []
                break                                   # another heading before the groups: no tab row
            if isinstance(element, NavigableString) and heading not in element.parents:
                text = re.sub(r"\s+", " ", str(element)).strip()
                if text:
                    labels.append(text)
        if len(labels) != len(indexes):
            continue
        if any(len(label.split()) > TAB_LABEL_MAX_TOKENS or re.search(r"\d", label) for label in labels):
            continue
        out.update(dict(zip(indexes, labels)))
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
    if vec.get("gearbox") and identity.transmission and identity.transmission not in vec["gearbox"]:
        return {"status": "unresolved", "reason": "gearbox"}           # PR #43: a version sold with another gearbox
    codes = vec.get("gov_model_code") or []
    if codes and identity.gov_model_code is not None and codes == [identity.gov_model_code]:
        # F5: the region names the target's own government model (degem_cd): the target's market trim, provided the
        # catalog does not contradict its technical identity
        if region.get("status") == "assigned":
            from .document_binding import parse_catalog_key

            parts = parse_catalog_key(region["assignment"]) or {}
            target = _target_parts(identity)
            if any(target[k] not in (None, "") and parts.get(k) not in (None, "") and parts[k] != target[k]
                   for k in ("drivetrain", "power", "displacement_l", "body", "propulsion")):
                return {"status": "unresolved", "reason": "gov_model_code_catalog_conflict"}
        return {"status": "target", "reason": "gov_model_code", "rule": "gov_model_code"}
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
                field_values: int, doc_statuses: dict | None = None, headings: list[str] | None = None) -> dict | None:
    """The DVM decision of one fact: {status, region_id, region_kind, identity, candidates_before, candidates_after,
    reason, ...} (status target | other_variant | shared | unresolved | none), or None without a map.
    `field_values`: how many materially different values the document's own harvest has for this field.

    PR #43 designation link: a fact no region decides whose own clause / quote / section heading names exactly ONE
    version designation ("Q7 S-line 55 TFSIe") takes the verdict of the regions of the document that name it: target
    (reason `designation_region`) when one is assigned to the target by the catalog with its own power and none is
    another variant."""
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
    # 2. DOM groups: a pair whose label the quote names (or whose whole value the quote is) and which states the value
    whole = squash(fragment)
    held = [p for p in vmap.get("pairs") or []
            if (f" {squash(p['label'])} " in frag or (whole and whole == squash(p["value"])))
            and _cell_has(p["value"], value)]
    groups = [p["group"] for p in held]
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
        group_target = None
        if level == "group" and "target" in kinds and kinds != {"target"}:
            # the same label / value stated in several repeated groups (a spec section per variant): a group of the
            # target states it, so it is the target's value, unless a target group states another value for the label
            group_target = next((r2, v2) for r2, v2 in judged if v2["status"] == "target")
        if kinds == {"target"}:
            status = "target"
        elif group_target is not None:
            status = "target"
            rid, v = group_target
        elif kinds == {"other_variant"} and len({v2.get("contradicts") for _, v2 in judged}) >= 1:
            status = "other_variant"
        else:
            status = "unresolved"
            rid, v = next(((r2, v2) for r2, v2 in judged if v2["status"] == "unresolved"), judged[0])
        if level == "document" and status != "target":
            break                                   # the document's own verdict is only a positive proof
        if level == "group" and status == "target":
            # another group of the target that states a different value under the same label: no silent pick
            labels = {squash(p["label"]) for p in held}
            if any(squash(p["label"]) in labels and not _cell_has(p["value"], value) and p["group"] in regions
                   and verdict(p["group"])["status"] == "target" for p in vmap.get("pairs") or []):
                disagree = True
        region = regions[rid]
        decision = {"status": status, "region_id": rid, "region_kind": region["kind"], "level": level,
                    "reason": v.get("reason"), "contradicts": v.get("contradicts"),
                    "identity": region["identity"], "identity_text": region.get("text"),
                    "candidates_before": region.get("candidates_before"),
                    "candidates_after": region.get("candidates_after"),
                    "assignment": region.get("assignment"), "regions": [r for r, _ in judged]}
        if v.get("rule"):
            decision["rule"] = v["rule"]
        if region.get("rule") == "tab_labels":
            decision["tab_label"] = region.get("tab_label")
        if group_target is not None:
            decision["region_statuses"] = {r2: v2["status"] for r2, v2 in judged}
        break
    if disagree and decision.get("status") == "target":
        decision.update({"status": "unresolved", "reason": "target_regions_disagree"})
    if decision.get("status") in ("none", "unresolved") and not disagree:
        linked = _designation_link(regions, verdict, " ".join([clause or "", fragment or "", *(headings or [])]))
        if linked is not None:
            decision = linked
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


def _designation_link(regions: dict, verdict, context: str) -> dict | None:
    named = designations(normalize_text(context))
    if len(named) != 1:
        return None
    (name,) = named
    judged = [(rid, verdict(rid)) for rid, r in regions.items()
              if r["kind"] != "document" and name in (r["identity"].get("designation") or [])]
    if any(v["status"] == "other_variant" for _, v in judged):
        return None
    proven = [(rid, v) for rid, v in judged if v["status"] == "target" and v.get("reason") == "catalog_assignment"
              and regions[rid]["identity"].get("power")]
    if not proven:
        return None
    rid, v = proven[0]
    region = regions[rid]
    return {"status": "target", "region_id": rid, "region_kind": region["kind"], "level": "designation",
            "reason": "designation_region", "designation": name, "identity": region["identity"],
            "identity_text": region.get("text"), "candidates_before": region.get("candidates_before"),
            "candidates_after": region.get("candidates_after"), "assignment": region.get("assignment"),
            "regions": [r for r, _ in judged]}


# --- F5: the target-market trim offer of a document (regulatory model-code tables) ------------------------------------

def market_trim_offer(vmap: dict | None, identity: TargetIdentity) -> dict | None:
    """What a document's complete model-code table(s) say about the target's market trim: {status: "offered", code,
    region} when a row has the target's government model code (degem_cd); {status: "market_trim_not_offered", codes,
    technical_variant, table_index} when no complete table has it but one lists the target's technical variant under
    other codes only (the importer sells that technical variant as other trims); None otherwise (no code table, an
    incomplete one, a target without a code, or a table that never lists the target's technical variant).
    Target-market / authority eligibility is the caller's (evidence_admission.fact_binding)."""
    if not vmap or identity.gov_model_code is None:
        return None
    tables = [t for t in vmap.get("model_codes") or [] if t.get("complete")]
    for table in tables:
        for row in table["rows"]:
            if row["code"] == identity.gov_model_code:
                return {"status": "offered", "code": row["code"], "region": row["region"],
                        "table_index": table["table_index"]}
    key = catalog_key(manufacturer=identity.manufacturer, family=identity.family, year=identity.year,
                      body=identity.body, propulsion=identity.propulsion, drivetrain=identity.drivetrain,
                      power=power_bucket(identity.power_hp), displacement_l=identity.displacement_l)
    for table in tables:
        codes = sorted({row["code"] for row in table["rows"] if row.get("assignment") == key})
        if codes:
            return {"status": "market_trim_not_offered", "codes": codes, "target_code": identity.gov_model_code,
                    "technical_variant": key, "table_index": table["table_index"]}
    return None
