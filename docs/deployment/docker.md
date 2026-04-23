# Running EasyCat in Docker

This guide packages the WebSocket example (`examples/ws_server.py`) as a
container.  Silero VAD and Smart-Turn ONNX models ship inside
`src/easycat/models/`, so they are embedded in the image — nothing is
fetched at first-request time besides the calls to OpenAI.

## Quickstart

```bash
export OPENAI_API_KEY=sk-...
docker compose -f docker/compose.yaml up --build
```

Open `examples/ws_browser_client.html` in a browser and point it at
`ws://localhost:8765`.

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
- Exposes TCP 8765 (WebSocket PCM16 audio)

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
as environment variables.

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
- One vCPU comfortably handles ~10 concurrent WebSocket sessions as a
  starting point; measure with `SessionManager` metrics before scaling up

## Not covered

- **Multi-arch builds** — the Dockerfile runs on amd64 without
  modification; arm64 requires verifying the onnxruntime wheel.
- **Kubernetes manifests** — the compose file maps directly to a
  Deployment + Service; see upstream `k8s` recipes rather than
  reinventing them here.
- **TLS termination** — put nginx / Caddy / an ALB in front for
  `wss://` in production.
