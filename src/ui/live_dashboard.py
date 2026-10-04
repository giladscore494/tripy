"""Live Hebrew operations dashboard for a running batch (Streamlit, main thread only).

Workers never call Streamlit. Their RunLog listeners only enqueue events (BatchLiveState.listener_for);
this module, on the script thread, drains the queue every ~200 ms while the vehicle futures run,
updates the in-memory state and re-renders only what changed. Polling makes no API request: every
number comes from events the runs already emit (src/runstate/live_state.py).
"""

from __future__ import annotations

import html
import threading
import time
from typing import Callable

import pandas as pd
import streamlit as st

from ..benchmark import run_batch
from ..presentation import labels_he as he
from ..runstate.live_state import BatchLiveState, VehicleLive

CARD_MIN_INTERVAL_S = 0.75
HEADER_MIN_INTERVAL_S = 0.25


def _rtl(markdown_html: str) -> None:
    st.markdown(f'<div dir="rtl" style="text-align:right">{markdown_html}</div>', unsafe_allow_html=True)


def _e(value) -> str:
    return html.escape(str(value)) if value is not None else "—"


def _bar(done: int, total: int, width: int = 20) -> str:
    filled = round(width * done / total) if total else 0
    return "█" * filled + "░" * (width - filled)


def render_header(state: BatchLiveState, slot) -> None:
    h = state.header()
    with slot.container(border=True):
        _rtl(f"<h3>{_e(h['title'])}</h3>")
        cols = st.columns(6)
        cols[0].metric("הושלמו", f"{h['done']} / {h['total']}")
        cols[1].metric("פעילים", h["active"])
        cols[2].metric("ממתינים למודל", h["waiting_model"])
        cols[3].metric("ממתינים לחיפוש", h["waiting_search"])
        cols[4].metric("נכשלו", h["failed"])
        cols[5].metric("הופסקו", h["interrupted"])
        if h["pools"]:
            _rtl(" · ".join(f"<b>{_e(p['name'])}</b>: {p['active']} / {p['limit']} פעיל"
                            + (f" (מגבלת ספק {p['provider_limit']})" if p.get("provider_limit") else "")
                            + (f", {p['waiting']} ממתינים" if p["waiting"] else "") for p in h["pools"]))
        cost = f"${h['cost_usd']:.4f}" + ("" if h["cost_complete"] else " (חלקי: יש שימוש לא ידוע)")
        _rtl(f"זמן ריצה: {h['elapsed_s']:.0f} שניות · עלות מצטברת ידועה: {cost} · קריאות מודל: {h['model_calls']}"
             f" · חיפושים: {h['searches']} · 429: {h['http_429']} · timeouts: {h['timeouts']}")
        f = h["fields"]
        if f["total"]:
            st.progress(min(1.0, f["completed"] / f["total"]), text=f"כל הרכבים: {h['fields_text']}")
            _rtl(f"עם ראיות: {f['with_evidence']:,} · ללא ראיות: {f['without_evidence']:,} · "
                 f"מידע סותר: {f['conflicting']:,} · דורשים המשך: {f['needs_followup']:,}")
        st.caption(h["progress_caption"])


def render_card(vehicle: VehicleLive, slot) -> None:
    c = vehicle.card()
    p = c["progress"]
    with slot.container(border=True):
        lines = [f"<b>{_e(c['title'])}</b>", f"שלב: <b>{_e(c['phase'])}</b>",
                 f"מודל: {_e(c['model'])} · {_e(c['op'])}"]
        if c["status"]:
            lines.append(f"סטטוס: {_e(c['status'])}")
        if c["field"]:
            lines.append(f"שדה נוכחי: <b>{_e(c['field'])}</b>")
        if c["why"]:
            lines.append(f"למה: {_e(c['why'])}")
        if c["action"]:
            detail = f"<br><span style='opacity:.7'>{_e(c['action']['detail'])}</span>" if c["action"]["detail"] else ""
            lines.append(f"פעולה: {_e(c['action']['label'])}{detail}")
        _rtl("<br>".join(lines))
        if p["total"]:
            st.progress(min(1.0, p["completed"] / p["total"]), text=c["progress_text"])
        recovery = c["recovery"]
        rec_text = f"Recovery: {recovery['text']}" + (f" · נותרו: {recovery['remaining']}"
                                                      if recovery["remaining"] is not None else "")
        harvest = c["harvest"]
        extra = (f" · נסרקו {harvest['documents']} מסמכים · נמצאו מועמדים ל־{harvest['fields_with_candidates']} שדות"
                 if harvest["documents"] else "")
        if c.get("evidence_rejected"):
            extra += f" · {he.EVIDENCE_REJECTED_HE}: {c['evidence_rejected']}"
        _rtl(f"עם ראיות: {p['with_evidence']} / {p['total']} · דורשים המשך: {p['needs_followup']} · "
             f"ללא ראיות: {p['without_evidence']} · סתירות פתוחות: {p['conflicting']}<br>{rec_text}{_e(extra)}")
        if c["reasoning"] is not None:
            with st.expander(c["reasoning_title"] + (f" · {c['reasoning_context']}" if c["reasoning_context"] else "")):
                st.text(c["reasoning"])
        rows = vehicle.field_rows()
        if rows:
            with st.expander(he.FIELD_TABLE_TITLE_HE):
                # right-to-left reading order: the first logical column (group) is shown at the right edge
                st.dataframe(pd.DataFrame(rows, columns=he.FIELD_TABLE_COLUMNS_HE[::-1]), hide_index=True,
                             width="stretch")
        with st.expander(he.DETAIL_LOG_TITLE_HE):
            st.code("\n".join(list(vehicle.lines)[-60:]) or "—", language=None)


class LiveBatchView:
    """Placeholders owned by the script thread; re-rendered from BatchLiveState."""

    def __init__(self, state: BatchLiveState):
        self.state = state
        self.header = st.empty()
        self.cards = {rid: st.empty() for rid in state.order}
        self._rendered: dict[str, tuple[int, float]] = {}
        self._header_at = 0.0

    def refresh(self, force: bool = False) -> None:
        changed = self.state.drain()
        now = time.monotonic()
        if force or now - self._header_at >= HEADER_MIN_INTERVAL_S:
            render_header(self.state, self.header)
            self._header_at = now
        for rid in self.state.order:
            vehicle = self.state.vehicles[rid]
            version, at = self._rendered.get(rid, (-1, 0.0))
            stale = vehicle.version != version and (rid in changed or now - at >= CARD_MIN_INTERVAL_S)
            if force or version == -1 or (stale and now - at >= CARD_MIN_INTERVAL_S):
                render_card(vehicle, self.cards[rid])
                self._rendered[rid] = (vehicle.version, now)


def run_live_batch(selection: list[dict], rows_by_id: dict[str, dict], run_one: Callable[[dict, dict], dict],
                   state: BatchLiveState, *, max_workers: int, cancel_event: threading.Event,
                   stats: dict | None = None, poll_interval_s: float = 0.2) -> list[dict]:
    """Run the batch on worker threads while this (script) thread renders the dashboard."""
    view = LiveBatchView(state)
    view.refresh(force=True)

    def on_done(vehicle: dict, result: dict) -> None:
        live = state.vehicles.get(vehicle["upstream_record_id"])
        if live is not None and result.get("worker_exception"):
            live.mark_error(result.get("error") or "worker error")

    results = run_batch(selection, rows_by_id, run_one, on_done=on_done, max_workers=max_workers,
                        cancel_event=cancel_event, on_poll=view.refresh, poll_interval_s=poll_interval_s, stats=stats)
    view.refresh(force=True)
    return results
