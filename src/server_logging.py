"""Server-side logging: stdout (Railway captures it), one line per record, secrets redacted.

Full tracebacks of failed research jobs are logged here, never rendered in the normal UI.
"""

from __future__ import annotations

import logging
import os
import sys

from .app_config import redact

_CONFIGURED = False


class RedactingFilter(logging.Filter):
    """Scrub secret values out of every record (message, args and formatted traceback)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            message = str(record.msg)
        record.msg, record.args = redact(message), None
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def configure_logging() -> logging.Logger:
    """Idempotent: configures the `tripy` logger once per process."""
    global _CONFIGURED
    logger = logging.getLogger("tripy")
    if _CONFIGURED:
        return logger
    level = (os.environ.get("TRIPY_LOG_LEVEL") or "INFO").upper()
    logger.setLevel(getattr(logging, level, logging.INFO))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    logger.propagate = False
    _CONFIGURED = True
    return logger


def get_logger(name: str = "") -> logging.Logger:
    configure_logging()
    return logging.getLogger(f"tripy.{name}" if name else "tripy")
