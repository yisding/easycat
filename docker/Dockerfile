# syntax=docker/dockerfile:1.7
#
# EasyCat WebSocket example image.
#
# Build from the repo root:
#   docker build -f docker/Dockerfile -t easycat:ws .
#
# Or via compose:
#   docker compose -f docker/compose.yaml up --build
#
# The default extras cover the `examples/ws_server.py` golden path.
# To swap providers, override EXTRAS at build time, e.g.:
#   docker build --build-arg EXTRAS="--extra openai-agents --extra silero-vad --extra rnnoise --extra deepgram --extra elevenlabs" ...

# ── builder: resolve the virtualenv with uv ──────────────────────────────────
FROM python:3.14-slim-bookworm@sha256:23c59390fc717bf09f9336908199a0ae75d9c4264bf296123f94ad772fea3b52 AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded /uv /uvx /bin/
WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

ARG EXTRAS="--extra openai --extra openai-agents --extra silero-vad --extra rnnoise"

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

RUN uv sync --frozen --no-dev ${EXTRAS}

# ── runtime: slim Python base + the ready-made venv ──────────────────────────
FROM python:3.14-slim-bookworm@sha256:23c59390fc717bf09f9336908199a0ae75d9c4264bf296123f94ad772fea3b52 AS runtime
WORKDIR /app

# ca-certificates: TLS to OpenAI/Deepgram/etc.
# libgomp1:        onnxruntime links against OpenMP.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      libgomp1 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 1000 --home-dir /app --shell /usr/sbin/nologin easycat

COPY --from=builder --chown=easycat:easycat /app /app
COPY --chown=easycat:easycat examples ./examples
COPY --chown=easycat:easycat docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY --chown=easycat:easycat docker/healthcheck.py /usr/local/bin/healthcheck.py
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/healthcheck.py \
 # EASYCAT_DATA_DIR (default ".easycat", i.e. /app/.easycat here) holds the
 # crash-durable SQLite journals, artifacts, crash-dumps, and archives that
 # `debug="full"` writes on every session (the `EasyConfig` default is the
 # in-memory `debug="light"`, which writes nothing here). Pre-create it
 # owned by the runtime user so a bind mount or named volume mounted here
 # does not need a container-side chown step. See "Persisting the journal"
 # in docs/deployment/docker.md.
 && mkdir -p /app/.easycat \
 && chown easycat:easycat /app/.easycat

USER easycat
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8765

# The journal directory is crash-durable ONLY if it survives container
# restarts/recreation — mount a named volume or bind mount here in
# production. See "Persisting the journal across restarts" in
# docs/deployment/docker.md.
VOLUME ["/app/.easycat"]

# Probes EASYCAT_HEALTH_URL (an HTTP /health/ready endpoint) when set, else
# falls back to a TCP connect against EASYCAT_WS_HOST/EASYCAT_WS_PORT — see
# docker/healthcheck.py for why the fallback exists for the default CMD.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "/usr/local/bin/healthcheck.py"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "examples/ws_server.py"]
