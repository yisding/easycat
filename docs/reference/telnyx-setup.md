# Telnyx Call Control setup

This guide covers the portal configuration, environment variables, and
security model for running EasyCat against Telnyx Call Control v2.

## Prerequisites

1. A [Telnyx](https://telnyx.com/) account with a programmable voice
   connection (Call Control).
2. An EasyCat installation with the `telnyx` extra:

   ```bash
   uv sync --extra telnyx --group dev
   ```

3. A public `wss://` endpoint that Telnyx can reach for media streams.

## Portal setup

### Create a Call Control connection

1. Navigate to **Voice → Programmable Voice** in the Telnyx Mission Control
   portal and create a new **Call Control** application.
2. Set the webhook URL to `https://your-host:8000/telnyx` — this is where
   EasyCat receives `call.initiated` and other lifecycle deliveries.
3. Note the **Connection ID** for outbound calls (`TELNYX_CONNECTION_ID`).

### Copy credentials

- **API Key**: generate under **Auth → API Keys**. This is your
  `TELNYX_API_KEY`.
- **Webhook Public Key**: on the Call Control app's settings page, copy the
  Ed25519 public key. This is your `TELNYX_PUBLIC_KEY`. EasyCat uses it to
  verify that every inbound webhook was signed by Telnyx.

## Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `TELNYX_STREAM_URL` | Yes | Public `wss://` URL Telnyx dials back into. |
| `TELNYX_API_KEY` | Yes | Call Control Bearer token for answering/dialing/hanging up. |
| `TELNYX_PUBLIC_KEY` | Yes* | Ed25519 public key for webhook signature verification. |
| `TELNYX_STREAM_TOKEN_SECRET` | No | Pins the stream-token signing key across restarts. |
| `TELNYX_CONNECTION_ID` | Outbound only | Connection ID for outbound Dial commands. |
| `TELNYX_WS_PORT` | No | Media WebSocket port (default: `8766`). |
| `TELNYX_MAX_SESSIONS` | No | Max concurrent sessions (default: `64`). |
| `TELNYX_START_TIMEOUT_S` | No | Seconds to wait for a valid `start` frame (default: `10`). |

*\* Required unless you explicitly pass `unsafe_allow_unsigned_webhooks=True`,
which accepts unauthenticated webhooks and is intended only for local testing.*

## Security model

Telnyx signs webhooks with Ed25519 over `{timestamp}|{raw_body}` using headers
`telnyx-signature-ed25519` / `telnyx-timestamp`. EasyCat verifies each delivery
and rejects anything outside a five-minute replay window.

The media WebSocket handshake carries no signature. Authentication rests on a
one-time stream token embedded in the answer command's `stream_url`, validated
at `start` preflight and bound to the call control ID. A replayed or stolen
token cannot attach to a different call, and each token is consumed exactly
once.

## Run it

Using the example app:

```bash
export OPENAI_API_KEY="..."
export TELNYX_STREAM_URL="wss://your-public-host:8766"
export TELNYX_API_KEY="..."
export TELNYX_PUBLIC_KEY="..."
uv run python examples/telnyx_voice.py
```

Or programmatically:

```python
from easycat.voice_app import VoiceApp

app = VoiceApp(stt="openai", tts="openai")
app.run("telnyx")
```
