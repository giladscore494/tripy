"""Israeli trim pages and version-compare tables (PR #47, A3): carzone.co.il, configured as data in
data/source_rules.json (`il_version_sites`, the carzone entry: `trim_query_param`, `trim_slug`, `compare_table`).

carzone publishes, per model and model year, a model page, one trim page per version (/<Make>/<Model>/<Sub>?/<year>
?trim=<slug>; the slug names the trim words, power / fuel, displacement, gearbox, drivetrain and seats:
"Business_Edition-hybrid-1.8L-AT-4X2-5seats") and a compare page (/<Make>/<Model>/<Sub>?/<year>/compare) with one
column per version, a header row naming each column's trim ("רמת גימור") and "-" for a value a version does not state.
Everything here is deterministic and fail-closed:

    parse_trim_slug(slug)                {trim_words, propulsion, displacement_l, gearbox, drivetrain, seats}
    trim_words_match(words, identity)    the slug's / a column's trim words ARE the target's government trim: the
                                         government trim matches them with the PR #42 catalog-trim rules (prefixes,
                                         aliases: BUSINESS EDI = "Business Edition") and they hold no other word.
                                         Hebrew column words are read through the identity vocabulary's
                                         `trim_word_forms_he` ("ביזנס אדישן" = business edition)
    trim_page_verdict(material, ...)     a carzone trim page: accepted (binding basis `il_trim_page`) when the slug's
                                         trim words match the target trim, the slug's displacement / propulsion /
                                         drivetrain are the target's AND the page itself names that version (its
                                         title / H1 / breadcrumb): carzone renders the DEFAULT version in the static
                                         HTML of every ?trim= URL, so a slug alone proves nothing about the content
    compare_verdict(material, ...)       a carzone compare page: the columns (trim + technical identity read from the
                                         identity rows of each column), the ONE column whose trim is the target's;
                                         accepted when that column's year, body, propulsion, displacement and
                                         drivetrain match (power: the binding ladder's rule; a hybrid's stated system
                                         power never contradicts unless the catalog's single entry says otherwise)
    fact_column(material, ...)           which column a fact's value sits in (its row read from the quote "label |
                                         cells", the cells split by the page's own lines; "-" / "—" is no value):
                                         `target` (binds exact_market_trim, basis il_compare_column), `other_variant`
                                         (another version's column: never exact_market_trim) or None (unknown: the
                                         normal binding)

Nothing here is evidence: every value still passes the admission gate.
"""

from __future__ import annotations

import re
from typing import Any, Iterable
from urllib.parse import unquote

from .candidate_harvest import normalize_text
from .document_binding import (HYBRID_PROPULSIONS, TargetIdentity, dimension_status, mentions, normalize_catalog_trim,
                               subvariant_status, vocabulary, zone_names_target)

TRIM_PAGE_VERSION = "il-trim-page-v1"
COLUMN_DIMENSIONS = ("year", "body", "propulsion", "displacement", "drivetrain", "power")


def _site_cfg(material) -> tuple[str | None, dict]:
    from .il_version_pages import _site_config, site_of

    site = site_of(material.doc.url)
    return site, _site_config(site)


# --- trim words ---------------------------------------------------------------------------------------------------------

def _hebrew_forms() -> dict[str, str]:
    """Hebrew written form -> Latin trim word (identity vocabulary `trim_word_forms_he`)."""
    forms = vocabulary().get("trim_word_forms_he") or {}
    return {normalize_text(he): latin.lower() for latin, spellings in forms.items() if not latin.startswith("_")
            for he in spellings or []}


def trim_words(text: str, noise: Iterable[str] = ()) -> list[str]:
    """The trim words of a version name: words, Hebrew ones read as their Latin trim word, noise words (powertrain /
    generation descriptors of the site's `trim_noise_words`, numbers) dropped. An unknown Hebrew word stays (and then
    never matches a Latin government trim: fail-closed)."""
    forms = _hebrew_forms()
    patterns = [re.compile(rf"^(?:{n})$") for n in noise]
    out = []
    for word in re.findall(r"[\wא-ת]+(?:\.\d+)?", normalize_text(re.sub(r"[_]", " ", text or ""))):
        if any(p.match(word) for p in patterns):
            continue
        out.append(forms.get(word, word))
    return out


def trim_words_match(words: list[str], identity: TargetIdentity) -> bool:
    """The words ARE the target's government trim (module doc): the catalog-trim rules match the trim on them, and
    the match covers every word (a column "Comfort Business" is not BUSINESS)."""
    from .variant_map import catalog_trim_matches

    if not words or not identity.trim_words:
        return False
    own = normalize_catalog_trim(" ".join(identity.trim_words))
    matches = catalog_trim_matches(" ".join(words), [own], identity)
    return any(m["start"] == 0 and m["end"] == len(words) for m in matches)


# --- trim pages -------------------------------------------------------------------------------------------------------

def parse_trim_slug(slug: str, cfg: dict | None = None) -> dict:
    """{trim_words, propulsion, displacement_l, gearbox, drivetrain, seats, unknown} of a trim slug (the site's
    `trim_slug` rules). The trim words are the parts before the first recognised technical part."""
    cfg = cfg or {}
    rules = cfg.get("trim_slug") or {}
    parts = [p for p in unquote(str(slug or "")).split(rules.get("separator") or "-") if p]
    out: dict[str, Any] = {"trim_words": [], "propulsion": None, "displacement_l": None, "gearbox": None,
                           "drivetrain": None, "seats": None, "unknown": []}
    technical = False
    for part in parts:
        low = part.lower()
        for key in ("propulsion", "drivetrain", "gearbox"):
            if low in (rules.get(key) or {}):
                out[key] = rules[key][low]
                technical = True
                break
        else:
            m = re.match(rules.get("displacement") or r"^(\d\.\d)l$", low)
            s = re.match(rules.get("seats") or r"^(\d)seats?$", low)
            if m:
                out["displacement_l"] = float(m.group(1))
                technical = True
            elif s:
                out["seats"] = int(s.group(1))
                technical = True
            elif not technical:
                out["trim_words"] += [w.lower() for w in part.split(rules.get("word_separator") or "_") if w]
            else:
                out["unknown"].append(part)
    return out


def _slug_mismatches(slug: dict, identity: TargetIdentity) -> list[str]:
    bad = []
    if identity.propulsion != "battery_electric":
        if slug["displacement_l"] is None or identity.displacement_l is None \
                or abs(slug["displacement_l"] - identity.displacement_l) > 0.06:
            bad.append("displacement")
    if slug["propulsion"] != identity.propulsion:
        bad.append("propulsion")
    if slug["drivetrain"] != identity.drivetrain:
        bad.append("drivetrain")
    if slug["gearbox"] and identity.transmission and slug["gearbox"] != identity.transmission:
        bad.append("gearbox")
    return bad


def _page_head(material) -> str:
    from .il_version_pages import page_head

    doc = material.doc
    crumbs = [ln for ln in (doc.text or "").splitlines()[:40] if ln.strip()]
    return page_head(doc) + "\n" + "\n".join(crumbs[:12])


def trim_page_verdict(material, identity: TargetIdentity) -> dict | None:
    """A carzone trim page's verdict (module doc), None when the document is not one."""
    from .il_version_pages import trim_slug_of

    site, cfg = _site_cfg(material)
    slug_text = trim_slug_of(material.doc.url)
    if not site or not slug_text:
        return None
    slug = parse_trim_slug(slug_text, cfg)
    out = {"version": TRIM_PAGE_VERSION, "kind": "trim_page", "site": site, "slug": slug_text,
           "slug_identity": {k: v for k, v in slug.items() if v not in (None, [], "")}}
    if not zone_names_target(normalize_text(_page_head(material)), identity):
        return {**out, "status": "rejected", "reason": "family_absent"}
    if not trim_words_match(slug["trim_words"], identity):
        return {**out, "status": "rejected", "reason": "trim_other" if slug["trim_words"] else "trim_absent"}
    bad = _slug_mismatches(slug, identity)
    if bad:
        return {**out, "status": "rejected", "reason": f"{bad[0]}_mismatch"}
    # the page must name the slug's version itself: the title / H1 / breadcrumb, never the version selector
    noise = (cfg.get("compare_table") or {}).get("trim_noise_words") or []
    head = [ln for ln in _identity_lines(material)]
    if not any(trim_words_match(_trim_span(trim_words(line, noise), identity), identity) for line in head):
        return {**out, "status": "rejected", "reason": "page_trim_unconfirmed"}
    statuses = mentions("\n".join(head), identity)
    year = dimension_status("year", statuses["year"], identity)
    if year == "mismatch":
        return {**out, "status": "rejected", "reason": "year_mismatch"}
    return {**out, "status": "accepted", "reason": "trim_page", "trim_named": True}


def _identity_lines(material) -> list[str]:
    """The lines that say which version a carzone page shows: its title, H1s and breadcrumb (the first lines of the
    page text up to the first heading)."""
    from .il_version_pages import unescape

    doc = material.doc
    lines = [unescape(doc.meta.get("title"))] + [unescape(h) for h in doc.headings or []]
    text = [ln.strip() for ln in (doc.text or "").splitlines() if ln.strip()]
    first_heading = next((i for i, ln in enumerate(text) if ln in set(doc.headings or [])), min(len(text), 12))
    return [ln for ln in lines + text[:first_heading] if ln]


def _trim_span(words: list[str], identity: TargetIdentity) -> list[str]:
    """The words of a heading / breadcrumb line that can name a trim: the line minus the target's make / family /
    body names and the model year (a breadcrumb item "קורולה ספייס" names no trim)."""
    from .il_version_pages import body_names

    vocab = vocabulary()
    drop = {normalize_text(t) for t in (vocab.get("manufacturers") or {}).get(str(identity.manufacturer or ""), [])}
    drop |= {normalize_text(t) for t in (vocab.get("model_families") or {}).get(str(identity.family or ""), [])}
    drop |= {normalize_text(t) for t in body_names(identity)}
    drop |= {w for t in list(drop) for w in t.split()}
    return [w for w in words if w not in drop and not re.fullmatch(r"(?:19|20)\d{2}", w)]


# --- compare tables -------------------------------------------------------------------------------------------------

def _lines(material) -> list[str]:
    return [ln.strip() for ln in (material.doc.text or "").splitlines() if ln.strip()]


def _row_after(lines: list[str], label: str, n: int, start: int = 0) -> tuple[int, list[str]] | None:
    """(index, the n cells after) of the first line equal to `label` from `start`."""
    want = normalize_text(label)
    for i in range(start, len(lines) - n):
        if normalize_text(lines[i]) == want:
            return i, lines[i + 1:i + 1 + n]
    return None


def compare_table(material, cfg: dict) -> dict | None:
    """{trims, rows: {label: cells}} of a compare page: the column count from the year row (every cell a model year),
    the trims from the trim row, identity rows by their labels. None when the page does not read as a table."""
    table = cfg.get("compare_table") or {}
    lines = _lines(material)
    n = 0
    for label in table.get("year_row_labels") or []:
        for i, line in enumerate(lines):
            if normalize_text(line) == normalize_text(label):
                years = 0
                while i + 1 + years < len(lines) and re.fullmatch(r"(?:19|20)\d{2}", lines[i + 1 + years]):
                    years += 1
                n = max(n, years)
    if n < 1:
        return None
    trims = None
    for label in table.get("trim_row_labels") or []:
        found = _row_after(lines, label, n)
        if found:
            trims = found[1]
            break
    if not trims:
        return None
    rows = {}
    for label in table.get("identity_row_labels") or []:
        found = _row_after(lines, label, n)
        if found:
            rows[label] = found[1]
    return {"columns": n, "trims": trims, "rows": rows}


def empty_cell(cell: str, cfg: dict) -> bool:
    empties = {normalize_text(e) for e in ((cfg.get("compare_table") or {}).get("empty_cells") or ["-", "—"])}
    return not cell.strip() or normalize_text(cell.strip()) in empties


def compare_verdict(material, identity: TargetIdentity, index: dict | None = None) -> dict | None:
    """A carzone compare page's verdict (module doc); None when the document is not one."""
    from .il_version_pages import classify_url, page_head, single_catalog_drivetrain, single_catalog_entry

    site, cfg = _site_cfg(material)
    if not site or not cfg.get("compare_table") or classify_url(material.doc.url)[1] != "version" \
            or not re.search(r"/compare/?$", material.doc.url.split("?")[0]):
        return None
    out: dict[str, Any] = {"version": TRIM_PAGE_VERSION, "kind": "compare", "site": site}
    if not zone_names_target(normalize_text(page_head(material.doc)), identity):
        return {**out, "status": "rejected", "reason": "family_absent"}
    sub = subvariant_status(page_head(material.doc), identity)
    if sub and sub["status"] == "mismatch":
        return {**out, "status": "rejected", "reason": "body_mismatch"}
    table = compare_table(material, cfg)
    if table is None:
        return {**out, "status": "rejected", "reason": "no_table"}
    noise = (cfg.get("compare_table") or {}).get("trim_noise_words") or []
    columns = []
    for c, trim in enumerate(table["trims"]):
        text = "\n".join([trim] + [f"{label} {cells[c]}" for label, cells in table["rows"].items()
                                   if not empty_cell(cells[c], cfg)])
        found = mentions(text, identity)
        statuses = {d: dimension_status(d, found[d], identity) for d in COLUMN_DIMENSIONS}
        words = trim_words(trim, noise)
        columns.append({"column": c, "trim": trim, "words": words, "target_trim": trim_words_match(words, identity),
                        "statuses": statuses})
    out["columns"] = [{k: col[k] for k in ("column", "trim", "target_trim", "statuses")} for col in columns]
    targets = [col for col in columns if col["target_trim"]]
    if not targets:
        return {**out, "status": "rejected", "reason": "no_target_column"}
    if len(targets) > 1:
        return {**out, "status": "rejected", "reason": "ambiguous_target_column"}
    target = targets[0]
    out["target_column"] = target["column"]
    statuses = dict(target["statuses"])
    if identity.propulsion == "conventional" and statuses["propulsion"] == "absent":
        statuses["propulsion"] = "match"
    needed = ["year", "body", "propulsion", "drivetrain"] + (["displacement"]
                                                            if identity.propulsion != "battery_electric" else [])
    if statuses["drivetrain"] == "absent" and single_catalog_drivetrain(identity, index) is not None:
        needed.remove("drivetrain")
        out["catalog_single_drivetrain"] = identity.drivetrain
    bad = [d for d in needed if statuses[d] != "match"]
    if bad:
        return {**out, "status": "rejected", "reason": f"{bad[0]}_{statuses[bad[0]]}"}
    if statuses["power"] in ("mismatch", "mixed") and not (identity.propulsion in HYBRID_PROPULSIONS
                                                           and single_catalog_entry(identity, index)):
        return {**out, "status": "rejected", "reason": f"power_{statuses['power']}"}
    return {**out, "status": "accepted", "reason": "target_column"}


def _quote_rows(texts: Iterable[str]) -> list[tuple[str, str]]:
    out = []
    for text in texts:
        for line in str(text or "").splitlines():
            if " | " in line:
                label, cells = line.split(" | ", 1)
                out.append((label.strip(), cells.strip()))
    return out


def _states(cell: str, value: Any) -> bool:
    from .typed_values import as_boolean, numbers_in

    if isinstance(value, bool):
        return as_boolean(cell) is value
    wanted = numbers_in(value)
    if wanted:
        have = numbers_in(cell)
        return all(any(abs(h - w) <= 1e-6 * max(1.0, abs(w)) for h in have) for w in wanted)
    squash = lambda t: re.sub(r"[\W_]+", "", normalize_text(str(t)))   # noqa: E731
    v, c = squash(value), squash(cell)
    return bool(v and c) and (v in c or c in v)


def fact_column(material, verdict: dict | None, value: Any, texts: Iterable[str]) -> dict | None:
    """Which column of an accepted compare page a fact's value sits in (module doc). `texts`: the fact's quote and its
    source lines ("label | cells" rows). None when the row or the value's column cannot be told."""
    if not verdict or verdict.get("kind") != "compare" or verdict.get("status") != "accepted":
        return None
    _, cfg = _site_cfg(material)
    n = len(verdict.get("columns") or [])
    target = verdict.get("target_column")
    lines = _lines(material)
    for label, joined in _quote_rows(texts):
        start = 0
        while True:
            found = _row_after(lines, label, n, start)
            if found is None:
                break
            index, cells = found
            start = index + 1
            if normalize_text(" ".join(cells)) != normalize_text(joined):
                continue
            stating = [c for c, cell in enumerate(cells) if not empty_cell(cell, cfg) and _states(cell, value)]
            if not stating:
                return None
            column = target if target in stating else stating[0]
            return {"status": "target" if target in stating else "other_variant", "column": column,
                    "trim": (verdict.get("columns") or [{}] * (column + 1))[column].get("trim"),
                    "row": label, "columns_stating": stating, "basis": "il_compare_column"}
    return None


def page_trim_column(verdict: dict | None) -> dict | None:
    """An accepted trim page: every value of the page is the target trim's (basis il_trim_page)."""
    if verdict and verdict.get("kind") == "trim_page" and verdict.get("status") == "accepted":
        return {"status": "target", "basis": "il_trim_page", "slug": verdict.get("slug")}
    return None


def trim_page_or_compare_verdict(material, identity: TargetIdentity, index: dict | None = None) -> dict | None:
    """The carzone verdict of a document (a trim page or a compare page), None for any other document."""
    return trim_page_verdict(material, identity) or compare_verdict(material, identity, index)
