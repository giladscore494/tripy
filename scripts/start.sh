#!/bin/sh
# TRIPY production start (Railway / Docker). Binds 0.0.0.0:$PORT (Railway injects PORT; 8501 locally).
set -e
cd "$(dirname "$0")/.."

# Diagnostics: resolved paths and presence of configuration (never secret values). Never blocks startup:
# configuration problems are also shown precisely in the UI, and the health check must still pass.
python -m src.startup_check || echo "startup diagnostics failed (continuing)" >&2

exec streamlit run app.py \
  --server.address=0.0.0.0 \
  --server.port="${PORT:-8501}" \
  --server.headless=true \
  --server.fileWatcherType=none \
  --server.runOnSave=false \
  --client.showErrorDetails=none \
  --browser.gatherUsageStats=false
