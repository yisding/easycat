# Running EasyCat in Docker

This guide packages the WebSocket example (`examples/ws_server.py`) as a
container.  Silero VAD and Smart-Turn ONNX models ship inside
`src/easycat/models/`, so they are embedded in the image — nothing is
fetched at first-request time besides the calls to OpenAI.
From this repository, run `uv run easycat docs --audience operators` for the
operator-facing route slice covering deployment, observability, and journal
durability. Use `uv run easycat docs --audience operators --json` when
automation needs that same operator map with command hints.

## Quickstart

```bash
export OPENAI_API_KEY=sk-...
export EASYCAT_WS_TOKEN=$(uv run python - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)
docker compose -f docker/compose.yaml up --build
```

The compose service publishes the container only on host loopback
(`127.0.0.1:8765`) and requires the token above before it creates a
provider-backed EasyCat session.  Non-browser clients should send
`Authorization: Bearer $EASYCAT_WS_TOKEN`.
Inside the container, Compose explicitly sets `EASYCAT_WS_HOST=0.0.0.0`
so Docker's loopback-published host port can reach the process; keep the
host-side bind on loopback or put equivalent ingress controls in front.
Compose also sets `EASYCAT_WS_ALLOW_QUERY_TOKEN=1` for the bundled browser
demo. This opt-in is appropriate only because the published host port remains
loopback-only; production clients should keep it unset and use the bearer
header.

For the browser example, serve the static client from the repo in a
second terminal:

```bash
python -m http.server 8080 --directory examples
```

Then open
`http://localhost:8080/ws_browser_client.html?token=<EASYCAT_WS_TOKEN>`.
The page derives the WebSocket host from `localhost` and connects to
`ws://localhost:8765` automatically.

Browsers cannot set headers on the WebSocket handshake, so
`ws_browser_client.html` uses the token query parameter. The example server
maps the explicit `EASYCAT_WS_ALLOW_QUERY_TOKEN=1` environment setting to
`allow_query_token=True`; without that opt-in, query auth remains off. This
keeps a direct `uv run python examples/ws_server.py` launch header-only unless
the developer deliberately enables the local browser flow.
The same browser limitation applies whenever a bundled-browser demo is
token-protected: it needs the explicit query-token opt-in even on loopback.

To stop:

```bash
docker compose -f docker/compose.yaml down
```

Press `Ctrl-C` in the static-client terminal if you started one.

## Secrets: use a `.env` file, don't bake them in

Prefer an env file passed to Compose over passing secrets as build args:

```bash
# docker/.env  — git-ignored, never copied into the image
OPENAI_API_KEY=sk-...
EASYCAT_WS_TOKEN=<random-long-token>
# Optional: EASYCAT_WS_MAX_SESSIONS=10
```

Then point Compose at that file explicitly:

```bash
docker compose --env-file docker/.env -f docker/compose.yaml up --build
```

The repo's `.dockerignore` excludes `.env` and `.env.*` files at any depth
from the build context as a second line of defence while still allowing
`.env.example` templates — if you fork the Dockerfile to use a wildcard
`COPY . /app`, secrets still won't ship.
It also excludes local TLS certificate and private-key files matching
`**/*.pem` and `**/*.key`.
It also excludes common local tool caches and coding-agent state such as
`.hypothesis/`, `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`,
`.uv-cache/`, `.agents/`, `.codex`, `.codex/`, `.claude/`, and
`.pipecat-bench/` so local generated state is not uploaded as part of
`docker compose ... up --build`.
Generated reports and docs sites such as `.coverage`, `.coverage.*`,
`coverage.xml`, `htmlcov/`, `site/`, `mutants/`, and `.mutmut-cache` are
excluded for the same reason.

Never use `ARG OPENAI_API_KEY=...` in the Dockerfile: build args are
recoverable from image history.

## What the image contains

- `python:3.14-slim-bookworm` runtime
- EasyCat installed with the Dockerfile `EXTRAS` default: `openai`,
  `openai-agents`, `silero-vad`, `rnnoise`
- Bundled Silero VAD and Smart-Turn v3.2 ONNX models
- Runs as a non-root `easycat` user (uid 1000)
- Exposes TCP 8765 (WebSocket PCM16 audio); compose binds it to host loopback by default
- `HEALTHCHECK` running `docker/healthcheck.py` every 30s (see
  [Container health checks](#container-health-checks) below)
- `VOLUME /app/.easycat`, pre-created and owned by the `easycat` user (see
  [Persisting the journal across restarts](#persisting-the-journal-across-restarts))

Final image size is roughly 450 MB on amd64.

## Container health checks

`docker compose ps` and orchestrator readiness probes should reflect real
server state, not just "the process is still running". The image ships
`docker/healthcheck.py` as its `HEALTHCHECK`, copied to
`/usr/local/bin/healthcheck.py`:

- **Default CMD (`examples/ws_server.py`).** This is a raw `websockets`
  server — it speaks the WebSocket handshake only and does not serve an HTTP
  endpoint, so `examples/ws_server.py` cannot be probed with the framework's
  real `/health/ready` readiness endpoint
  (`src/easycat/server/health.py`, wired by `src/easycat/server/routes.py`).
  The healthcheck falls back to a TCP connect against `EASYCAT_WS_HOST`
  (`0.0.0.0` is treated as loopback for the probe itself) /
  `EASYCAT_WS_PORT`. This confirms the listener accepts connections; it does
  **not** confirm draining state, capacity, or (for a manifest-backed
  server) that the provider plan loaded cleanly.
- **A `VoiceServer`-based CMD.** If you swap the image's `CMD` for a script
  built on `easycat.server.VoiceServer` (`run_webrtc_config_server()`, a
  custom `VoiceServer.from_app(...)`, or the `easycat serve` CLI), that
  process serves the real three-tier health family: `GET /health/live`
  (loop responsiveness), `GET /health/ready` (draining / capacity /
  route-stack / manifest+plan checks — 200 only when all pass), and
  `GET /health` (the full JSON snapshot). Point the same healthcheck script
  at it instead of rebuilding the image:

  ```bash
  docker run ... -e EASYCAT_HEALTH_URL=http://127.0.0.1:8080/health/ready easycat:ws
  ```

  `EASYCAT_HEALTH_URL` set to anything makes `docker/healthcheck.py` do a
  plain HTTP GET and require a 2xx response instead of the TCP fallback —
  no image rebuild needed to switch modes. Compose's `healthcheck:` block
  in `docker/compose.yaml` only overrides the timing (`interval`/`timeout`/
  `retries`/`start_period`), so it inherits whichever probe the running
  container's environment selects.

## Persisting the journal across restarts

EasyCat's crash-durability promise (see
[`src/easycat/runtime/DURABILITY.md`](../../src/easycat/runtime/DURABILITY.md))
only holds if you opt into a durable journal *and* the journal directory
survives container restarts and recreation. The `EasyConfig` default is
`debug="light"` — an in-memory journal that writes nothing to disk — and the
default `examples/ws_server.py` CMD (`EasyConfig(transport=..., agent=...)`,
no `debug` argument) inherits it, so out of the box the container persists
nothing and needs no mount.

Set `debug="full"` (edit `examples/ws_server.py`, or point the CMD at your
own config script) to turn on the crash-survivable journal: it writes every
session's SQLite journal, artifacts, crash-dumps, and retention archive under
`EASYCAT_DATA_DIR` (default `.easycat`, i.e. `/app/.easycat` given the image's
`WORKDIR`). Once `debug="full"` is on, a container filesystem without a mount
there is ephemeral: `docker compose down` (and any `docker rm`/redeploy)
silently discards every journal, breaking that promise — mount a named volume
or bind mount to preserve it.

The Dockerfile declares `VOLUME ["/app/.easycat"]` and pre-creates that
directory owned by the `easycat` user (uid 1000) so a bind mount or named
volume dropped on top of it does not need a container-side `chown` step.
`docker/compose.yaml` mounts a named volume there by default:

```yaml
volumes:
  - easycat-journal:/app/.easycat
```

To use a host bind mount instead (for direct host-side backup tooling),
override the volume line with a host path and `chown` it to uid 1000 once:

```bash
sudo mkdir -p /srv/easycat-journal && sudo chown 1000:1000 /srv/easycat-journal
```

```yaml
volumes:
  - /srv/easycat-journal:/app/.easycat
```

**Inspecting a persisted journal** from the host (no running container
required — SQLite files are safe to read while the container is stopped, and
`easycat` CLI commands work against the mounted path directly):

```bash
uv run easycat bundles show /srv/easycat-journal/journals/<session_id>.sqlite
uv run easycat journal follow /srv/easycat-journal/journals/<session_id>.sqlite
```

**Backup.** The simplest approach is periodic filesystem-level backup of the
volume/bind mount (the SQLite files are WAL-mode; snapshot them with the
container stopped, or use a filesystem/volume snapshot tool that supports
consistent snapshots of open files). For continuous off-host replication
instead of periodic snapshots, see Litestream below.

The default `debug="light"` already keeps journals in memory only (no disk
writes, no mount needed); set `debug="off"` on `EasyConfig`/`SessionConfig`
to disable recording entirely (e.g. a stateless demo). The entrypoint's
writability check is a warning, not a hard failure, so neither mode blocks
startup when nothing is mounted at `/app/.easycat`.

## Litestream and libSQL replicas in a container

The app selects its journal backend through `journal_backend=` on
`EasyConfig`/`SessionConfig`; the Dockerfile's example CMD does not set one
today, so wire it into your own `config()` factory (see "Swapping STT / TTS
providers" above for the same mount-your-own-script pattern). The right value
depends on whether replication runs outside or inside the app container.

### Litestream

Ships WAL segments continuously (about every second) to object storage,
bounding the kernel-crash loss window to the replication interval instead of
the OS dirty-page writeback window.

The `litestream` binary itself is **not** bundled in this image (keeping the
runtime stage minimal). `LitestreamSqliteJournal` degrades to plain SQLite
with a log warning if the binary is missing, and the entrypoint now prints
the same warning at container start so missing replication is visible.
Available topologies are:

- **Directory-watcher sidecar**: keep the app on plain
  `journal_backend="sqlite"` and mount the same `easycat-journal` volume into
  the Litestream service. EasyCat creates
  `/app/.easycat/journals/{session_id}.sqlite` as sessions start. Pin a
  Litestream release that supports the
  [directory watcher](https://litestream.io/guides/directory-watcher/)
  (for example `litestream/litestream:0.5.14`) and configure:

  ```yaml
  dbs:
    - dir: /app/.easycat/journals
      pattern: "*.sqlite"
      watch: true
      replica:
        url: s3://your-bucket/easycat-journals
  ```

  The sidecar discovers databases created after startup and namespaces each
  remote replica by the database's relative path. Put replica credentials in
  the sidecar, not `EASYCAT_JOURNAL_LITESTREAM_REPLICA` on the app.
- **Bundle the binary**: add `litestream` to the runtime stage in a fork of the
  Dockerfile (download the static binary in the `runtime` stage before `USER
  easycat`), select `journal_backend="sqlite+litestream"`, and set:

  ```bash
  EASYCAT_JOURNAL_LITESTREAM_REPLICA=s3://your-bucket/easycat-journals
  ```

  `LitestreamSqliteJournal` starts the bundled process. If the binary or
  replica variable is absent, it deliberately falls back to plain SQLite with
  a warning.

Credentials for the replica target (e.g. `LITESTREAM_ACCESS_KEY_ID` /
`LITESTREAM_SECRET_ACCESS_KEY` for S3) follow Litestream's own environment
variable contract — pass them the same way you pass `OPENAI_API_KEY`, via
`-e` or a `.env` file, never baked into the image.

### libSQL (`journal_backend="libsql"`)

Embedded replica with async remote sync instead of a sidecar process —
requires the `libsql_experimental` SDK (an optional dependency the factory
falls back from if it's missing) and:

```bash
EASYCAT_LIBSQL_URL=libsql://your-db.turso.io
EASYCAT_LIBSQL_AUTH_TOKEN=...
```

Sync interval defaults to 10s (`EASYCAT_JOURNAL_LIBSQL_SYNC_INTERVAL_S`).
libSQL does **not** implement this framework's crash-recovery/crash-dump
promotion (see DURABILITY.md's "Backend support" section) — prefer
`sqlite+litestream` when crash-recovery semantics on reused session ids
matter (using either Litestream topology above), and reach for libSQL when a
managed remote-replica target outweighs that gap.

## Scraping metrics

The Dockerfile's default `examples/ws_server.py` CMD does not expose HTTP
metrics — it is a raw WebSocket-only process. Metrics scraping applies to a
`VoiceServer`-based CMD (see [Container health checks](#container-health-checks)
above for the same swap):

- **`GET /metrics`** — a read-only, PII-safe JSON snapshot of the in-process
  server counters/gauges (`VoiceServer.metrics_payload()`); it does not
  require an OTel SDK and is stable to poll directly (e.g. with a sidecar
  `curl` + your own metrics pipeline, or a Prometheus `json_exporter`).
  Requires the same bearer token as `/webrtc/*` when the server has an auth
  policy configured.
- **OpenTelemetry (`easycat._observability`)** — for histograms and traces
  beyond the `/metrics` snapshot, install an OTel SDK and exporter
  (`opentelemetry-sdk`, `opentelemetry-exporter-otlp`; EasyCat treats OTel as
  fully optional and never pulls it in as a hard dependency — see
  [observability.md](../observability.md#d-opentelemetry-facade)) and
  initialize the SDK's `MeterProvider`/`TracerProvider` in your own `config()`
  factory before creating sessions. Point the standard OTel SDK environment
  variables at your collector — nothing container-specific:

  ```bash
  OTEL_SERVICE_NAME=easycat-ws
  OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
  OTEL_EXPORTER_OTLP_PROTOCOL=grpc
  ```

  A collector receiving OTLP push from the container is the usual pattern
  for a pull-based Prometheus scrape target — EasyCat itself emits
  `easycat.server.*` metrics (`requests.total`, `request.duration`,
  `sessions.rejected.total`, `connections.active`, `draining`) plus the
  pipeline-stage/turn-latency metrics documented in
  [observability.md](../observability.md); it does not serve Prometheus text
  exposition natively, so run the collector's `prometheus` exporter (or
  `prometheusremotewrite`) alongside your `otlp` receiver to bridge the two.

## Swapping STT / TTS providers

Rebuild with a different set of extras:

```bash
# Deepgram STT + ElevenLabs TTS
docker build \
  --build-arg EXTRAS="--extra openai-agents --extra silero-vad --extra rnnoise --extra deepgram --extra elevenlabs" \
  -f docker/Dockerfile -t easycat:dg-el .
```

Then edit `examples/ws_server.py` (or mount your own server script) to
wire the providers into `EasyConfig`, and pass the relevant API keys
as environment variables. Keep the WebSocket token gate, session cap, and
loopback bind (or equivalent ingress controls) when deploying modified
server scripts.

The entrypoint fails fast when `EASYCAT_WS_HOST` is non-loopback and
`EASYCAT_WS_TOKEN` is unset. If your mounted script deliberately serves
without a token because authentication terminates at an ingress proxy
(the `unsafe_allow_no_auth=True` pattern), set
`EASYCAT_UNSAFE_ALLOW_NO_AUTH=1` so the entrypoint check passes, **and** update
the mounted script to pass `unsafe_allow_no_auth=True` to its server helper.
Both are required: the env var bypasses only the container preflight, while
Python's bind guard rejects an unauthenticated non-loopback listener unless the
server call itself enables that explicit escape hatch.

## Latency notes

Bridge networking is fine for this example: one TCP connection per
session, PCM16 over WebSocket, no UDP media.

If you extend the example to WebRTC or SIP telephony, switch to
`network_mode: host`.  aiortc's ICE gathering and RTP media ports
(49152-65535/udp) do not play well with Docker's default NAT.

## Resource sizing

- Idle session: ~150 MB RAM
- Active session: ~250 MB RAM + short CPU bursts at each turn boundary
  (VAD / Smart-Turn inference on CPU)
- The example defaults to `EASYCAT_WS_MAX_SESSIONS=10`. One vCPU comfortably
  handles ~10 concurrent WebSocket sessions as a starting point; measure with
  `SessionManager` metrics before scaling up.

## Not covered

- **Multi-arch builds** — the Dockerfile runs on amd64 without
  modification; arm64 requires verifying the onnxruntime wheel.
- **Kubernetes manifests** — the compose file maps directly to a
  Deployment + Service; see upstream `k8s` recipes rather than
  reinventing them here.
- **TLS termination and public ingress** — put nginx / Caddy / an ALB in front
  for `wss://` in production. Require authentication / authorization at the
  edge, preserve rate and session limits, and do not publish this example
  directly on all host interfaces.
