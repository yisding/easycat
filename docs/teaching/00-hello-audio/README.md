# Chapter 0 — Hello, Audio

> Record, play, and *understand* raw PCM. No framework. Just bytes
> and sample rates.

## Prerequisites

- Python 3.11+.
- `uv sync --extra quickstart --group dev` from the repo root.
  The `quickstart` extra bundles `sounddevice` (mic/speaker),
  `numpy` (sample buffers), plus what later chapters need. On
  Linux you may also need `libportaudio2` from your package
  manager.
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
   sizes — so your ears can feel the chunking-latency tradeoff.

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

Human speech energy stops around 8 kHz. By the Nyquist theorem,
sampling twice that — 16 kHz — is enough to reconstruct speech
perfectly. Music, which reaches 20 kHz, needs 44.1 kHz. For a
voice pipeline, doubling the sample rate doubles your bandwidth
for no intelligibility gain.

A few common sample rates you will meet later in the ladder:

| Format | Used by |
|---|---|
| 8 kHz | Telephony (G.711) |
| 16 kHz | Most STT providers (Deepgram, OpenAI Realtime, ElevenLabs) |
| 24 kHz | Many TTS providers (OpenAI default) |
| 48 kHz | WebRTC, pro audio |

## The chunk-size demo

Every stage of a voice pipeline processes audio in *chunks*. The
smaller the chunk, the lower the latency. The larger the chunk,
the less scheduling overhead. Change the chunk size in `main.py`
and you are making exactly the same tradeoff every voice framework
makes every day.

The script prints two numbers per chunk size: *time-to-first-sound*
(how long after `stream.start()` the first sample actually plays)
and *total* wall-clock (which should match the recording length).
The first number is the one your ears feel:

- **10ms chunks** — first sound within a few milliseconds. Feels
  instant.
- **200ms chunks** — a perceptible hesitation before the first
  syllable, then smooth playback. Feels slow-start.

We pass `latency='low'` and a matching `blocksize` to
`sd.OutputStream`. The default `latency='high'` has PortAudio
pre-buffer hundreds of milliseconds of silence before playback
starts, which would flatten the difference we are trying to
hear — the host buffer, not the chunk size, would dominate.

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

## What's next

[Chapter 1 — Echo](../01-echo/) takes the same PCM stream but
moves it through EasyCat's `Transport` protocol as async chunks,
so we can do other things (detect speech, call APIs) while audio
is flowing.
