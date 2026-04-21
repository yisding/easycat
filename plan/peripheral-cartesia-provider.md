# Cartesia STT + TTS Provider — Peripheral

> **This is a peripheral initiative.** It is not essential to the
> debug-first thesis in `essential-debug-first-runtime.md`. It adds
> Cartesia Ink-Whisper (streaming STT) and Sonic (streaming TTS) to the
> chained-pipeline provider registry alongside OpenAI, Deepgram, and
> ElevenLabs.
>
> **Related peripheral docs:**
> - `peripheral-provider-ecosystem.md` — notes Cartesia Sonic 3 / Sonic
>   Turbo as the 2026 TTS latency bar and, incorrectly, says an "existing
>   Cartesia adapter covers it." No adapter exists today — this plan adds
>   it. Update that doc's Competitive Context once this work lands.
>
> **In scope (this file):** `CartesiaSTT` (WebSocket, ink-whisper),
> `CartesiaTTS` (WebSocket, sonic-3 / sonic-2 / sonic-turbo), factory
> registration, env-var wiring, unit tests, optional-extra dependency.
>
> **Out of scope:** Cartesia realtime Agents API (voice-to-voice), SSE
> TTS endpoint, bytes/batch TTS endpoint, the Cartesia Python SDK (we
> speak the WebSocket protocol directly via `ReconnectingWebSocket`, the
> same pattern Deepgram and ElevenLabs use).

## Why

Cartesia is now the latency leader on both sides of the pipeline:

- **Sonic 3 ~90ms TTFA**, **Sonic Turbo ~40ms TTFA** — the 2026 TTS
  latency bar (see `peripheral-provider-ecosystem.md`).
- **Ink-Whisper** — Cartesia's streaming-optimised Whisper variant;
  comparable accuracy to `deepgram/nova-2` at lower per-minute cost
  and with native word-timestamps.

Without a first-party Cartesia adapter, EasyCat users who want best-in-
class latency have to drop out of the factory / `easycat doctor` flow
and hand-roll a `TTSProvider`. Adding Cartesia keeps the
chained-pipeline provider story competitive and unlocks the default
latency-optimised template.

## Protocol Summary (cached for implementation)

Authoritative references (re-verify before coding — Cartesia versions
the API by date header):

- https://docs.cartesia.ai/api-reference/stt/stt (STT WebSocket)
- https://docs.cartesia.ai/api-reference/tts/tts (TTS WebSocket)
- https://docs.cartesia.ai/api-reference/api-conventions (auth, version)

### STT — `wss://api.cartesia.ai/stt/websocket`

- **Auth:** `X-API-Key: <key>` header (preferred). Browser clients can
  fall back to `?access_token=<short-lived>` query param — not relevant
  here since we run server-side.
- **Version:** `Cartesia-Version: 2026-03-01` header (or
  `cartesia_version=` query).
- **Query params (all required):** `model` (`ink-whisper`), `language`
  (ISO-639-1, default `en`), `encoding` (`pcm_s16le`), `sample_rate`
  (e.g. `16000`), `min_volume` (VAD threshold 0.0–1.0),
  `max_silence_duration_secs` (endpointing).
- **Input:** binary WS frames — raw PCM matching `encoding`/`sample_rate`.
  Cartesia recommends ~100ms chunks.
- **Control text messages (JSON):**
  - `finalize` — flush buffered audio, get `flush_done` ack. Maps to
    `STTBase._on_commit_segment` (same role as ElevenLabs' `commit`).
  - `done` — flush + close session, get `done` ack. Maps to
    `STTBase._on_end`.
- **Server JSON messages:**
  - `{type: "transcript", is_final, request_id, text, duration,
    language, words: [{word, start, end}]}` → `STTEvent(PARTIAL|FINAL)`
    with `WordTimestamp` list when `words` is present.
  - `{type: "flush_done" | "done" | "error", ...}` → log / route to
    journal Error event via `_emit_provider_error` pattern.
- **Idle timeout:** 3 min (reset on any message). Matches our existing
  `ReconnectingWebSocket` reconnect loop semantics.

### TTS — `wss://api.cartesia.ai/tts/websocket`

- **Auth + Version:** same headers as STT.
- **No query params** on the URL — everything flows through JSON
  request frames.
- **Request JSON** (per synthesis):
  ```json
  {
    "model_id": "sonic-3",
    "transcript": "<text>",
    "context_id": "<uuid>",
    "voice": {"mode": "id", "id": "<voice-uuid>"},
    "language": "en",
    "output_format": {
      "container": "raw",
      "encoding": "pcm_s16le",
      "sample_rate": 24000
    },
    "continue": false,
    "add_timestamps": true
  }
  ```
  Use `pcm_s16le` at 24000 Hz to match `TTSBase` default
  `PCM16_MONO_24K` — avoids resample cost (same trick ElevenLabs and
  Deepgram use).
- **Cancel:** send `{"context_id": "...", "cancel": true}`.
- **Flush (multi-chunk context):** send `{..., "flush": true}`; server
  returns `{type: "flush_done", flush_id}`. Not required for v1 — we
  synthesize one utterance per `context_id`.
- **Server messages:**
  - `{type: "chunk", data: <base64 pcm>, done}` → `_make_audio_event`
  - `{type: "timestamps", word_timestamps: {words, start, end}}` →
    `_make_markers_event` (reuse the ElevenLabs markers path).
  - `{type: "done"}` → break loop.
  - `{type: "error", code, message, status_code}` → emit journal
    `Error` event via `_emit_provider_error`.

**Models to support in v1:** `sonic-3` (default — best quality/latency
balance), `sonic-2`, `sonic-turbo`. All accept the same request shape.

## File-by-file Plan

### New files

| File | Purpose |
|---|---|
| `src/easycat/stt/cartesia_provider.py` | `CartesiaSTT` + `CartesiaSTTConfig`. Shape matches `DeepgramSTT` closely: `ReconnectingWebSocket`, background `_receive_loop`, `_emit_provider_error` on `error` messages, `_on_commit_segment` → `finalize`, `_on_end` → `done` + await ack. |
| `src/easycat/tts/cartesia_tts.py` | `CartesiaTTS` + `CartesiaTTSConfig`. Shape matches `DeepgramTTS`: per-synthesis WS connect, JSON request, base64-decode chunks, handle `timestamps` markers, handle `error`. `cancel()` sends `{cancel: true}` then closes. |
| `tests/stt/test_stt_cartesia.py` | Mirror `test_stt_deepgram.py`: fake `ws_connect`, feed partial/final transcript frames, assert `STTEvent` stream, verify `finalize`/`done` messages sent. |
| `tests/tts/test_tts_cartesia.py` | Mirror `test_tts_deepgram.py`: fake WS, feed base64 chunk frames, assert audio events + markers, test `cancel()` sends the cancel JSON. |

### Edits

| File | Edit |
|---|---|
| `src/easycat/stt/factory.py` | Import `CartesiaSTT`/`CartesiaSTTConfig`. Add to `_PROVIDER_TO_CONFIG` under `"cartesia"`. Add `STTConfig` union member. Add `"cartesia": "CARTESIA_API_KEY"` to `_PROVIDER_ENV_VAR`. Include `CartesiaSTTConfig` in the `needs_event_bus` isinstance check in `create_stt_provider_from_config` (it emits provider errors on the bus). |
| `src/easycat/tts/factory.py` | Symmetric edits in TTS factory: register `"cartesia"`, add to `TTSConfig` union, `_PROVIDER_ENV_VAR`, and the `event_bus` replace branch in `create_tts_provider_from_config`. |
| `src/easycat/config.py` | Import `CartesiaSTTConfig`/`CartesiaTTSConfig` for type-union propagation (mirror existing `DeepgramSTTConfig` import; nothing else to change — string-keyed `stt="cartesia"` / `tts="cartesia/sonic-turbo"` falls out of the factory work above). |
| `pyproject.toml` | Add optional extra `cartesia = []` (no SDK — we use raw `websockets`). Include Cartesia under the `all` and `quickstart` extras at the reviewer's discretion. Also update the `CARTESIA_API_KEY` mention in `easycat doctor` (see below). |
| `src/easycat/cli.py` (doctor) | Add `CARTESIA_API_KEY` to the env-var probe list and a "Cartesia STT/TTS reachable" WebSocket handshake check (same pattern as Deepgram Flux is slated to get in `peripheral-provider-ecosystem.md`). |
| `plan/peripheral-provider-ecosystem.md` | Correct the "existing Cartesia adapter covers it" line once this work lands — cite this plan and the new provider files. |

## Design Decisions

- **No `cartesia` SDK dependency.** EasyCat's existing Deepgram and
  ElevenLabs adapters speak the WS protocol directly through
  `ReconnectingWebSocket`. Going through the SDK would bypass our
  reconnect/journal plumbing, double the dependency surface, and lock
  us to whatever Python version matrix the SDK supports.
- **Request `pcm_s16le` @ 24000 Hz from Cartesia.** Matches the
  `TTSBase` default output format. No resample on the hot path.
- **Default TTS model = `sonic-3`.** Best quality/latency balance.
  Templates that explicitly want minimum TTFA select
  `cartesia/sonic-turbo` via the string-keyed provider spec.
- **`add_timestamps: true` by default in TTS.** Cheap, and word
  alignment feeds the `InterruptionController` word-level estimation
  once that lands (see `essential-debug-first-runtime.md`). Gate it
  behind a config flag only if we observe bandwidth cost in practice.
- **STT `min_volume` / `max_silence_duration_secs` exposed on config
  but not in the session-level `stt=` shortcut.** They're Cartesia-
  specific VAD knobs; the common default is fine for the string-keyed
  path.
- **Error path reuses `_emit_provider_error`.** Cartesia's `error`
  frames carry `code` / `status_code` / `message` — attach all three as
  notes so recorded bundles explain *why* Cartesia refused a request
  (quota, bad voice id, deprecated model). Pattern is already proven
  in `ElevenLabsTTS._emit_provider_error`.
- **Sample-rate alignment with telephony.** 8kHz μ-law /  `pcm_mulaw`
  is supported by Cartesia but left out of v1 — telephony paths in
  EasyCat already resample. Revisit only if we see the resample cost
  on the telephony profile.

## Tests

- **Unit — STT (`test_stt_cartesia.py`):**
  - Fake WS returns a partial then a final transcript → assert two
    `STTEvent`s with correct type, text, confidence(=None), language,
    and word timestamps.
  - `commit_segment()` sends `finalize`; `end_stream()` sends `done`
    and returns after `done` ack.
  - Server `error` frame → `Error` event posted to the bus (assert via
    a stub bus like the Deepgram test does).
  - Unknown frame types are ignored, don't break the receive loop.
- **Unit — TTS (`test_tts_cartesia.py`):**
  - Fake WS returns two base64 `chunk` frames + a `timestamps` frame
    + a `done` frame → assert two AUDIO events + one MARKERS event,
    then loop exits.
  - `cancel()` mid-stream sends `{cancel: true}` with the right
    `context_id` and closes the WS.
  - `error` frame on the bus.
- **Factory (`test_stt_factory.py` / `test_tts_factory.py`):**
  - `parse_stt_string("cartesia/ink-whisper")` with `CARTESIA_API_KEY`
    set returns a populated `CartesiaSTTConfig`. Unset env var raises
    `EASYCAT_E203`.
  - `parse_tts_string("cartesia/sonic-turbo")` likewise. Bad model
    string still parses (model is opaque) — factory validation is
    structural, not semantic.
  - `create_*_provider_from_config` injects the event bus when the
    config's `event_bus` is `None`.
- **Integration (optional, `@pytest.mark.integration`):** live-API
  round-trip for both STT and TTS, gated on `CARTESIA_API_KEY`.
  Follows the same pattern as the live Deepgram / ElevenLabs tests.

## Sequencing

1. Land `CartesiaTTS` + unit tests + factory registration. TTS is the
   higher-value half (latency leadership) and simpler (one-shot
   synthesis, no endpointing concerns).
2. Land `CartesiaSTT` + unit tests + factory registration. Reuses the
   journal-error pattern established in step 1.
3. Update `easycat doctor` with the Cartesia reachability probe.
4. Correct `peripheral-provider-ecosystem.md`'s stale "existing
   adapter" claim.
5. (Optional) Add a `examples/cartesia_quickstart.py` showing
   `stt="cartesia/ink-whisper", tts="cartesia/sonic-turbo"` for the
   latency-optimised template.

Steps 1 and 2 are independent and can be done in parallel. Both
exercise the factory pattern — landing them in one PR each keeps the
review surface small and matches the established commit cadence
(`stt: ...` / `tts: ...`).

## Open Questions

- **Cartesia version pinning.** Pick one `Cartesia-Version` date to
  ship with (e.g. `2026-03-01`) and document the policy for rolling
  it forward. Putting the version on `CartesiaSTTConfig`/
  `CartesiaTTSConfig` as a field with a sensible default is cheap and
  matches how Deepgram's `base_url` is overridable.
- **Default voice id.** Cartesia ships a library of voices keyed by
  UUID (no stable symbolic names like ElevenLabs "Sarah"). Pick one
  well-known public voice id as the default (e.g. the one in their
  own docs, `6ccbfb76-1fc6-48f7-b71d-91ac6298247b`) and document
  that users are expected to override for production.
- **Realtime Agents API.** Cartesia also exposes a voice-to-voice
  Agents API. It is out of EasyCat's chained-pipeline scope per the
  essential-plan guardrail and stays out of this work.
