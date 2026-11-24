# syntax=docker/dockerfile:1.7
#
# One image definition, four targets:
#   test    - the suite, run on Linux:   docker build --target test -t tmd-test . && docker run --rm tmd-test
#   bot     - trusted orchestrator: Telegram token, SQLite, no extractors' network access
#   worker  - untrusted extractor service: yt-dlp / gallery-dl / ffmpeg, NO token, NO database
#   proxy   - allowlisting egress proxy (the worker's only route to the internet)
#
# The base image is pinned by the digest of the registry's multi-arch index for
# python:3.12-slim-bookworm, read with `docker buildx imagetools inspect` on 2026-11-24.
# Refresh it deliberately (and re-run the suite + live probe) rather than floating on a tag.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:82836454a795d9d75549a9e157fd08dbac6c383292cf89e99f4248a97ce6b2ff

FROM ${PYTHON_IMAGE} AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tini reaps the child processes yt-dlp spawns (ffmpeg) so they cannot linger as zombies.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Two unprivileged identities that share ONE group. They exchange job files through a
# group-writable (never world-accessible) directory; nothing else is shared.
RUN groupadd --system --gid 10000 media \
 && useradd --system --uid 10001 --gid media --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin bot \
 && useradd --system --uid 10002 --gid media --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin worker

WORKDIR /app
COPY requirements.lock ./
# --require-hashes: every wheel must match the hash recorded in the lock file.
RUN pip install --require-hashes --no-deps -r requirements.lock
COPY app ./app

# ---------------------------------------------------------------------------------------------
FROM base AS test
COPY requirements-dev.lock ./
RUN pip install --require-hashes --no-deps -r requirements-dev.lock
COPY pyproject.toml .python-version .env.example PRD.md architecture.md tasks.md README.md ./
COPY scripts ./scripts
COPY ops ./ops
COPY tests ./tests
# Runs as root on purpose: the suite creates symlinks and checks permission bits.
CMD ["pytest", "-q", "-p", "no:cacheprovider"]

# ---------------------------------------------------------------------------------------------
FROM base AS bot
RUN mkdir -p /data /work && chown bot:media /data && chown bot:media /work && chmod 0700 /data && chmod 2770 /work
USER bot
ENV DATA_DIR=/data WORK_DIR=/work
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.main"]
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
  CMD ["python", "-m", "app.healthcheck"]

# ---------------------------------------------------------------------------------------------
FROM base AS worker
USER worker
ENV WORK_DIR=/work
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.extractors.service"]
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/v1/live', timeout=5).status == 200 else 1)"]

# ---------------------------------------------------------------------------------------------
FROM base AS proxy
USER worker
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.services.egress_proxy"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import socket,sys; s=socket.create_connection(('127.0.0.1',3128),3); s.close()"]
