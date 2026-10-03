"""Structural HTML harvest (Part A of the candidate-yield work): label / value PAIRS read from the DOM.

`tools/extract.visible_text` flattens a page to lines, and the line harvester pairs a label only with a value on the
same line or the next one. Hebrew spec pages put label and value in sibling elements, cards or definition lists, so the
flattening separates them. This module reads those structures directly from the cached HTML:

    <dl>                      each <dt> with its following <dd>(s)
    two-column elements       an element whose element children are exactly two short text blocks
                              (label <= 80 chars, value <= 120 chars): li > span + span, div > div + div card rows,
                              th / td pairs outside a <table>
    one-element pairs         "label: value", "label – value", "label | value" inside one element
    repeated groups           >= 3 sibling pairs with the same tag / class signature are the strong signal; an isolated
                              pair needs its label to match a dictionary alias
    trim headings             a repeated group under a heading that names a trim / version (the dictionary's
                              trim_header matcher) keeps that heading as its `header` (as a table column header)

Every pair carries the quote "{label} | {value}" (the form table-row segments use). Admission reads the cached
document text, so a pair is emitted only when that quote occurs in the document as admission sees it
(evidence_admission: squashed visible text + tag-stripped source); else only when label and value each occur and the
"…"-joined quote "label … value" passes the fragment rule; else never.

Pure functions only: no cache, no network, no field names. A pair is a CANDIDATE source (via the existing matchers in
src/candidate_harvest.py), never evidence.
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag

from .candidate_harvest import normalize_text

MAX_PAIRS = 400
LABEL_MAX, VALUE_MAX = 80, 120
REPEAT_MIN = 3
NOISE_TAGS = ("script", "style", "noscript", "template", "svg", "iframe", "object", "canvas")
SKIP_TAGS = ("nav", "footer", "header")
CONSENT = re.compile(r"cookie|consent|gdpr|onetrust|cookiebot", re.I)
HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
BLOCK_TAGS = {"div", "p", "li", "ul", "ol", "table", "section", "article", "dl", "dt", "dd", "tr", "tbody", "thead",
              "h1", "h2", "h3", "h4", "h5", "h6", "form", "aside", "main", "figure", "header", "footer", "nav"}
HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
# "label: value" / "label – value" / "label | value" inside one element (a hyphen needs spaces around it; "0-100" and
# "e-CVT" are no separators)
SPLIT = re.compile(r"^(?P<label>[^:|–—]{1,80}?)\s*(?::|\s[–—-]\s|\s\|\s)\s*(?P<value>\S.{0,119})$", re.S)


def _text(node: Any) -> str:
    if isinstance(node, NavigableString):
        return re.sub(r"\s+", " ", str(node)).strip()
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _hidden(tag: Tag) -> bool:
    attrs = tag.attrs or {}
    if "hidden" in attrs or str(attrs.get("aria-hidden") or "").lower() == "true":
        return True
    if HIDDEN_STYLE.search(str(attrs.get("style") or "")):
        return True
    marker = " ".join([str(attrs.get("id") or "")] + [str(c) for c in attrs.get("class") or []])
    return bool(CONSENT.search(marker))


def clean_soup(html: str) -> BeautifulSoup:
    """The page without scripts, navigation, footer, header, cookie / consent blocks and hidden elements."""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(NOISE_TAGS + SKIP_TAGS):
        tag.decompose()
    for tag in list(soup.find_all(True)):
        if not getattr(tag, "decomposed", False) and tag.attrs is not None and _hidden(tag):
            tag.decompose()
    return soup


def _element_children(tag: Tag) -> list[Tag]:
    return [c for c in tag.children if isinstance(c, Tag)]


def _own_text(tag: Tag) -> str:
    return " ".join(_text(c) for c in tag.children if isinstance(c, NavigableString) and _text(c))


def _signature(tag: Tag) -> tuple:
    return (tag.name, tuple(sorted(str(c) for c in tag.get("class") or [])),
            tuple(c.name for c in _element_children(tag)))


def _label(text: str) -> str:
    return text.strip().rstrip(":：").strip()


def _pair_ok(label: str, value: str) -> bool:
    return bool(label and value) and len(label) <= LABEL_MAX and len(value) <= VALUE_MAX and label != value


def _two_column(tag: Tag) -> tuple[str, str] | None:
    """(label, value) of an element whose element children are exactly two short text blocks."""
    if tag.name in ("tr", "table", "tbody", "thead", "dl") or tag.find_parent("table") is not None:
        return None
    kids = _element_children(tag)
    if len(kids) != 2 or _own_text(tag):
        return None
    if any(k.name in ("ul", "ol", "table", "dl") for k in kids):
        return None
    label, value = _label(_text(kids[0])), _text(kids[1])
    return (label, value) if _pair_ok(label, value) else None


def _split_pair(tag: Tag) -> tuple[str, str] | None:
    """(label, value) of one element whose own text is "label: value" (inline children only)."""
    if tag.name in BLOCK_TAGS - {"div", "p", "li", "dd", "dt"} or tag.find_parent("table") is not None:
        return None
    if any(c.name in BLOCK_TAGS for c in tag.find_all(True)):
        return None
    text = _text(tag)
    if not text or len(text) > LABEL_MAX + VALUE_MAX + 4:
        return None
    m = SPLIT.match(text)
    if not m:
        return None
    label, value = _label(m.group("label")), m.group("value").strip()
    return (label, value) if _pair_ok(label, value) else None


def _heading_before(tag: Tag) -> str | None:
    heading = tag.find_previous(HEADINGS)
    return _text(heading)[:120] if heading is not None else None


def raw_pairs(soup: BeautifulSoup) -> list[dict]:
    """Every structural pair candidate in page order: {label, value, signature, parent, node, kind}."""
    out: list[dict] = []
    for dl in soup.find_all("dl"):
        term = None
        for child in _element_children(dl):
            if child.name == "dt":
                term = _label(_text(child))
            elif child.name == "dd" and term:
                value = _text(child)
                if _pair_ok(term, value):
                    out.append({"label": term, "value": value, "signature": ("dl",), "parent": id(dl), "node": dl,
                                "kind": "definition_list"})
    for tag in soup.find_all(True):
        if tag.name in ("dl", "dt", "dd"):
            continue
        pair = _two_column(tag)
        kind = "two_column"
        if pair is None:
            pair = _split_pair(tag)
            kind = "split"
            # the label / value children of a two-column element are not pairs of their own
            if pair is not None and tag.parent is not None and _two_column(tag.parent) is not None:
                pair = None
        if pair is not None:
            out.append({"label": pair[0], "value": pair[1], "signature": _signature(tag),
                        "parent": id(tag.parent), "node": tag, "kind": kind})
    return out


def admission_haystack(html: str, text: str) -> str:
    """The comparison text admission checks a quote against for an HTML document (evidence_admission.document_text:
    squashed visible text + tag-stripped source; structured data is left out, which only makes this stricter)."""
    from .evidence_admission import squash

    return " " + squash(text) + " " + squash(re.sub(r"<[^>]+>", " ", html or "")) + " "


def admissible_quote(haystack: str, label: str, value: str) -> str | None:
    """The pair's quote: "label | value" when it occurs in the document as admission reads it, else "label … value"
    when admission's fragment rule accepts it, else None."""
    from .evidence_admission import FRAGMENT_WINDOW, _in_order, quote_fragments, squash

    for quote in (f"{label} | {value}", f"{label} … {value}"):
        parts = [squash(p) for p in quote_fragments(quote)]
        if parts and all(parts) and _in_order(haystack, parts, FRAGMENT_WINDOW):
            return quote
    return None


def html_pairs(html: str, text: str, *, alias_pattern=None, trim_header=None, limit: int = MAX_PAIRS) -> list[dict]:
    """Admissible structural pairs of one HTML page: [{label, value, quote, header, kind}], deduplicated by
    (label, value), at most `limit`. `alias_pattern` (any field's dictionary alias) admits an isolated pair;
    `trim_header` (the dictionary's trim header matcher) decides which headings are kept as a group's header.
    Never raises (an unparsable page yields no pairs)."""
    try:
        soup = clean_soup(html)
        pairs = raw_pairs(soup)
    except Exception:  # noqa: BLE001 - a broken page only loses its structural pairs
        return []
    groups: dict[tuple, int] = {}
    for p in pairs:
        key = (p["parent"], p["signature"])
        groups[key] = groups.get(key, 0) + 1
    haystack = admission_haystack(html, text)
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    headers: dict[int, str | None] = {}
    for p in pairs:
        if len(out) >= limit:
            break
        label, value = p["label"], p["value"]
        key = (normalize_text(label), normalize_text(value))
        if key in seen:
            continue
        repeated = p["kind"] == "definition_list" or groups[(p["parent"], p["signature"])] >= REPEAT_MIN
        if not repeated and not (alias_pattern is not None and alias_pattern.search(normalize_text(label))):
            continue
        quote = admissible_quote(haystack, label, value)
        if quote is None:
            continue
        seen.add(key)
        header = None
        if repeated:
            if p["parent"] not in headers:
                heading = _heading_before(p["node"])
                headers[p["parent"]] = heading if heading and trim_header is not None \
                    and trim_header.search(normalize_text(heading)) else None
            header = headers[p["parent"]]
        out.append({"label": label, "value": value, "quote": quote, "header": header, "kind": p["kind"],
                    "repeated": repeated})
    return out
