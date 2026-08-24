# Chapter 0 — Hello, Audio

<!-- BEGIN auto:navigation -->
**Progress: 1 of 16** · [Ladder index](../) · [Progress worksheet](../PROGRESS.md) · [Exercises](./EXERCISES.md) · [Chapter 1 — Echo →](../01-echo/)
<!-- END auto:navigation -->

> Record, play, and *understand* raw PCM. No framework. Just bytes
> and sample rates.

<!-- BEGIN auto:offline-checkpoint -->
> **Hardware-free checkpoint:** prove `audio format boundaries` without a microphone,
> speakers, or provider credentials:
>
> **Predict first:** Which rates belong to the wire, pipeline/provider input, provider defaults,
> and WebRTC media boundaries?
>
> ```bash
> uv run python docs/teaching/00-hello-audio/format_boundaries.py
> ```
>
> **Evidence to find:** wire, provider-input, pipeline, config-default, and media roles use
> different rates.
>
> **Explain the result:** If any rate surprised you, name the boundary and the conversion its
> neighbor requires.
>
> [See all 16 checkpoints](../#hardware-free-checkpoint-spine).
<!-- END auto:offline-checkpoint -->

## Prerequisites

- Python 3.11+.
- `uv sync --extra local --group dev` from the repo root.
  The `local` extra bundles `sounddevice` (mic/speaker) and
  `numpy` (sample buffers). On Linux you may also need
  `libportaudio2` from your package manager.
- Later chapters and cumulative checkpoint replay use provider/model extras;
  follow each chapter's prerequisites or install the ladder-level
  [`quickstart`](../../../README.md#install) bundle before continuing.
- A working microphone and speakers.

## Run it

```bash
uv run python docs/teaching/00-hello-audio/main.py
```

The script:

1. Records 3 seconds of audio at 16 kHz mono int16.
2. Prints the byte size of the buffer and the arithmetic that
   explains it.
3. Plays the recording back.
4. Replays it three more times — at 10ms, 50ms, and 200ms chunk
   sizes — while simulating the wait to collect the first live
   chunk, so your ears can feel the chunking-latency tradeoff.

## What is in the buffer

Audio is an array of numbers sampled 16,000 times per second.
Each sample is a 16-bit signed integer — a number between
-32,768 and +32,767 — that represents the instantaneous pressure
at the microphone.

```
time ──►
[  120,  118,  119,  -400, -610, ... ]   ← one int16 per sample
  ^─── 16,000 of these per second
```

The byte math follows directly:

```
seconds × samples/second × bytes/sample × channels = total bytes
   3    ×     16_000     ×       2      ×     1    =   96_000 B
```

## Why 16 kHz?

Nyquist is a statement about an **ideally band-limited signal**:
sampling above twice its highest frequency makes reconstruction
possible under those assumptions. It does not say that speech energy
stops at 8 kHz or that a 16 kHz recording is “perfect.” A 16 kHz sample
rate gives a theoretical upper boundary of 8 kHz; real capture devices,
anti-alias filters, codecs, and resamplers use a narrower passband.

Telephony standards make the distinction concrete. Narrowband
[ITU-T P.342's G.711 profile](https://handle.itu.int/11.1002/1000/3633)
filters speech to roughly 300–3400 Hz and samples at 8 kHz. Wideband
[ITU-T G.722](https://www.itu.int/rec/T-REC-G.722/en) uses 16 kHz
sampling for audio up to 7 kHz. The sample rate sets a ceiling; the
front end and codec decide what useful spectrum survives below it.

Higher rates cost proportionally more raw PCM bytes and may preserve
upper harmonics and speech cues. Whether that improves recognition or
perceived quality depends on the source, model, codec, and playback
path—sample rate alone is not a quality guarantee.

A few common sample rates you will meet later in the ladder:

| Boundary | Current EasyCat default |
|---|---|
| Twilio wire | 8 kHz μ-law |
| Deepgram / Cartesia / ElevenLabs realtime STT target | 16 kHz PCM |
| WebSocket / WebRTC / Twilio pipeline target | 16 kHz PCM |
| `LocalTransport` capture/playback pipeline | 24 kHz PCM |
| OpenAI Realtime STT input | 24 kHz PCM |
| Raw bundled TTS config | 24 kHz PCM before transport alignment |
| WebRTC media frames | 48 kHz before Opus encode / after decode |

These are **boundaries and defaults**, not one rate per provider or
session. Configurable providers can choose other supported rates, batch
OpenAI STT accepts the WAV rate it receives, transports may negotiate or
resample, and a single turn can cross several rates in each direction:

```text
WebRTC peer 48 kHz ──resample──► pipeline 16 kHz ──► Deepgram STT 16 kHz
OpenAI provider-native 24 kHz ──resample──► resolved TTS output 16 kHz
  ──resample──► WebRTC media 48 kHz ──Opus──► peer
```

Run the provider-free catalog to read the maintained runtime defaults
without opening hardware or making an API request:

```bash
uv run python docs/teaching/00-hello-audio/format_boundaries.py
```

That catalog reports `LocalTransport`'s 24 kHz default. This chapter's
smaller raw-`sounddevice` demo is a separate path and deliberately sets
`SAMPLE_RATE = 16_000` so the byte math and speech-band discussion stay
concrete.

### Raw TTS default vs. resolved session output

The 24 kHz TTS rows in that catalog are **config defaults**, not a promise
that every built session emits 24 kHz toward its transport. By default,
`EasyConfig(auto_align_tts_output_to_transport=True)` retargets an untouched
bundled TTS config to the transport's preferred output format:

| Transport config | Resolved TTS transport-output rate |
|---|---:|
| `LocalTransportConfig` | 24 kHz |
| `WebSocketTransportConfig` | 16 kHz |
| `WebRTCTransportConfig` | 16 kHz |
| `TwilioTransportConfig` | 8 kHz |

Run the second provider-free probe to see the four bundled TTS providers
across all four transports:

```bash
uv run python docs/teaching/00-hello-audio/tts_alignment_probe.py
```

The matrix separates the **provider request rate** from the final
**transport-output rate**. They differ for ElevenLabs and OpenAI on
Twilio: ElevenLabs' nearest supported PCM request is 16 kHz, while
OpenAI returns fixed 24 kHz PCM; EasyCat then normalizes either stream
to the 8 kHz transport target. Deepgram and Cartesia can request 8 kHz
directly. “Transport output” still names a format boundary, not proof
that a human heard the audio.

Alignment only rewrites untouched defaults. An explicit non-default output
format is preserved, and `auto_align_tts_output_to_transport=False` opts out
entirely; a later transport boundary may still resample mismatched audio.
This precedence keeps caller intent authoritative while making the common
default path efficient.

Resampling makes formats compatible; it cannot recreate spectrum that an
earlier capture, filter, or codec already removed. Name the boundary when
you discuss a rate—wire, capture, pipeline, provider input, or provider
output—so “16 kHz” is an actionable fact rather than an ambiguous label.

## The chunk-size demo

Every stage of a voice pipeline processes audio in *chunks*. A live
source has to collect a full chunk before it can send that chunk
downstream: 10ms chunks become available every 10ms; 200ms chunks
become available every 200ms. Smaller chunks reduce that batching
delay, while larger chunks reduce scheduling overhead.

Because this recording is already in memory, the script deliberately
waits one chunk duration before opening playback. That models a live
microphone or provider accumulating its first complete chunk; without
the wait, every pre-recorded variant would have data ready immediately
and would not demonstrate source-side chunking latency.

The script prints *time-to-first-write-return* and total wall-clock.
The first number runs from “start collecting a live-sized chunk” until
PortAudio returns from the first `write()`. It is an observable enqueue
boundary, **not proof that the speaker played the sample**—there is no
playback acknowledgement in this small demo. Use your ears for the
actual onset comparison:

- **10ms chunks** — one short source wait plus host latency. Feels
  effectively instant.
- **200ms chunks** — at least a 200ms source wait before the first
  syllable, then smooth playback. Feels slow-start.

We pass `latency='low'` and a matching `blocksize` to
`sd.OutputStream`. The default `latency='high'` can let the host buffer
dominate the delay and flatten the difference we are trying to hear.

The stream is also a context manager. Entering starts it; leaving stops
and closes it even if `write()` raises or you press Ctrl-C. The ladder
keeps this ownership rule from its first hardware handle onward: the
scope that opens a resource also guarantees its teardown.

This is the whole justification for streaming the rest of the
ladder.

## Try breaking it

Change `SAMPLE_RATE` at the top of `main.py` to `8000` and listen.
Speech still intelligible? What about music? (Try humming the
first bar of a song while the recording window is open.) The
answer should match what you just read about Nyquist.

## What you should be able to answer now

> If I want a 50ms chunk at 24 kHz stereo float32, how many bytes
> is that?

> `50 ms × 24 samples/ms × 2 channels × 4 bytes/sample = 9600 B`.

If you had to look that up, read this chapter again.

<!-- BEGIN auto:practice-handoff -->
## Practice and self-check

Work through [the chapter exercises](./EXERCISES.md), then try their closing
self-check from memory. If an answer is weak, rerun the hardware-free
checkpoint or revisit the section that owns the gap.
<!-- END auto:practice-handoff -->

## What's next

[Chapter 1 — Echo](../01-echo/) takes the same PCM stream but
moves it through EasyCat's `Transport` protocol as async chunks,
so we can do other things (detect speech, call APIs) while audio
is flowing.
