#!/bin/sh
# TRIPY production start (Railway / Docker): ONE Uvicorn process serving the FastAPI application, which serves the
# API (/api), the health check (/health), the React production bundle (frontend/dist) and, only when TRIPY_MCP_TOKEN
# is set, the read-only MCP (/mcp/<token>). Binds 0.0.0.0:$PORT (Railway injects PORT; 8000 when unset).
#
# Exactly one worker: the process-wide RunManager owns TRIPY_DATA_DIR (one process per data directory). Never add
# --workers, and never run a second server against the same data directory.
set -e
cd "$(dirname "$0")/.."

# Diagnostics: resolved paths and presence of configuration (never secret values). Never blocks startup:
# configuration problems are also reported by /api/config/status, and the health check must still pass.
python -m src.startup_check || echo "startup diagnostics failed (continuing)" >&2

# exec: the shell is replaced by Uvicorn, so the platform's SIGTERM reaches Uvicorn directly; the application's lifespan
# shutdown then asks active runs to stop at their next safe point (RunManager.shutdown, within Railway's draining
# window: at most 5 s for open requests, then TRIPY_SHUTDOWN_GRACE_S for the runs).
exec uvicorn src.api.app:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --timeout-graceful-shutdown 5 \
  --no-server-header
