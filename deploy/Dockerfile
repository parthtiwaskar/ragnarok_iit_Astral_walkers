# syntax=docker/dockerfile:1
# Build from the repository root:   docker build -f deploy/Dockerfile -t ragnarisk .
FROM python:3.12-slim

# Where the Runemark pack lives in the build context (team repo: data/runemark).
ARG PACK_DIR=data/runemark

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    NINE_REALMS_ENV=production HOST=0.0.0.0 PORT=8765 \
    DATA_DIR=/data FASTEMBED_CACHE=/models RUNEMARK_PACK=/app/pack IMPORT_RUNS_DIR=/app/runs

WORKDIR /app
COPY deploy/requirements-prod.txt ./requirements-prod.txt
RUN pip install -r requirements-prod.txt

# Bake the embedding model into the image: no download at runtime, fast first start.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/models')"

COPY nine_realms ./nine_realms
COPY index.html app.js charts.js theme.js styles.css ./
COPY ${PACK_DIR} ./pack
COPY runs ./runs

# The app runs as an unprivileged user (uid 10001). /data is the only place it writes (database + agent memory).
# The entrypoint starts as root only to fix ownership of a freshly mounted disk, then drops to that user.
RUN useradd --create-home --uid 10001 app && mkdir -p /data /models && chown -R app:app /data /models /app
COPY --chmod=755 deploy/entrypoint.sh /entrypoint.sh
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8765')+'/api/health',timeout=4)"

ENTRYPOINT ["/entrypoint.sh"]
