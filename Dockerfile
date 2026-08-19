# syntax=docker/dockerfile:1
FROM python:3.13-slim

# PYTHONUNBUFFERED: print logs immediately instead of buffering them, so
# `docker logs` shows output live. Without it, logs appear in delayed chunks.
# PYTHONDONTWRITEBYTECODE: skip .pyc files — useless in a throwaway container.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# curl is only here for the HEALTHCHECK below.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Requirements before source — same caching trick as the frontend.
# pip will print a dependency-conflict warning about opentelemetry.
# That is expected and documented in requirements.txt.
COPY requirements.txt .

# mistralai pins opentelemetry-semantic-conventions <0.61 while chromadb needs
# >=0.65b0. No single version satisfies both, so pip's resolver refuses when it
# sees them together. Install everything else first, then force the newer trio
# on top with --no-deps so pip does not re-resolve. The combination works in
# practice — see the note at the bottom of requirements.txt.
RUN grep -v '^opentelemetry-' requirements.txt > /tmp/base.txt \
 && pip install --no-cache-dir -r /tmp/base.txt \
 && pip install --no-cache-dir --no-deps \
      opentelemetry-api==1.44.0 \
      opentelemetry-sdk==1.44.0 \
      opentelemetry-semantic-conventions==0.65b0


COPY app ./app

# Uploaded workbooks, DuckDB files and the Chroma index live here.
# A Docker volume gets mounted on top of this path at run time.
ENV DATA_DIR=/data

# Run as a normal user, not root — same reasoning as nginx-unprivileged.
RUN useradd -m -u 1001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /app
USER appuser

EXPOSE 8000

# start-period is 30s, not 5s: importing pandas, duckdb and chromadb is slow.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
