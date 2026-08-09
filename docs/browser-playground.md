# Browser Playground

Talk to EasyCat's bundled playground agent in the browser. Without a manifest,
`easycat serve` wires the playground to the bundled WebRTC client and prints the
URL to open; it does not import a `VoiceApp` from the current directory. The page
shows a live transcript (user and bot), an interruption indicator that lights up
when you barge in, and a per-turn latency readout (final user transcript → first
bot audio).

## Quickstart

```bash
uv sync --extra quickstart --extra webrtc --group dev
test -e .env || cp .env.example .env
uv run easycat doctor --env-file .env
uv run easycat doctor --env-file .env --json
uv run --env-file .env easycat serve
```

If the key is exported instead, use `uv run easycat doctor`; add `--json`
(`uv run easycat doctor --json`) for parseable checks.

Then open the printed URL (`Open http://localhost:8080`) and click **Start**.

Useful options:

- `--mode` — playground mode to serve. Defaults to `browser` (WebRTC + bundled
  client); pass `--mode websocket` for per-client WebSocket sessions or
  `--mode local` for a local mic/speaker run. The same `VoiceApp` backs every
  mode.
- `--port` / `--host` — where the playground server listens. The default
  bind is loopback (`127.0.0.1`), matching the WebSocket/docker security
  defaults.
- `--token` (or `EASYCAT_SERVE_TOKEN`) — shared secret required by the
  signaling endpoints. `easycat serve` refuses a non-loopback `--host`
  without a token. The printed Open URL embeds it in a `#token=...` fragment,
  which is not sent in the HTTP request; the bundled client removes it from the
  visible URL and forwards it as an `Authorization: Bearer` header.
- `--agent-model` / `--instructions` — swap the playground agent's OpenAI
  Responses API model or its guidance.
- `--manifest` / `--profile` — build a manifest-backed `VoiceServer` instead
  of the bundled playground.

For a script-shaped equivalent (and EC2/TURN deployment notes), see
`uv run python examples/webrtc_server.py`. To inspect the session afterwards,
open the journal in the debugger UI (`uv sync --extra debugger --group dev`,
then `serve_session(session)` — see [observability](observability.md)); the
playground page links there directly.

## Wire protocol

Both browser transports speak the same JSON event vocabulary; the
implementation lives in `src/easycat/transports/_browser_events.py` and is
guarded by `uv run pytest tests/transports/test_webrtc_auth_browser_playground.py`.

The maintained WebSocket and WebTransport clients request browser-native echo
cancellation through `getUserMedia`. Server-side AEC is off by default for
those transports because socket/datagram write time is not the browser's
playout clock and cannot provide a continuous, silence-filled far-end
reference. You can still opt in with `enable_echo_cancellation=True` for a
custom endpoint, but that best-effort path records
`aec_reference_degraded` in the session journal. WebRTC can pace its outbound
reference and retains its transport-aware AEC behavior.

### WebSocket transport

Inbound (client → server):

- **Binary frames** — raw PCM16 audio chunks.
- **Text frames** — JSON control messages:
  - `{"type": "start"}` — client signals session start.
  - `{"type": "stop"}` — client signals session end.
  - `{"type": "config", "sample_rate": 16000}` — negotiate the inbound
    audio format.

Each inbound WebSocket message is capped at 64 KiB. Decoded audio is then
buffered under independent frame-count and byte-count ceilings (200 chunks and
4 MiB by default); a frame that would exceed either queue limit is dropped and
reported as `TransportDegraded("inbound_queue_full")`.

Outbound (server → client):

- **Binary frames** — raw PCM16 audio chunks (bot speech).
- **Text frames** — JSON control messages:
  - `{"type": "ready"}` — sent once when the connection is accepted.
  - `{"type": "audio_format", "sample_rate": 24000}` — sent before audio
    whenever the outbound sample rate changes.
  - `{"type": "clear"}` — stop and discard bot audio already scheduled by
    the client (sent on barge-in and explicit playback cancellation).
  - Session event messages (below).

### WebRTC transport

Audio flows over the Opus peer connection. Signaling is HTTP
(`POST /offer`, `GET /config`, `POST /stats`, `GET /health`); when
`WebRTCTransportConfig.auth_token` is set, `/config`, `/offer`, and `/stats`
require the token as `Authorization: Bearer <token>`. A `?token=` query
parameter is accepted only when `allow_query_token=True`; it is disabled by
default because URLs leak more readily through logs and browser history.
Session event messages arrive on a client-created data channel named
`events`.

### Session event messages

One JSON object per message (`schema_version` 1):

| Message | Fields | Meaning |
| --- | --- | --- |
| `stt_partial` | `text`, `turn_id` | Partial user transcript (in-progress). |
| `stt_final` | `text`, `turn_id` | Final user transcript for the turn. |
| `agent_delta` | `text`, `turn_id` | Streaming bot reply text. |
| `agent_final` | `text`, `turn_id` | Complete bot reply text. |
| `turn_started` | `turn_id` | A new user turn began (VAD triggered). |
| `interruption` | `turn_id` | User barged in while the bot was speaking. |
| `turn_latency` | `turn_id`, `ms` | Final user transcript → first bot audio. |

`turn_id` may be `null` for events outside a tracked turn (for example a
greeting). Event delivery is best-effort observability through a bounded
32-message writer queue: the audio/event pipeline never awaits transport I/O,
and a full queue, failed send, or send taking longer than 250 ms drops that
browser event.
