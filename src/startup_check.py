"""Startup diagnostics, run by scripts/start.sh before Streamlit starts.

    python -m src.startup_check

Prints the resolved configuration (presence flags and paths only, never secret values) and creates the data
folders. It never blocks startup: a configuration problem is reported here and, precisely, in the UI, so the
health check still passes and the operator can read what is missing.
"""

from __future__ import annotations

import json
import sys

from .app_config import diagnostics
from .server_logging import get_logger
from .storage.paths import resolve_paths


def main() -> int:
    log = get_logger("startup")
    try:
        resolve_paths().ensure()
    except OSError as exc:
        log.error("could not create the data folders: %s", exc)
    info = diagnostics()
    log.info("TRIPY startup diagnostics: %s", json.dumps({k: v for k, v in info.items() if k != "checks"},
                                                         ensure_ascii=False))
    for check in info["checks"]:
        line = f"{check['name']}: {check['status']}" + (f" — {check['detail']}" if check["detail"] else "")
        {"ok": log.info, "warning": log.warning}.get(check["level"], log.error)("check %s", line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
