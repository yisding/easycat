# Telephony-Native TTS Output (8 kHz / μ-law) — Peripheral

> **This is a peripheral initiative.** It is not essential to the
> debug-first thesis in `essential-debug-first-runtime.md`. It removes
> a pre-existing inefficiency: every Twilio-terminated call today
> resamples TTS audio 24 kHz → 8 kHz inside `TwilioTransport`, for every
> provider (OpenAI, Deepgram, ElevenLabs, and the planned Cartesia).
>
> **Related peripheral docs:**
> - `peripheral-cartesia-provider.md` — adds Cartesia TTS; calls out 8 kHz
>   / μ-law as a v2 follow-up. That follow-up is *this* plan, generalised
>   to every provider.
> - `peripheral-provider-ecosystem.md` — the broader provider-quality
>   backlog.
>
> **In scope (this file):** transport-aware TTS `output_format`
> negotiation, per-provider "best native match" logic, optional PCM16 ↔
> μ-law conversion in `TTSBase`, `TwilioTransport` μ-law pass-through
> branch, tests, rollout.
>
> **Out of scope:** STT input-format negotiation for telephony (a
> symmetric but separate win — noted as "see also" at the end), any new
> transport, any non-Twilio telephony backend.

## Why

Every phone call today pays work no phone call can use:

1. TTS generates PCM16 at 24 kHz.
2. `TwilioTransport.send_audio` calls `pcm16_to_mulaw(chunk.data,
   chunk.format.sample_rate)`
   (`src/easycat/transports/twilio_media.py:285-289`). That function
   resamples 24 → 8 kHz, *then* μ-law encodes.
3. The 12 kHz of spectrum above the phone-line ceiling is thrown away.

Costs:

- **CPU.** Resample is linear-ish in sample count. Per-chunk on a live
  call, at provider-native TTS chunk sizes. Not huge, but not free, and
  it happens on the synchronous send path.
- **Bandwidth between provider and EasyCat.** Over-the-wire, PCM16 @ 24
  kHz is **6× the bytes** of μ-law @ 8 kHz (2 bytes × 24 kHz vs 1 byte ×
  8 kHz). On a 5-minute call that's ~1.7 MB vs ~280 kB per leg.
- **Journal / bundle storage.** Every debug bundle stores TTS audio.
  Three of our four providers (Deepgram, ElevenLabs, Cartesia) can emit
  μ-law @ 8 kHz natively; recording that instead of 24 kHz PCM16 shrinks
  bundles 6× for the TTS track.
- **Quality artifacts.** Resample → μ-law introduces two sources of
  numerical error. Asking the provider for 8 kHz (or μ-law) directly
  lets its own higher-quality model-side resampler do the work.

The win isn't dramatic on any one axis, but it's free money — none of
the existing logic depends on 24 kHz, and the transport already knows
its own preferred rate.

## Goals & Non-goals

**Goals**

- Twilio calls should emit at most one conversion (μ-law encode) and
  zero resamples, for every TTS provider that can produce ≤ 8 kHz PCM
  or μ-law natively.
- The mechanism generalises: a WebRTC transport that prefers 48 kHz, or
  a future narrowband transport, uses the same pathway.
- No change in behaviour for local / WebRTC / WebSocket transports.
- User override always wins — an explicit `output_format` in a
  `TTSConfig` is respected.

**Non-goals**

- Making OpenAI TTS emit 8 kHz. OpenAI's `/audio/speech` returns PCM
  at a fixed rate (currently 24 kHz). We resample locally for OpenAI,
  same as today, and accept the cost.
- Changing the canonical internal audio unit. `TTSEvent.audio` remains
  an `AudioChunk` with `AudioFormat`; downstream consumers already
  read `sample_rate` / `sample_width` / `encoding` off the chunk.
- Switching telephony to a different codec (G.722, Opus). Twilio
  Media Streams is μ-law 8 kHz today and for the foreseeable future
  — see this session's conversation notes.

## Provider capability matrix

Verified against each provider's current API docs. Re-check before
coding — providers version output formats silently.

| Provider | Native 8 kHz PCM16 | Native μ-law 8 kHz | Notes |
|---|---|---|---|
| Deepgram Aura | ✅ `encoding=linear16&sample_rate=8000` | ✅ `encoding=mulaw&sample_rate=8000` | sample_rate already a config field (`deepgram_tts.py:35`) |
| ElevenLabs | ✅ `output_format=pcm_8000` (already in `_ELEVENLABS_FORMAT_MAP`) | ✅ `output_format=ulaw_8000` (not in map today) | current code rejects non-PCM in `__post_init__` — needs to accept μ-law |
| Cartesia | ✅ `{encoding: "pcm_s16le", sample_rate: 8000}` | ✅ `{encoding: "pcm_mulaw", sample_rate: 8000}` | greenfield — land this plan's support directly in the new provider |
| OpenAI | ❌ (fixed 24 kHz per `openai_tts.py:29`) | ❌ | local resample unavoidable; already happens today |

## Architecture

### Core idea

`TTSBase` already has `output_format: AudioFormat` as a constructor
argument and stores it on `self._output_format`. Today it is *only*
used as the post-normalization target. This plan promotes it to the
**declarative request target**: each provider's synthesis path reads
`self._output_format` and picks the closest native provider-side
format to request.

The normalization step (`_normalize_audio`) stays as a safety net for
the gap — when the provider can't match the target exactly.

### Pipeline, before vs after

**Before (Twilio + Deepgram TTS):**

```
Deepgram API  →  PCM16 24k  →  TTSBase (no-op)  →  TTSAudio  →  TwilioTransport
                                                                   ↓ resample 24→8
                                                                   ↓ μ-law encode
                                                                 wire
```

**After (Twilio + Deepgram TTS, output_format=PCM16_MONO_8K):**

```
Deepgram API  →  PCM16 8k   →  TTSBase (no-op)  →  TTSAudio  →  TwilioTransport
                                                                   ↓ μ-law encode
                                                                 wire
```

**After (Twilio + Cartesia TTS, output_format=MULAW_8K):** (optional
Stage 2, see sequencing)

```
Cartesia API  →  μ-law 8k   →  TTSBase (no-op)  →  TTSAudio  →  TwilioTransport
                                                                   ↓ base64
                                                                 wire
```

### Transport declares its preferred output

A new read-only field on the `Transport` protocol:

```python
# in easycat/providers.py
class Transport(Protocol):
    ...
    preferred_tts_format: AudioFormat  # default implementation returns PCM16_MONO_24K
```

Per-transport defaults:

| Transport | `preferred_tts_format` |
|---|---|
| `LocalTransport` | `PCM16_MONO_24K` |
| `WebSocketTransport` | `PCM16_MONO_24K` |
| `WebRTCTransport` | `PCM16_MONO_48K` (matches Opus @ 48 kHz native) |
| `TwilioTransport` | `PCM16_MONO_8K` (Stage 1) → `MULAW_8K` (Stage 2) |

The attribute lives on the transport (not on Session) because the
transport is the one authoritative source of "what does the terminal
wire accept." Any new transport gets one knob to configure.

Pattern matches how `Transport` already declares `expected_audio_format`
for the incoming direction. This is the symmetric outgoing side.

### `create_session()` plumbs it through

When building a TTS provider, `config.create_session()`:

1. Looks at the user's `TTSConfig`. If `output_format` is set
   explicitly (non-default), respect it — the user has opinions.
2. Otherwise, read `transport.preferred_tts_format` and set
   `output_format` on the TTS config via `dataclasses.replace()`
   before instantiation.
3. Log a single `TransportFormatApplied` event on the bus with
   `{transport=..., tts_format=...}` so journal consumers can see
   what happened. No user-facing print.

"Set explicitly" is detected by comparing to the dataclass default. If
we want to be safer about this, make `output_format` default to `None`
in every TTSConfig and normalise to the transport preference (or
`PCM16_MONO_24K` if no transport) during session construction. Pick
whichever is less noisy across the diff; my read is the `None` sentinel
is cleaner.

### Per-provider "best native match" logic

Each provider gains a small private function:

```python
# in each provider file (deepgram_tts.py, elevenlabs_tts.py, cartesia_tts.py, openai_tts.py)
def _request_params_for(target: AudioFormat) -> tuple[ProviderRequest, AudioFormat]:
    """Given a target output format, pick provider API params that come
    closest, and return the (request, actual-source-format) pair."""
```

Behaviour per provider:

- **Deepgram**: if target is μ-law 8 kHz → `encoding=mulaw&sample_rate=8000`.
  If target sample_rate ∈ {8000, 16000, 24000, 48000} → linear16 at that
  rate. Else → closest supported, let `_normalize_audio` bridge the gap.
- **ElevenLabs**: map target to nearest `pcm_*` entry in
  `_ELEVENLABS_FORMAT_MAP`. Extend the map with `ulaw_8000` → `MULAW_8K`.
  Lift the `__post_init__` rejection of non-PCM formats (the guard made
  sense when the code assumed PCM16; the new path explicitly supports
  μ-law).
- **Cartesia**: target encoding + sample_rate pass through as the
  `output_format` JSON field directly. `pcm_s16le`, `pcm_f32le`,
  `pcm_mulaw`, `pcm_alaw` all supported on the wire.
- **OpenAI**: fixed at 24 kHz PCM16. No provider-side selection;
  `_normalize_audio` picks up the slack.

The function returns both the provider request *and* the actual source
format it will produce. That second value feeds `_normalize_audio` so
the resample path still works when target ≠ native.

### `TTSBase` gains encoding-aware normalization

`_normalize_audio` today handles:
- mono downmix (`to_mono`)
- sample-rate resample (`resample`)

It does **not** handle PCM16 ↔ μ-law. Add that:

```python
def _normalize_audio(self, data: bytes, source_format: AudioFormat) -> bytes:
    src, dst = source_format, self._output_format

    # decode μ-law → PCM16 first so resample/downmix operate on PCM16
    if src.encoding == "mulaw" and dst.encoding != "mulaw":
        data = mulaw_to_pcm16_raw(data)  # no rate change
        src = AudioFormat(src.sample_rate, src.channels, 2, encoding="pcm")

    if src.channels > 1 and dst.channels == 1:
        data = to_mono(data, src.channels)
        src = replace(src, channels=1)

    if src.sample_rate != dst.sample_rate:
        data = resample(data, src.sample_rate, dst.sample_rate)
        src = replace(src, sample_rate=dst.sample_rate)

    if src.encoding == "pcm" and dst.encoding == "mulaw":
        data = _mulaw_encode(data)

    return data
```

All three helpers already exist in `transports/twilio_media.py`
(`_mulaw_decode`, `_mulaw_encode`, `mulaw_to_pcm16`, `pcm16_to_mulaw`).
Move them to `audio_utils.py` so TTS and telephony both import from one
place. No behaviour change for the transport.

### `TwilioTransport` μ-law pass-through

In `twilio_media.py`, `send_audio` currently always calls
`pcm16_to_mulaw`. Add a branch:

```python
async def send_audio(self, chunk: AudioChunk) -> None:
    if chunk.format.encoding == "mulaw" and chunk.format.sample_rate == 8000:
        mulaw_data = chunk.data
    else:
        mulaw_data = pcm16_to_mulaw(chunk.data, chunk.format.sample_rate)
    payload = base64.b64encode(mulaw_data).decode("ascii")
    ...
```

Mirror the same branch in the secondary code path at line ~400. Safe
change: `pcm16_to_mulaw` today would mangle μ-law input (it runs the
PCM→μ-law table over μ-law bytes); this branch is a strict correctness
gate, not just an optimisation.

### Interruption estimation is already format-agnostic

Checked: `session/_interruption.py` works in raw byte counts and
per-chunk `duration_ms`, which is derived from `AudioChunk.num_samples
/ sample_rate`. `AudioChunk.frame_size = channels * sample_width`, so
μ-law (`sample_width=1`) computes correctly. No changes needed —
verify with a regression test.

## File-by-file

### Edits

| File | Edit |
|---|---|
| `src/easycat/audio_format.py` | Add `MULAW_8K = AudioFormat(sample_rate=8000, channels=1, sample_width=1, encoding="mulaw")` constant. |
| `src/easycat/audio_utils.py` | Move `_mulaw_encode`, `_mulaw_decode`, `pcm16_to_mulaw`, `mulaw_to_pcm16` here from `transports/twilio_media.py`. Re-export from `twilio_media.py` for backward compat. |
| `src/easycat/providers.py` | Add `preferred_tts_format: AudioFormat` to the `Transport` Protocol. |
| `src/easycat/transports/local.py` | Declare `preferred_tts_format = PCM16_MONO_24K`. |
| `src/easycat/transports/websocket.py` | Declare `preferred_tts_format = PCM16_MONO_24K`. |
| `src/easycat/transports/webrtc.py` | Declare `preferred_tts_format = PCM16_MONO_48K`. |
| `src/easycat/transports/twilio_media.py` | Declare `preferred_tts_format = PCM16_MONO_8K` (Stage 1). Add μ-law 8k pass-through branch in both `send_audio` paths. Stage 2: bump preference to `MULAW_8K`. |
| `src/easycat/tts/base.py` | Extend `_normalize_audio` with encoding conversion (PCM16 ↔ μ-law). Keep the existing resample/mono paths as-is. |
| `src/easycat/tts/openai_tts.py` | No change to API request (OpenAI has no 8 kHz option). `_source_format` stays at `_OPENAI_PCM_FORMAT`; `_normalize_audio` handles the bridge to whatever `output_format` the session picked. |
| `src/easycat/tts/deepgram_tts.py` | Add `_request_params_for(output_format)`. Drive `_build_url` + `_source_format` from its output. |
| `src/easycat/tts/elevenlabs_tts.py` | Add `_request_params_for(output_format)`. Extend `_ELEVENLABS_FORMAT_MAP` with `ulaw_8000`. Lift the PCM-only guard in `__post_init__` — allow μ-law, keep the error for encodings we genuinely can't decode (mp3, opus). |
| `src/easycat/tts/cartesia_tts.py` (new per `peripheral-cartesia-provider.md`) | Ship with `_request_params_for` from day one — JSON `output_format` field derives from the target. Default target stays `PCM16_MONO_24K`; transport override handles the telephony case. |
| `src/easycat/config.py` | In `create_session()`, after building the TTS config, check `tts_config.output_format` against the dataclass default. If default, set it to `transport.preferred_tts_format` via `replace`. Emit a `TransportFormatApplied` event on the bus. |
| `src/easycat/events.py` | Add `TransportFormatApplied` dataclass event. |

### New tests

| File | Test |
|---|---|
| `tests/tts/test_tts_base.py` | Add cases for `_normalize_audio`: PCM16 24k → μ-law 8k (resample + encode); μ-law 8k → PCM16 24k (decode + resample); PCM16 8k → μ-law 8k (encode only, no resample). |
| `tests/tts/test_tts_deepgram.py` | Add a parametrized case: `output_format=PCM16_MONO_8K` ⇒ URL carries `sample_rate=8000`, `_source_format` matches, no resample in `_normalize_audio`. Same with `MULAW_8K` ⇒ `encoding=mulaw`. |
| `tests/tts/test_tts_elevenlabs.py` | Parametrize over `pcm_8000` and `ulaw_8000` output formats. Confirm `__post_init__` no longer rejects `ulaw_8000`. |
| `tests/tts/test_tts_cartesia.py` (from the Cartesia plan) | Parametrize over PCM16 24k, PCM16 8k, and μ-law 8k targets; confirm the JSON request matches. |
| `tests/session/test_session_transport_format.py` | With `TwilioTransport` + default `DeepgramTTSConfig`, assert the instantiated provider has `output_format=PCM16_MONO_8K`. With an *explicit* `output_format=PCM16_MONO_24K` in the config, the session respects it (no override). |
| `tests/transports/test_twilio_media.py` | μ-law 8 kHz `AudioChunk` → base64 of raw bytes (no re-encoding). PCM16 8 kHz `AudioChunk` → `_mulaw_encode` only, no resample. |
| `tests/session/test_interruption.py` | Regression case: interruption estimation with μ-law 8 kHz chunks produces the same spoken-text output as the PCM16 24 kHz baseline, given equivalent send-log durations. |

## Sequencing

**Stage 1 — PCM16 @ 8 kHz across all providers (primary win)**

1. Land `audio_utils.py` relocation of μ-law helpers + `MULAW_8K`
   constant. No behaviour change.
2. Extend `_normalize_audio` with encoding conversion + unit tests.
3. Add `preferred_tts_format` to the Transport protocol; set it on
   each transport. Twilio = `PCM16_MONO_8K`.
4. Refactor one provider (Deepgram is the safest — smallest adapter,
   best test coverage) to read `self._output_format` and drive the
   URL from it. Land with tests.
5. Apply the same refactor to ElevenLabs and Cartesia. OpenAI only
   needs the downstream `_normalize_audio` path, no provider-side
   change.
6. Wire the `create_session()` transport → output_format override and
   `TransportFormatApplied` event.

**Stage 2 — μ-law pass-through on Twilio (optimization)**

7. Bump `TwilioTransport.preferred_tts_format` to `MULAW_8K`.
8. Add the μ-law 8 kHz pass-through branch in `send_audio`.
9. Parametrize per-provider tests over `MULAW_8K` targets.

Stage 1 is the load-bearing change. Stage 2 is a follow-up that can
land independently once Stage 1 is stable in production. Splitting
them keeps the blast radius of each PR contained.

## Risks & Open Questions

- **Model quality at 8 kHz.** All four providers produce their own
  model output at a fixed internal rate (typically 22.05 / 24 kHz) and
  downsample on the way out. Provider-side downsample quality is
  usually better than ours, so this should be a small win on the
  quality axis too. Worth a subjective A/B on one voice per provider
  during Stage 1 review.
- **Explicit-vs-default detection.** The "respect user's explicit
  `output_format`" rule relies on detecting a non-default value. The
  cleanest version is making `output_format` default to `None` on
  every TTSConfig and normalising during session build. Weigh diff
  size vs clarity in review — either is acceptable.
- **OpenAI TTS.** Stays at 24 kHz natively. For Twilio + OpenAI the
  24→8 kHz resample still happens — but inside `_normalize_audio`
  (our code) instead of inside `pcm16_to_mulaw` (our code). No
  performance change. Flagged for future work only if OpenAI exposes
  a lower-rate option.
- **Stage 2 μ-law path and interruption estimation.** Regression-
  tested but worth a careful look: μ-law is 1 byte/sample, so
  byte-count heuristics that implicitly assume 2 bytes/sample (none
  found, but verify) would silently drift 2×.
- **Backward compatibility of `TTSConfig.output_format`.** If we flip
  the default to `None`, anyone constructing a config by hand with
  positional args needs to audit. Only affects external tests /
  examples; no production concern for in-tree callers.

## See Also

- **STT input path.** Symmetric opportunity: Deepgram STT and Cartesia
  STT both accept μ-law 8 kHz input natively. Feeding raw μ-law to STT
  on telephony would eliminate the `mulaw_to_pcm16` resample at the
  ingress side of `TwilioTransport` too. Out of scope here — file a
  follow-up plan if the telephony profile becomes a latency-priority
  workload.
- `peripheral-cartesia-provider.md` — remove the "v2 follow-up" note
  about 8 kHz output once this plan lands; cite this doc instead.
- `peripheral-observability-and-cost.md` — bundle-size reduction from
  native 8 kHz is the sort of thing the cost model should report.
