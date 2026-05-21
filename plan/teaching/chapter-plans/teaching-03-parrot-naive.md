# Chapter 3 — Parrot, the Naive Way

> **Historical planning note.** The shipped curriculum lives under `docs/teaching/`;
> use this file for original intent and rationale.
>
> A bot that repeats what you said. Except it breaks the moment you
> say "um."

This is a **"wrong version first"** chapter. Do not skip it. Do not
collapse it with chapter 4.

## Prerequisites

- Chapter 2
- A TTS provider (OpenAI default; any from `tts/factory.py`)

## Learning objectives

1. Implement turn-taking with a fixed silence timeout and personally
   experience its failure modes.
2. Explain why "the user is done speaking" is not a boolean that can
   be read off the microphone.
3. Recognize hedged speech, thinking pauses, and trailing intonation
   as the enemies of naive turn detection.

## What you build

`docs/teaching/03-parrot-naive/main.py`:

- Starts from a copy of `docs/teaching/02-transcribe/streaming.py`.
- Consumes STT partials.
- When **N** ms of silence pass since the last partial (default
  500ms), the last seen partial becomes "final" and is sent to TTS
  via `easycat.speak(transport, text)` (the quick helper).
- Plays TTS back. Repeats forever.

This script explicitly **does not use** `VAD`, `turn_manager`, or
`Session`. We are reinventing that layer poorly on purpose. When
the reader feels the pain, chapter 4 will sell them on the real
thing.

## Narrative arc

1. **The naive plan.** "If no new STT partial has arrived in 500ms,
   the user is done — promote the last partial to final and fire
   TTS." Reasonable-sounding. Note that we are *explicitly
   breaking* chapter 2's "never act on partials, only on
   `STTFinal`" rule. That's part of the wrong-version-first
   payload: by the end of the chapter, you will *feel* why the
   rule exists. Chapter 4 restores it by waiting for a real turn
   boundary from the VAD.
2. **It works!** Short, clean, confident sentences work fine.
3. **Break it, deliberately.** Say each of these and observe:
   - "The capital of France is... uh... Paris."
   - "I was thinking... [long pause] ...we should order pizza."
   - A yes/no question with rising intonation.
   - A list: "apples, bananas, pears."
4. **Why it breaks.** Silence ≠ done. Thinking silence, breathing
   silence, and actual end-of-turn silence are indistinguishable
   at the STT-partial layer.
5. **Dump the journal.** Look at the STT timeline; find the exact
   moment the bot wrongly committed. The timestamp of the last
   partial plus 500ms — precisely.

## The naive bug, visualized

In the chapter README, include a side-by-side timeline diagram:

- Real speech segments (colored bars).
- What the bot *thought* was the turn boundary (a vertical line).
- The misprediction highlighted in red.

## Key concepts

- None from EasyCat — we are building a bad alternative to
  `turn_manager` so the reader can feel the pain.
- Silence detection by absence-of-partial-events
- Minimum-turn-length heuristics (a patch that helps a little,
  not enough)

## Exercises

1. Change the silence timeout from 500ms to 2000ms. Gain: fewer
   false triggers. Loss: feels sluggish. Find *your* preferred
   tradeoff on your own voice.
2. Add a minimum-turn-length check (ignore "turns" < 1 second of
   speech). Does it help? What does it break?
3. Record five phrases that always break your implementation. Save
   them to `docs/teaching/03-parrot-naive/breakers/`. Chapter 4
   will replay them through the real VAD.

## Journal highlights

- A stream of STT partials with wall-clock timestamps.
- Your hand-rolled turn-boundary events (custom records, since no
  `turn_manager` yet).
- Conspicuously absent: `vad.active`, `turn_manager.transition` —
  these will appear in chapter 4 and the contrast should be visible.

## Files created

- `docs/teaching/03-parrot-naive/main.py`
- `docs/teaching/03-parrot-naive/README.md`
- `docs/teaching/03-parrot-naive/breakers/` (reader-produced
  recordings; a couple of canonical samples checked in, rest
  generated locally)

## Success criteria

- The reader has personally experienced at least three distinct
  failure modes of timeout-based turn detection.
- The reader is *motivated* to learn VAD — they are asking for it
  before chapter 4 starts.

## Links forward

Chapter 4 replaces the silence timeout with a real VAD + pre-roll
buffer, and replays these same breaker recordings through it.
