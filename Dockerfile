# TRIPY — one Railway service, one process: FastAPI (Uvicorn) serves the API, the React production bundle, /health and
# the optional read-only MCP; background research runs execute in the same process (src/jobs/manager.py).

# --- stage 1: the React production bundle (Node is never part of the runtime image) ---------------------------------
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# `npm run build` = `tsc -b && vite build`: a TypeScript error fails the image build
RUN npm run build

# --- stage 2: the Python runtime ---------------------------------------------------------------------------------------
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TRIPY_ENV=production \
    TRIPY_DATA_DIR=/data

WORKDIR /app

# Dependencies first (cached layer). All of them ship manylinux wheels; no compiler needed.
COPY requirements.txt .
RUN pip install -r requirements.txt

# render_page and the automatic render fallback of unreadable importer pages (src/tools/fetch.py): Playwright's official
# installer, Chromium's headless shell only, with the system libraries it needs. No other browser, no stealth plugin.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN python -m playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache

COPY . .
# Only the built bundle is kept under frontend/ (sources, configs and tests stay in the build stage).
RUN find frontend -mindepth 1 -maxdepth 1 -exec rm -rf {} +
COPY --from=frontend /frontend/dist ./frontend/dist

# Durable state lives under TRIPY_DATA_DIR. On Railway, mount a Volume at /data; without one this folder is the
# container's ephemeral disk (the configuration status then warns that runs are not persistent).
RUN mkdir -p /data && chmod +x scripts/start.sh

# Documentation only: the server binds $PORT (Railway injects it; 8000 when unset).
EXPOSE 8000

# Railway uses the healthcheckPath in railway.json; this keeps plain `docker run` honest too.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8000'), timeout=4)" || exit 1

# Runs as root on purpose: Railway mounts Volumes owned by root, so a non-root user could not write to /data.
CMD ["sh", "scripts/start.sh"]
