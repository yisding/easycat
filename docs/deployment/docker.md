# Running EasyCat in Docker

This guide packages the WebSocket example (`examples/ws_server.py`) as a
container.  Silero VAD and Smart-Turn ONNX models ship inside
`src/easycat/models/`, so they are embedded in the image — nothing is
fetched at first-request time besides the calls to OpenAI.

## Quickstart

```bash
export OPENAI_API_KEY=sk-...
export EASYCAT_WS_TOKEN=$(python - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)
docker compose -f docker/compose.yaml up --build
```

The compose service publishes the container only on host loopback
(`127.0.0.1:8765`) and requires the token above before it creates a
provider-backed EasyCat session.  Non-browser clients should send
`Authorization: Bearer $EASYCAT_WS_TOKEN`.  For the browser example,
open `examples/ws_browser_client.html?token=<EASYCAT_WS_TOKEN>` and point
it at `ws://localhost:8765`.

To stop:

```bash
docker compose -f docker/compose.yaml down
```

## Secrets: use a `.env` file, don't bake them in

Prefer compose's `.env` file (read by the client, expanded into the
container's environment) over passing secrets as build args:

```bash
# docker/.env  — git-ignored, never copied into the image
OPENAI_API_KEY=sk-...
EASYCAT_WS_TOKEN=<random-long-token>
# Optional: EASYCAT_WS_MAX_SESSIONS=10
```

Then `docker compose -f docker/compose.yaml up` picks it up
automatically.  The repo's `.dockerignore` excludes `.env` and `.env.*`
from the build context as a second line of defence — if you fork the
Dockerfile to use a wildcard `COPY . /app`, secrets still won't ship.

Never use `ARG OPENAI_API_KEY=...` in the Dockerfile: build args are
recoverable from image history.

## What the image contains

- `python:3.11-slim-bookworm` runtime
- EasyCat with extras: `openai-agents`, `silero-vad`, `rnnoise`
- Bundled Silero VAD and Smart-Turn v3.2 ONNX models
- Runs as a non-root `easycat` user (uid 1000)
- Exposes TCP 8765 (WebSocket PCM16 audio); compose binds it to host loopback by default

Final image size is roughly 450 MB on amd64.

## Swapping STT / TTS providers

Rebuild with a different set of extras:

```bash
# Deepgram STT + ElevenLabs TTS
docker build \
  --build-arg EXTRAS="--extra openai-agents --extra silero-vad --extra rnnoise --extra deepgram --extra elevenlabs" \
  -f docker/Dockerfile -t easycat:dg-el .
```

Then edit `examples/ws_server.py` (or mount your own server script) to
wire the providers into `SessionConfig`, and pass the relevant API keys
as environment variables. Keep the WebSocket token gate, session cap, and
loopback bind (or equivalent ingress controls) when deploying modified
server scripts.

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
