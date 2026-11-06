# syntax=docker/dockerfile:1
# Image of the workspace terminals (ADR-0036): the official Claude Code and Codex CLIs,
# git, Node.js, Python and tmux, and ai-coding-agent: the background service behind the
# chat panel (ADR-0037). Users sign in to their own accounts inside the CLIs.
#
#   docker build -t ai-coding-workspace:local deploy/workspace
#
# The base is the digest-pinned Node image of deploy/Dockerfile; the CLIs are pinned to
# releases at least two weeks old. Containers run read-only, without capabilities, as the
# server's uid (any uid: see ai-coding-workspace-init).

ARG NODE_IMAGE=mirror.gcr.io/library/node@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d

FROM ${NODE_IMAGE}

ARG CLAUDE_CODE_VERSION=2.1.309
ARG CODEX_VERSION=0.163.0

LABEL org.opencontainers.image.title="ai-coding-workspace" \
      org.opencontainers.image.description="Workspace terminal of the AI Coding Platform: Claude Code, Codex, git"

RUN apt-get update \
 && apt-get install --no-install-recommends -y \
      git ca-certificates curl tmux libnss-wrapper python3 python3-venv python3-pip \
      ripgrep less nano jq \
 && rm -rf /var/lib/apt/lists/*

RUN npm install --global --no-audit --no-fund \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "@openai/codex@${CODEX_VERSION}" \
 && npm cache clean --force \
 && claude --version && codex --version && git --version

# uv: Python projects (uv run pytest, uv sync) without installing it first. uv fetches the
# Python a project needs (Debian's is 3.11) and keeps it, its cache and its downloads in the
# user's home, which outlives the container: only the first run of a project downloads.
# Declared here, not above: a build argument is part of the cache key of every later RUN.
ARG UV_VERSION=0.13.5
RUN python3 -m pip install --no-cache-dir --disable-pip-version-check --break-system-packages \
      "uv==${UV_VERSION}" \
 && uv --version

COPY ai-coding-workspace-init /usr/local/bin/ai-coding-workspace-init
COPY ai-coding-agent /usr/local/bin/ai-coding-agent
COPY tmux.conf /etc/tmux.conf
COPY profile.sh /etc/profile.d/ai-coding.sh

# nss_wrapper gives the container's uid a user ("dev", home /home/dev) without a writable
# /etc/passwd; Node (Claude Code, Codex) and ssh need one. UV_LINK_MODE: uv's cache (home)
# and the project (/workspace) are separate mounts, which cannot share hard links.
ENV LANG=C.UTF-8 \
    SHELL=/bin/bash \
    TMUX_TMPDIR=/tmp \
    DISABLE_AUTOUPDATER=1 \
    UV_LINK_MODE=copy \
    LD_PRELOAD=libnss_wrapper.so \
    NSS_WRAPPER_PASSWD=/tmp/passwd \
    NSS_WRAPPER_GROUP=/tmp/group

WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/ai-coding-workspace-init"]
CMD ["sleep", "infinity"]
