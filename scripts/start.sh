#!/bin/sh
# TRIPY production start (Railway / Docker). Binds 0.0.0.0:$PORT (Railway injects PORT; 8501 locally).
set -e
cd "$(dirname "$0")/.."

# Diagnostics: resolved paths and presence of configuration (never secret values). Never blocks startup:
# configuration problems are also shown precisely in the UI, and the health check must still pass.
python -m src.startup_check || echo "startup diagnostics failed (continuing)" >&2

# TRIPY_MCP_TOKEN set: the same server and app.py through tripy_server.py (st.App), plus the read-only MCP at
# /mcp/<TRIPY_MCP_TOKEN> (src/mcp_server). Unset or empty: exactly the plain command below, no MCP at all.
if [ -n "$(printf '%s' "${TRIPY_MCP_TOKEN:-}" | tr -d '[:space:]')" ]; then
  exec streamlit run tripy_server.py \
    --server.address=0.0.0.0 \
    --server.port="${PORT:-8501}" \
    --server.headless=true \
    --server.fileWatcherType=none \
    --server.runOnSave=false \
    --client.showErrorDetails=none \
    --browser.gatherUsageStats=false
fi

exec streamlit run app.py \
  --server.address=0.0.0.0 \
  --server.port="${PORT:-8501}" \
  --server.headless=true \
  --server.fileWatcherType=none \
  --server.runOnSave=false \
  --client.showErrorDetails=none \
  --browser.gatherUsageStats=false
