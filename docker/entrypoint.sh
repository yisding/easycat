#!/usr/bin/env bash
# entrypoint.sh — fail fast on missing/inconsistent config, then exec the server.
set -euo pipefail

if [ -z "${OPENAI_API_KEY:-}" ]; then
    echo "error: OPENAI_API_KEY is not set" >&2
    echo "       pass it via \`-e OPENAI_API_KEY=...\` or in compose.yaml" >&2
    exit 1
fi

# A non-loopback WebSocket bind with no token would serve an unauthenticated
# voice endpoint to anything that can reach the container's network. The app
# itself raises ValueError at start() for this (enforce_bind_guard); fail
# here too so a misconfigured container exits before the slower Python
# import/model-load path.
ws_host="${EASYCAT_WS_HOST:-127.0.0.1}"
if [ -z "${EASYCAT_WS_TOKEN:-}" ]; then
    case "$ws_host" in
        127.0.0.1 | localhost | ::1) ;;
        *)
            echo "error: EASYCAT_WS_HOST=$ws_host is not loopback, but EASYCAT_WS_TOKEN is unset" >&2
            echo "       set EASYCAT_WS_TOKEN, or keep EASYCAT_WS_HOST on loopback behind your own ingress auth" >&2
            exit 1
            ;;
    esac
fi

# EASYCAT_DATA_DIR (default ".easycat", relative to WORKDIR /app) holds the
# crash-durable journal, artifacts, crash-dumps, and archive directories
# `debug="full"` writes on every session (the `EasyConfig` default is the
# in-memory `debug="light"`, which writes nothing here). Warn — do not fail —
# when it is not writable: deployments that keep the default `debug="light"`
# (or set `debug="off"`) write no journal here and never mount anything.
data_dir="${EASYCAT_DATA_DIR:-.easycat}"
data_dir_ancestor="$data_dir"
while [ ! -e "$data_dir_ancestor" ]; do
    parent_dir="$(dirname -- "$data_dir_ancestor")"
    if [ "$parent_dir" = "$data_dir_ancestor" ]; then
        break
    fi
    data_dir_ancestor="$parent_dir"
done
if [ ! -w "$data_dir_ancestor" ] || [ ! -x "$data_dir_ancestor" ]; then
    if [ -e "$data_dir" ]; then
        echo "warning: EASYCAT_DATA_DIR=$data_dir exists but is not writable by this user" >&2
    else
        echo "warning: EASYCAT_DATA_DIR=$data_dir cannot be created; nearest existing ancestor $data_dir_ancestor is not writable by this user" >&2
    fi
    echo "         journals will not persist; chown it to uid 1000 (the 'easycat' user) on the host/volume" >&2
fi

# EASYCAT_JOURNAL_LITESTREAM_REPLICA only takes effect with journal_backend=
# "sqlite+litestream"; LitestreamSqliteJournal falls back to plain SQLite
# (with a log warning) when the replica URL or the `litestream` binary is
# missing, so this is a warning, not a fail-fast — but it is cheap to catch
# here before a session silently loses replication.
if [ -n "${EASYCAT_JOURNAL_LITESTREAM_REPLICA:-}" ] && ! command -v litestream >/dev/null 2>&1; then
    echo "warning: EASYCAT_JOURNAL_LITESTREAM_REPLICA is set but no 'litestream' binary is on PATH" >&2
    echo "         this image does not bundle litestream; run it as a sidecar container against the" >&2
    echo "         same journal volume, or rebuild with litestream installed in the runtime stage" >&2
    echo "         see 'Litestream replication' in docs/deployment/docker.md" >&2
fi

exec "$@"
