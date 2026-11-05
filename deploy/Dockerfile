# AI Coding Platform — Self-Hosted server image (ТЗ §15, B-708).
# Two stages: build the wheel, then a slim runtime that runs as a non-root user.
# Base images are pinned by digest (mirror.gcr.io mirrors Docker Hub's official images).
ARG PYTHON_IMAGE=mirror.gcr.io/library/python@sha256:da54eb86849fe19be7584d8b3f291fcf0c53c196760ab3d70dc6382f681ef9c0

ARG NODE_IMAGE=mirror.gcr.io/library/node@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d

FROM ${NODE_IMAGE} AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM ${PYTHON_IMAGE} AS build
WORKDIR /src
RUN python -m pip install --no-cache-dir --disable-pip-version-check "build==1.3.0"
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN python -m build --wheel --outdir /dist

FROM ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.title="ai-coding-platform" \
      org.opencontainers.image.source="https://github.com/aitmaganbetov/ai-coding-platform" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"
# git: cloning project repositories; docker CLI (workers only) talks to the daemon
# that starts the sandboxes — the sandboxes themselves never get the socket.
RUN apt-get update \
 && apt-get install --no-install-recommends -y git ca-certificates docker.io \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 aicoding \
 && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin aicoding
COPY --from=build /dist/*.whl /tmp/
COPY --from=web /web/dist /opt/ai-coding/ui
RUN python -m pip install --no-cache-dir --disable-pip-version-check \
      "$(ls /tmp/*.whl)[server,postgres]" \
 && rm /tmp/*.whl
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AI_CODING_SERVER_DATA_DIR=/srv/ai-coding \
    TMPDIR=/srv/ai-coding/tmp
RUN mkdir -p /srv/ai-coding/tmp && chown -R aicoding:aicoding /srv/ai-coding
USER aicoding
WORKDIR /srv/ai-coding
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=4).status!=200)"
ENTRYPOINT ["ai-coding", "server"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000", "--ui", "/opt/ai-coding/ui"]
