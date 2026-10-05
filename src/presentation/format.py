"""Plain-text formatting shared by framework-neutral view models (durations, UTC timestamps)."""

from __future__ import annotations

from ..runstate.repository import parse_ts


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(round(max(0.0, seconds)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def fmt_time(value: str | None) -> str:
    ts = parse_ts(value)
    return ts.strftime("%Y-%m-%d %H:%M UTC") if ts else "—"
