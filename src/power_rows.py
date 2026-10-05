"""PR #46 (P1.1): the power rows of a multi-column spec table, one power per version column.

A PDF catalog states its versions side by side: a header row naming each column ("A6 Design 55 TFSI quattro | A6 Design
45 TFSI quattro") and spec rows stating one value per column. The power row is where the versions differ, so it is what
tells a reader which versions the document covers. pdfplumber reads Hebrew in VISUAL order, so the row of 319.pdf comes
out as `340/5,000-6,400 245 )ס"כ/ד"לס( יברימ קפסה` ("הספק מירבי (כ"ס/סל"ד)": 340 hp at 5,000-6,400 rpm, 245 hp).

    power_rows(text, tables)   every power row of a document's tables (a label cell) and of its text lines (short
                               lines; a label read as written or backwards), with one power per value cell / token:
                               {powers, columns: [{column, power, identity, designation}], rows}

A row label is a power label (`הספק`, `כוח סוס`, `max power`, written forwards or backwards) with a horsepower unit
(כ"ס, hp, PS, or `כוח סוס` itself). A kW row counts only in a motor context (מנוע / חשמלי / motor / electric in its
label) and never in a charging context (the PR #42 exclusion: identity vocabulary `charging_words`); its kW are converted
to PS (the government's כ"ס). A value is a number of 2-4 digits, optionally followed by "/<rpm>" ("340/5,000-6,400").
Torque rows (מומנט / torque) are never power rows. Column identity: the table's header cell of the value's column (the
first row naming a designation), or, for a visual-order text line, the nearest header line above it naming exactly as
many designations as the row states values (visual order on both). Deterministic and pure; nothing here is evidence.
"""

from __future__ import annotations

import re
from typing import Iterable

from .candidate_harvest import normalize_text

POWER_LABELS = ("הספק", "כוח סוס", "כח סוס", "max power", "maximum power", "power output", "power")
HEBREW_LABELS = ("הספק", "כוח סוס", "כח סוס")
HP_UNITS = ('כ"ס', "כוח סוס", "כח סוס", "hp", "bhp", "ps")
KW_UNITS = ("קילוואט", 'קוט"ס', "kw")
MOTOR_WORDS = ("מנוע", "חשמלי", "motor", "electric")
TORQUE_WORDS = ("מומנט", "torque", 'קג"מ', "nm")
MAX_LINE_CHARS = 90
HEADER_LOOKBACK = 80
KW_TO_PS = 1.35962
VALUE = re.compile(r"^\*?(\d{2,4}(?:\.\d)?)(?:/[\d,.\-–]+)?\*?$")


def _both_ways(terms: Iterable[str]) -> list[str]:
    """Each term as written and read backwards (a visual-order Hebrew line stores 'הספק' as 'קפסה')."""
    out = []
    for term in terms:
        norm = normalize_text(term)
        out += [norm, norm[::-1]]
    return list(dict.fromkeys(out))


def _has(text: str, terms: Iterable[str]) -> bool:
    return any(re.search(rf"(?<![\wא-ת]){re.escape(t)}(?![\wא-ת])", text) for t in _both_ways(terms))


def _written(text: str, terms: Iterable[str]) -> bool:
    """`text` names one of `terms` exactly as written (no backwards reading)."""
    return any(re.search(rf"(?<![\wא-ת]){re.escape(normalize_text(t))}(?![\wא-ת])", text) for t in terms)


def label_kind(label: str, vocab: dict | None = None) -> str | None:
    """'hp' / 'kw' when `label` (a row label, forwards or backwards) is a power label, else None."""
    norm = normalize_text(label or "")
    if not _has(norm, POWER_LABELS) or _has(norm, TORQUE_WORDS):
        return None
    if _has(norm, HP_UNITS):
        return "hp"
    if _has(norm, KW_UNITS) and _has(norm, MOTOR_WORDS):
        if vocab is None:
            from .document_binding import vocabulary

            vocab = vocabulary()
        if _has(norm, vocab.get("charging_words") or []):
            return None          # PR #42: a charging power is never a motor power
        return "kw"
    return None


def cell_power(cell: str, kind: str) -> float | None:
    m = VALUE.match(str(cell or "").strip().replace(" ", ""))
    if not m:
        return None
    value = float(m.group(1))
    if kind == "kw":
        value = round(value * KW_TO_PS, 1)
    return value if 40 <= value <= 2000 else None


def _header_designations(rows: list[list[str]]) -> list[str] | None:
    from .document_binding import designations

    for row in rows[:3]:
        names = [sorted(designations(normalize_text(str(c or ""))))[0] if designations(normalize_text(str(c or "")))
                 else None for c in row]
        if any(names):
            return names
    return None


def _table_rows(tables: Iterable[dict]) -> list[dict]:
    out = []
    for t_index, table in enumerate(tables or []):
        rows = [[str(c or "") for c in row] for row in table.get("rows") or [] if isinstance(row, list)]
        header = _header_designations(rows)
        head_row = next((r for r in rows[:3] if any(_header_designations([r]) or [])), None)
        for r_index, row in enumerate(rows):
            labels = [(i, label_kind(c)) for i, c in enumerate(row) if c and not VALUE.match(c.strip().replace(" ", ""))]
            kind = next((k for _, k in labels if k), None)
            if kind is None:
                continue
            columns = []
            for c_index, cell in enumerate(row):
                power = cell_power(cell, kind)
                if power is None:
                    continue
                columns.append({"column": c_index, "power": power,
                                "identity": (head_row[c_index] if head_row and c_index < len(head_row) else None)
                                or None,
                                "designation": header[c_index] if header and c_index < len(header) else None})
            if columns:
                out.append({"source": f"table:{table.get('index', t_index)}:row:{r_index}", "kind": kind,
                            "columns": columns})
    return out


def _line_rows(text: str) -> list[dict]:
    from .document_binding import designation_spans

    lines = (text or "").splitlines()
    out = []
    for i, line in enumerate(lines):
        if len(line) > MAX_LINE_CHARS:
            continue
        tokens = line.split()
        values = [(j, t) for j, t in enumerate(tokens) if VALUE.match(t)]
        label = " ".join(t for j, t in enumerate(tokens) if not VALUE.match(t))
        kind = label_kind(label) if values else None
        if kind is None:
            continue
        powers = [p for p in (cell_power(t, kind) for _, t in values) if p is not None]
        if not powers:
            continue
        # visual order: a backwards label (the line as pdfplumber read it) keeps its value tokens in column order
        visual = _written(normalize_text(label), [t[::-1] for t in HEBREW_LABELS]) \
            and not _written(normalize_text(label), HEBREW_LABELS)
        names: list[str | None] = [None] * len(powers)
        identity: list[str | None] = [None] * len(powers)
        if visual:
            for k in range(i - 1, max(-1, i - HEADER_LOOKBACK), -1):
                spans = designation_spans(normalize_text(lines[k]))
                if len(spans) >= 2:
                    if len(spans) == len(powers):
                        names = [d for d, _, _ in sorted(spans, key=lambda s: s[1])]
                        identity = [lines[k].strip()] * len(powers)
                    break
        out.append({"source": f"line:{i}", "kind": kind, "visual": visual,
                    "columns": [{"column": n, "power": p, "identity": identity[n], "designation": names[n]}
                                for n, p in enumerate(powers)]})
    return out


def power_rows(text: str, tables: Iterable[dict] | None = None) -> dict:
    """{powers: sorted distinct powers, columns: [{column, power, identity, designation, source}], rows: sources}."""
    rows = _table_rows(tables or []) + _line_rows(text)
    columns: list[dict] = []
    for row in rows:
        for col in row["columns"]:
            columns.append({**col, "source": row["source"]})
    powers = sorted({c["power"] for c in columns})
    return {"powers": powers, "columns": columns, "rows": [r["source"] for r in rows]}


def powers_by_designation(result: dict) -> dict[str, list[float]]:
    """{designation: [powers]} of the columns whose identity names a designation."""
    out: dict[str, list[float]] = {}
    for col in (result or {}).get("columns") or []:
        if col.get("designation"):
            out.setdefault(col["designation"], [])
            if col["power"] not in out[col["designation"]]:
                out[col["designation"]].append(col["power"])
    return out
