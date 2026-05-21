# Chapter 0 — Hello, Audio

> **Historical planning note.** The shipped curriculum lives under `docs/teaching/`;
> use this file for original intent and rationale.
>
> Record, play, and *understand* raw PCM. No framework. Just bytes
> and sample rates.

## Prerequisites

- Python 3.11+
- `uv sync --group dev`
- A working microphone and speakers (any OS)

## Learning objectives

By the end of this chapter, the reader can:

1. Explain sample rate, bit depth, channels, and frame size in their
   own words.
2. Predict the size in bytes of N seconds of PCM at a given sample
   rate/depth/channels.
3. Feel the difference between a 10ms chunk and a 200ms chunk when
   played back — latency is not abstract, it is chunk size plus
   scheduling jitter.

## What you build

`docs/teaching/00-hello-audio/main.py`:

- Records 3 seconds of audio at 16 kHz mono int16.
- Plays it back.
- Prints the byte size of the recording and the arithmetic that
  explains it.
- Replays with different chunk sizes so the reader can hear the
  chunking-latency tradeoff.

## Narrative arc

1. **The simplest possible thing.** `sd.rec(3 * 16000, samplerate=16000, channels=1, dtype='int16')`.
2. **What just happened?** Audio is an array of numbers sampled
   16,000 times per second. Show the raw NumPy array. Point out
   that zero-crossings look like speech if you squint.
3. **Why 16 kHz and not 44.1 kHz?** Speech bandwidth stops around
   8 kHz (Nyquist → 16 kHz sample rate is enough). Music doesn't.
   Cost per minute of audio storage/transfer matters at scale.
4. **Chunk size demo.** Replay the same audio with 10ms chunks vs
   200ms chunks. First feels instant, second feels slow-start. This
   is the entire justification for streaming later in the ladder.
5. **The math.** `3 s × 16000 samples/s × 2 bytes/sample = 96000 B`.
   Confirm with `len(buffer.tobytes())`.

## Key concepts

No EasyCat internals yet. This chapter is deliberately
**pre-framework**. The reader should finish with a gut feel for
audio as "numbers in time" before any abstraction layers appear.

- `sounddevice` (already a dev dep)
- NumPy `int16` arrays and `.tobytes()`
- Sample rate, bit depth, channels, frame size

## Exercises

1. Change the sample rate to 8 kHz. Is speech still intelligible?
   Is music?
2. Hand-generate a 440 Hz sine wave and play it. Why does doubling
   the frequency halve the period? Predict, then confirm.
3. Record at `int16` vs `float32`. Same perceived quality? Why
   would a professional pipeline prefer float? (Hint: headroom,
   composable gain.)

## Journal highlights

None — no journal yet. The journal shows up in chapter 2.

## Files created

- `docs/teaching/README.md` (bootstrapped here; the landing page
  and table of contents for the whole ladder)
- `docs/teaching/00-hello-audio/main.py`
- `docs/teaching/00-hello-audio/README.md` (narrative for this
  chapter)

## Success criteria

- The reader can answer: *"If I want a 50ms chunk at 24 kHz stereo
  float32, how many bytes is that?"* without looking it up.
  (Answer: 50 × 24 × 2 × 4 = 9600 B.)
- The reader has heard, with their own ears, the difference between
  a 10ms and a 200ms playback chunk.

## Links forward

Chapter 1 takes the same PCM stream and moves it through EasyCat's
`Transport` protocol as async chunks, instead of a single blocking
`rec()` call.
