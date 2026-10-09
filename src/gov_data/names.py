"""Exact normalizations shared by the builds and the facts side. Never fuzzy, never a model.

    to_number            a numeric cell ("2019", "2019.0", " 145,900 ") or None
    to_int               a code / year cell: 588, "588", 588.0, "588.0", " 588 " -> 588; anything else -> None
                         ("588.5", "5,88", "5e2", "-1", "abc", True)
    norm_model           a model name as compared: NFKC, upper case, every non letter / digit a space, collapsed
                         ("C-HR" -> "C HR"; "CHR" stays "CHR": not equal)
    makes_of_name        the canonical make(s) of a manufacturer name through data/make_canonical.json: the `tozar`
                         table (a registry tozeret_nm such as "טויוטה יפן" counts as its tozar when the tozar is a
                         whole-word prefix of it: the country suffix of the registry), else a Latin spelling's
                         normalization (makes.canonical_of); only the plain makes of a tozar, never a model-gated one
    year_of / ym_of      the year / (year, month) of a date cell: YYYY-MM, YYYY-M, YYYYMM (int or text), YYYYMMDD,
                         YYYY-MM-DD, YYYY-MM-DDTHH:MM:SS[.fff][Z] (or a space before the time), DD/MM/YYYY
    shape                the format pattern of a cell for the build reports: digits -> 9, letters -> a, the rest as
                         stated ("2019-03-12T00:00:00" -> "9999-99-99a99:99:99"); never the value itself
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

_NOT_WORD = re.compile(r"[^0-9A-Z֐-׿]+")
_YM = re.compile(r"^\s*(\d{4})[-/.](\d{1,2})(?:[-/.]\d{1,2})?(?:[ T].*)?\s*$")
_DMY = re.compile(r"^\s*(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})(?:[ T].*)?\s*$")
_COMPACT = re.compile(r"^\s*(\d{4})(\d{2})(\d{2})?\s*$")
_YEAR = re.compile(r"^\s*(\d{4})(?:\.0+)?\s*$")
_INT = re.compile(r"^\s*(\d+)(?:\.0+)?\s*$")
SHAPE_MAX = 32


def to_number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return None if number != number else number


def to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value == value and value >= 0 and value.is_integer() else None
    m = _INT.match(str(value))
    return int(m.group(1)) if m else None


def shape(value: Any) -> str:
    """The format pattern of a cell (digits -> 9, letters of any script -> a), at most SHAPE_MAX characters."""
    text = str(value)
    out = "".join("9" if ch.isdigit() else "a" if ch.isalpha() else ch for ch in text[:SHAPE_MAX])
    return out + ("…" if len(text) > SHAPE_MAX else "")


def norm_model(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).upper()
    return " ".join(_NOT_WORD.sub(" ", text).split())


def norm_name(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def makes_of_name(value: Any, table: dict | None = None) -> list[str]:
    from ..open_data import makes as M

    table = M.canonical() if table is None else table
    name = norm_name(value)
    if not name:
        return []
    tozar = table.get("tozar") or {}
    if name in tozar:
        return sorted(M.tozar_makes(name, table)[0])
    prefixes = [k for k in tozar if name.startswith(f"{k} ")]
    if prefixes:
        return sorted(M.tozar_makes(max(prefixes, key=len), table)[0])
    latin = M.canonical_of(name, table)
    return [latin] if latin else []


def catalogue_makes(tozar: Any, table: dict | None = None) -> list[str]:
    """The canonical makes a catalogue row's tozar names (plain and model-gated)."""
    from ..open_data import makes as M

    plain, gated = M.tozar_makes(norm_name(tozar), table)
    return sorted({*plain, *gated})


def ym_of(value: Any) -> tuple[int, int] | None:
    text = str(value or "").strip()
    if not text:
        return None
    for pattern, order in ((_YM, (1, 2)), (_COMPACT, (1, 2))):
        m = pattern.match(text)
        if m:
            year, month = int(m.group(order[0])), int(m.group(order[1]))
            return (year, month) if 1900 <= year <= 2100 and 1 <= month <= 12 else None
    m = _DMY.match(text)
    if m:
        year, month = int(m.group(3)), int(m.group(2))
        return (year, month) if 1900 <= year <= 2100 and 1 <= month <= 12 else None
    return None


def year_of(value: Any) -> int | None:
    m = _YEAR.match(str(value or ""))
    if m:
        year = int(m.group(1))
        return year if 1900 <= year <= 2100 else None
    ym = ym_of(value)
    return ym[0] if ym else None
