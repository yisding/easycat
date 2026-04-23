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
#   docker build --build-arg EXTRAS="--extra openai-agents --extra silero-vad --extra deepgram" ...

# ── builder: resolve the virtualenv with uv ──────────────────────────────────
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS builder
WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

ARG EXTRAS="--extra openai-agents --extra silero-vad --extra rnnoise"

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev ${EXTRAS}

# ── runtime: slim Python base + the ready-made venv ──────────────────────────
FROM python:3.11-slim-bookworm AS runtime
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
RUN chmod +x /usr/local/bin/entrypoint.sh

USER easycat
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8765
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "examples/ws_server.py"]
