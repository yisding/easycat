# Chapter 3 — Parrot, the Naive Way

> A bot that repeats what you said. Except it breaks the instant
> you say "um."

**Wrong-version-first chapter.** The whole point of this chapter
is to fail. Do not skip it. Do not read chapter 4 until you have
personally heard this fail on your own voice.

## Prerequisites

- [Chapter 2](../02-transcribe/)
- `uv sync --extra quickstart --group dev`
- `OPENAI_API_KEY` (for TTS) and **`DEEPGRAM_API_KEY`** (the
  parrot needs mid-speech partials, which the OpenAI STT default
  does not produce).

> **Minimum to skip the ladder:** chapters 1-2 (Transport + STT
> events). Chapter 0's PCM math isn't needed here.

## Diff from chapter 2

- **Added:** TTS via `easycat.quick.speak`; a fixed silence-timeout
  turn detector; a conversation loop that keeps running until
  Ctrl-C.
- **New requirement:** `DEEPGRAM_API_KEY` — the parrot's silence
  timer keys off STT partials, which OpenAI's default STT only emits
  after the audio uploads.
- **Modified:** STT events drive an action (speak) instead of just
  printing.

## Run it

```bash
uv run python docs/teaching/03-parrot-naive/main.py
```

Talk. It repeats. Ctrl-C to stop.

## The naive plan

> If no new STT partial has arrived in **500 ms**, the user is
> done. Take the last partial text, hand it to TTS, play it.

Reasonable-sounding. Chapter 2's rule was "never act on partials
— wait for `STTFinal`." We are **deliberately** breaking it here
so you can feel why the rule exists.

## Architecture

```
 ┌─────┐    ┌─────┐   partials+finals   ┌─────────────────┐    ┌─────┐
 │ Mic │ ──►│ STT │ ──────────────────► │ silence-timeout │──► │ TTS │
 └─────┘    └─────┘                     │     parrot      │    └─────┘
                                        └─────────────────┘
                                        (fires on 500 ms
                                         of no STT events)
```

## Break it, deliberately

Say each of these and watch the parrot commit to the wrong thing:

1. **"The capital of France is... uh... Paris."** The 500 ms
   timeout fires during the "uh." The parrot speaks "The capital
   of France is" and then you say "Paris" to an ignoring bot.
2. **"I was thinking... [long pause] ...we should order pizza."**
   Same story. Thinking pauses indistinguishable from done.
3. **A list: "apples, bananas, pears."** Commas are 300-500 ms
   of silence. Bot fires mid-list.
4. **A yes/no question with rising intonation.** A short, clean
   sentence — works! Sometimes. Until the provider partial
   happens to land late and the timeout fires first.

## Why it breaks

Silence is not a boolean that can be read off the microphone:

| What it looks like | What it is |
|---|---|
| 500 ms no partial | End of turn |
| 500 ms no partial | Thinking pause |
| 500 ms no partial | Breath |
| 500 ms no partial | Provider happened to be slow |

The STT partial layer cannot distinguish these. It's a thresholding
decision on the wrong signal. Whatever number you pick for the
timeout, you will get either **false fires** (low number) or a
**sluggish bot** (high number). There is no good value.

## Read the journal

Open the bundle in `runs/`:

```python
from easycat.debug.testing import load_bundle
b = load_bundle("docs/teaching/03-parrot-naive/runs/<file>.bundle")
for r in b.records():
    if r["name"].startswith(("stt.", "parrot.")):
        print(r["sequence"], r["data"].get("offset_ms"), r["name"],
              r["data"].get("text") or r["data"].get("committed_text"))
```

Find the exact moment the parrot committed. The `parrot.fire`
record's `offset_ms` is the last-partial timestamp plus 500 ms —
precisely.

## Try breaking it

Change `SILENCE_TIMEOUT_S` at the top of `main.py` from `0.5` to
`2.0`. Re-run. Observations:

- Fewer false fires on "um."
- Feels sluggish. Turn latency is now permanently 2 seconds.

Then try `0.2`. It will fire on every breath. Somewhere between
the extremes is *your* personal compromise on *your* voice — and
that is still worse than the real thing.

## What you should feel now

Three failure modes, minimum. You should be actively asking for
VAD — for a signal that is "the microphone is currently carrying
speech" rather than "STT has been quiet."

## What's next

[Chapter 4 — VAD + pre-roll](../04-vad-preroll/) replaces the
silence timeout with a real voice-activity detector and a
pre-roll buffer, then replays your breakers through it.
