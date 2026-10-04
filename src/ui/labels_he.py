"""Compatibility alias: the Hebrew presentation labels moved to src/presentation/labels_he.py (framework-neutral).
`src.ui.labels_he` IS that module (same object), so existing imports keep working until the Streamlit UI is
removed."""

import sys

from ..presentation import labels_he as _labels_he

sys.modules[__name__] = _labels_he
