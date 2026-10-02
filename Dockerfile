# TRIPY — one Railway service: Streamlit UI + background research runs in the same process.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TRIPY_ENV=production \
    TRIPY_DATA_DIR=/data \
    PORT=8501

WORKDIR /app

# Dependencies first (cached layer). All of them ship manylinux wheels; no compiler needed.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Durable state lives under TRIPY_DATA_DIR. On Railway, mount a Volume at /data; without one this folder is the
# container's ephemeral disk (the app then warns that runs are not persistent).
RUN mkdir -p /data && chmod +x scripts/start.sh

EXPOSE 8501

# Railway uses the healthcheckPath in railway.json; this keeps plain `docker run` honest too.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/_stcore/health' % os.environ.get('PORT','8501'), timeout=4)" || exit 1

# Runs as root on purpose: Railway mounts Volumes owned by root, so a non-root user could not write to /data.
CMD ["sh", "scripts/start.sh"]
