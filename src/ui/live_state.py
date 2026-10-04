"""Compatibility alias: the live run state moved to src/runstate/live_state.py (framework-neutral; the engine, the
MCP and the HTTP API import it from there). `src.ui.live_state` IS that module (same object), so existing imports and
monkeypatches keep working until the Streamlit UI is removed."""

import sys

from ..runstate import live_state as _live_state

sys.modules[__name__] = _live_state
