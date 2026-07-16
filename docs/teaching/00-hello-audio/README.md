# Chapter 0 — Hello, Audio

<!-- BEGIN auto:navigation -->
**Progress: 1 of 16** · [Ladder index](../) · [Exercises](./EXERCISES.md) · [Chapter 1 →](../01-echo/)
<!-- END auto:navigation -->

> Record, play, and *understand* raw PCM. No framework. Just bytes
> and sample rates.

## Prerequisites

- Python 3.11+.
- `uv sync --extra local --group dev` from the repo root.
  The `local` extra bundles `sounddevice` (mic/speaker) and
  `numpy` (sample buffers). On Linux you may also need
  `libportaudio2` from your package manager.
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

Every stage of a voice pipeline processes audio in *chunks*. A live
source has to collect a full chunk before it can send that chunk
downstream: 10ms chunks become available every 10ms; 200ms chunks
become available every 200ms. Smaller chunks reduce that batching
delay, while larger chunks reduce scheduling overhead.

This script replays an already-complete recording, so all of its
chunks would normally be ready at once. To model a live source
honestly, each replay deliberately waits for one chunk before the
first write. Watch the line appear in the terminal, then listen:

- **10ms chunks** — the simulated source-buffer wait feels instant.
- **200ms chunks** — there is a perceptible hesitation before the
  first syllable, then smooth playback.

The script reports *time-to-first-write*: time from the start of the
simulated source wait until the first blocking `stream.write()`
returns. That is a useful code-path milestone, but it is **not** a
measurement of when sound reaches your ears. Measuring acoustic
time-to-first-sound requires an audio loopback or a second microphone;
device and operating-system buffers add latency after our code writes.
The reported wall-clock includes the simulated wait and playback.

We pass `latency='low'` and a matching `blocksize` to
`sd.OutputStream`. The default `latency='high'` can let host buffering
dominate the comparison and flatten the difference we are trying to
hear.

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
