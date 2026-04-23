#!/usr/bin/env bash
# entrypoint.sh — fail fast on missing credentials, then exec the server.
set -euo pipefail

if [ -z "${OPENAI_API_KEY:-}" ]; then
    echo "error: OPENAI_API_KEY is not set" >&2
    echo "       pass it via \`-e OPENAI_API_KEY=...\` or in compose.yaml" >&2
    exit 1
fi

exec "$@"
