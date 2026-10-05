"""Server-side logging: stdout (Railway captures it), one line per record, secrets redacted.

Full tracebacks of failed research jobs are logged here, never rendered in the normal UI. The same lines also go to a
rotating file under the data root (`<TRIPY_DATA_DIR>/logs/tripy.log`, 5 files x 5 MB) so the read-only MCP
(src/mcp_server) can tail them; stdout logging is unchanged and a file that cannot be opened is skipped silently.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path

from .app_config import redact

_CONFIGURED = False
LOG_FILE_NAME = "tripy.log"
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
LOG_FILE_BACKUPS = 4          # tripy.log + tripy.log.1 .. .4 = 5 files x 5 MB
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


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


_MCP_PATH = re.compile(r"(?i)(/mcp/)[^\s?#\"]+")


def redact_path(text: str) -> str:
    """A request line / path as it may be logged: configured secrets removed and the /mcp/<token> segment hidden
    (the MCP's secret is its URL path)."""
    return redact(_MCP_PATH.sub(r"\1[redacted]", text))


class ServerLogRedactingFilter(logging.Filter):
    """Uvicorn's records keep their args for its own formatters (the access log formats (client, method, path,
    version, status)); the message and every string arg are scrubbed in place."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_path(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_path(a) if isinstance(a, str) else a for a in record.args)
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def redact_server_logs() -> None:
    """Idempotent: scrub secrets out of the web server's own loggers (uvicorn access and error logs)."""
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, ServerLogRedactingFilter) for f in logger.filters):
            logger.addFilter(ServerLogRedactingFilter())


def configure_logging() -> logging.Logger:
    """Idempotent: configures the `tripy` logger once per process."""
    global _CONFIGURED
    logger = logging.getLogger("tripy")
    if _CONFIGURED:
        return logger
    level = (os.environ.get("TRIPY_LOG_LEVEL") or "INFO").upper()
    logger.setLevel(getattr(logging, level, logging.INFO))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    file_handler = _file_handler()
    if file_handler is not None:
        logger.addHandler(file_handler)
    logger.propagate = False
    _CONFIGURED = True
    return logger


def log_file_path() -> Path:
    """`<data root>/logs/tripy.log` (Railway: /data/logs/tripy.log, on the Volume)."""
    from .storage.paths import resolve_paths

    return resolve_paths().data_dir / "logs" / LOG_FILE_NAME


def _file_handler() -> logging.Handler | None:
    try:
        path = log_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=LOG_FILE_MAX_BYTES,
                                                       backupCount=LOG_FILE_BACKUPS, encoding="utf-8", delay=True)
    except OSError:
        return None           # a read-only or missing data root never costs stdout logging
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(RedactingFilter())
    return handler


def get_logger(name: str = "") -> logging.Logger:
    configure_logging()
    return logging.getLogger(f"tripy.{name}" if name else "tripy")
