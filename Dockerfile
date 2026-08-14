# =============================================================================
# Turnstone — Docker build with uv for reproducible, locked installs
# Single image for all services: server, console, channel, eval
# =============================================================================

FROM python:3.14-slim

LABEL org.opencontainers.image.title="turnstone" \
      org.opencontainers.image.description="Multi-node AI orchestration platform"

COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /usr/local/bin/uv

# Remove the slim image's man page exclusion so man-db has actual content
RUN rm -f /etc/dpkg/dpkg.cfg.d/docker

# System dependencies: psycopg (libpq5), developer tooling for agent workflows.
# ripgrep is the preferred backend for the search tool — natively bounds
# per-line, per-file, and per-filesize so pathological inputs (minified
# bundles, training-data JSONL with multi-MB single records) can't OOM us.
# ffmpeg transcodes omni STT uploads (browser webm/opus) to the 16 kHz mono
# WAV the omni chat-audio lane decodes.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    libpq5 git curl jq man-db manpages procps file ripgrep ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Node.js LTS (for npx-based MCP servers like @modelcontextprotocol/server-github)
COPY --from=node:24-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:24-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# Non-root user
RUN useradd --create-home --shell /bin/bash turnstone

WORKDIR /app

# Install dependencies first (cached layer — only re-runs when deps change)
COPY pyproject.toml uv.lock README.md LICENSE NOTICE THIRD-PARTY-NOTICES ./
RUN uv sync --frozen --no-install-project --no-dev \
    --no-compile --extra all

# Install the project itself
COPY turnstone/ turnstone/
RUN uv sync --frozen --no-dev \
    --no-compile --extra all

# Compile bytecode in a separate step (avoids fd exhaustion during install)
RUN python -m compileall -q .venv turnstone/

# Add venv to PATH so entry points are found
ENV PATH="/app/.venv/bin:$PATH"

# Health check script (stdlib only, no pip deps needed)
COPY docker/healthcheck.py /usr/local/bin/healthcheck.py

# Entrypoint script — runs migrations before starting
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# Data directory — SQLite DB is created in CWD
WORKDIR /data
RUN chown turnstone:turnstone /data

# Nix config only — the STORE itself is NOT baked in.  A ~1GB /nix layer made
# the image heavy and, worse, ephemeral: every toolchain downloaded at runtime
# was lost on the next rebuild.  Instead compose populates a persistent
# nix-store volume once, via a one-shot init service (see compose.yaml), and
# every node mounts it.  That keeps this image slim and makes toolchains
# survive rebuilds.  Without that volume, setup_env simply reports Nix as
# unavailable and dispatch falls back to the base image's runtimes.
RUN mkdir -p /etc/nix \
    && printf 'experimental-features = nix-command flakes\nsandbox = false\nbuild-users-group =\n' > /etc/nix/nix.conf
ENV PATH="/nix/var/nix/profiles/default/bin:${PATH}"

# Pre-create the agent credential directories, owned by the runtime user.
# Docker creates a missing bind-mount PARENT as root, which leaves the CLI
# unable to write its own state next to the mounted credential file
# (opencode fails with EACCES on mkdir .../opencode/repos). Creating them here
# means a file mount lands in an already-correct directory.
RUN mkdir -p /home/turnstone/.local/share/opencode /home/turnstone/.claude \
    && chown -R turnstone:turnstone /home/turnstone

# Workspace mount point — bind-mount a host directory here
RUN mkdir -p /workspace && chown turnstone:turnstone /workspace

# Coding-agent CLIs for dispatch (turnstone/core/agents/*).  The image already
# carries node+npm for these; each is optional at runtime — the adapter reports
# a clean "not installed" instead of failing the workstream, so a slimmer build
# can drop this layer.  Credentials are NOT baked in: they arrive per-run as env
# (console settings) or via a read-only mount of the operator's host login.
RUN npm install -g --no-fund --no-audit \
        @anthropic-ai/claude-code \
        opencode-ai \
    && npm cache clean --force \
    && claude --version && opencode --version

USER turnstone

ENTRYPOINT ["entrypoint.sh"]

# Default command (overridden per service in compose.yaml)
CMD ["turnstone-server", "--host", "0.0.0.0", "--port", "8080"]
