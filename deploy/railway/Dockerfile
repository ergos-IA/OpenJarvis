# OpenJarvis image for Railway: one container = one person's assistant.
#
# The same image also runs the AI Fast Track admin console (JARVIS_ROLE=admin).
#
# Differs from deploy/docker/Dockerfile in that it:
#   - installs the OpenAI *and* Anthropic SDKs (inference-cloud extra),
#   - bakes the WhatsApp Baileys bridge (Node runtime + node_modules) in,
#   - starts through deploy/railway/entrypoint.sh, which honours $PORT, fixes
#     volume ownership and renders config.toml from environment variables.

# The upstream React SPA is deliberately not built: users only see the AI
# Fast Track pages served by server/jarvis_ui_routes.py.

# Stage 1: Build the WhatsApp Baileys bridge
FROM node:22.23.0-slim@sha256:d9f850096136edbc402debdd8729579a288aac64574ada0ff4db26b6ae58b0b2 AS bridge
RUN apt-get update && \
    apt-get install -y --no-install-recommends git ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    git config --global url."https://github.com/".insteadOf ssh://git@github.com/
WORKDIR /bridge
COPY src/openjarvis/channels/whatsapp_baileys_bridge/package.json \
     src/openjarvis/channels/whatsapp_baileys_bridge/package-lock.json \
     src/openjarvis/channels/whatsapp_baileys_bridge/tsconfig.json ./
RUN npm ci
COPY src/openjarvis/channels/whatsapp_baileys_bridge/src ./src
RUN npx tsc && npm prune --omit=dev

# Stage 2: Build Python package
FROM python:3.12.13-slim-bookworm@sha256:76d4b7b6305788c6b4c6a19d6a22a3921bf802e9af4d5e1e5bd771208dba74bf AS builder

RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

ENV PATH="/root/.cargo/bin:${PATH}"

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | \
        sh -s -- -y --profile minimal --default-toolchain none && \
    rustup toolchain install 1.89 --profile minimal && \
    rustup default 1.89

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN pip install --no-cache-dir uv && \
    uv export --frozen --no-dev --extra server --extra inference-cloud --extra speech \
        --no-emit-project > requirements.txt && \
    uv pip install --system --no-deps -r requirements.txt && \
    uv pip install --system --no-deps "maturin>=1.12.6,<2"

COPY src/ src/
COPY rust/ rust/
COPY scripts/install scripts/install
COPY deploy/windows deploy/windows

RUN uv pip install --system --no-deps . && \
    maturin build --release \
        --manifest-path rust/crates/openjarvis-python/Cargo.toml \
        --interpreter python3 \
        --out /tmp/openjarvis-rust-wheel && \
    uv pip install --system --no-deps /tmp/openjarvis-rust-wheel/*.whl && \
    python3 -c "import openjarvis_rust; print('openjarvis_rust ok')" && \
    python3 -m pip uninstall -y maturin && \
    rm -rf /tmp/openjarvis-rust-wheel rust

# Stage 3: Runtime
FROM python:3.12.13-slim-bookworm@sha256:76d4b7b6305788c6b4c6a19d6a22a3921bf802e9af4d5e1e5bd771208dba74bf

COPY --from=builder /usr/local /usr/local
COPY --from=builder /app /app
# Node runtime (no npm needed: node_modules are pre-installed).
COPY --from=bridge /usr/local/bin/node /usr/local/bin/node
COPY --from=bridge /bridge /opt/baileys-bridge
COPY deploy/railway/entrypoint.sh /usr/local/bin/railway-entrypoint
COPY deploy/railway/bootstrap_config.py /usr/local/lib/openjarvis/bootstrap_config.py
WORKDIR /app

RUN chmod 0755 /usr/local/bin/railway-entrypoint && \
    groupadd --system --gid 10001 openjarvis && \
    useradd --system --uid 10001 --gid openjarvis \
        --create-home --home-dir /home/openjarvis openjarvis

ENV HOME=/home/openjarvis \
    OPENJARVIS_HOME=/data \
    OPENJARVIS_BAILEYS_BRIDGE_DIR=/opt/baileys-bridge \
    OPENJARVIS_NO_UPDATE_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Madrid

# The entrypoint starts as root only to chown the mounted volume, then drops
# to the unprivileged openjarvis user before running the server.
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/railway-entrypoint"]
