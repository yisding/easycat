# Chapter 4 — VAD + Pre-roll

> Real speech detection. And why the buffer *before* the detection
> matters as much as the detection itself.

## Prerequisites

- [Chapter 3](../03-parrot-naive/) (ideally with breaker recordings
  in your ears)
- `uv sync --extra quickstart --group dev` — the `quickstart`
  extra pulls in `onnxruntime`, which Silero VAD needs.
- `OPENAI_API_KEY` (TTS) and `DEEPGRAM_API_KEY` (STT).

> **Minimum to skip the ladder:** chapter 2 (STT events). Chapter
> 3 is the motivation; you can read its README without running
> it. The bonus `naive_threshold.py` here is the wrong-version
> warm-up for this chapter — see "The naive predecessor" below.

## Diff from chapter 3

- **Added:** `create_vad()` + a `MiniTurnDetector` with a 300 ms
  pre-roll ring buffer; a `--no-preroll` flag to demonstrate
  start-of-utterance truncation; `naive_threshold.py` showing why
  an energy threshold isn't enough.
- **Modified:** turns now commit on VAD boundaries, not on a
  fixed-timeout absence of STT partials.
- **Removed:** the silence-timeout turn detector from chapter 3.

## The naive predecessor

Before reaching for Silero, read `naive_threshold.py`:

```bash
uv run python docs/teaching/04-vad-preroll/naive_threshold.py
```

It classifies a chunk as speech if its RMS energy exceeds a fixed
threshold. **Wrong-version-first** warm-up: it fires on every
keyboard click, drops out mid-vowel for soft talkers, and never
fires at all next to a fan. The script logs each false-fire to
the journal so you can read the misclassifications back. Once
you've heard it fail on your own voice, the rest of this chapter
(real VAD + pre-roll) lands harder.

## Run it

```bash
# With pre-roll: the start of every word survives.
uv run python docs/teaching/04-vad-preroll/main.py

# Without pre-roll: "Hello" becomes "ello."
uv run python docs/teaching/04-vad-preroll/main.py --no-preroll
```

Say "Hello" ten times under each setting. Listen to the parrot.
That is the demo.

## What a VAD actually does

It classifies a small audio frame (10-30 ms) as **speech** or
**not-speech**. That's all. VAD is not a turn detector; it is the
primitive that makes a turn detector possible.

`easycat.vad.factory.create_vad()` picks a backend automatically:
Silero → FunASR → TEN → Krisp. Silero is the default; its ONNX
model is bundled.

## The pre-roll problem

A VAD is a decision made *after* it has seen enough audio. Fast
backends fire 100-200 ms late — you said "Hello," but the VAD's
"speech" verdict lands sometime during the "e." If you only forward
audio chunks that arrive *after* VAD-on, your STT hears "ello"
and confidently transcribes "Elo."

## The pre-roll fix

Keep a short ring buffer of recent audio (we use 300 ms, about 15
chunks of 20 ms at 24 kHz). When VAD fires, flush the buffer into
STT first, then forward live chunks. STT sees the full "Hello."

```mermaid
flowchart LR
    Mic[mic chunks] --> Ring["pre-roll ring buffer<br/>(15 chunks ≈ 300 ms,<br/>oldest dropped)"]
    Ring -. cache while<br/>VAD silent .-> Ring
    VAD([VAD fires:<br/>speech!]) -. triggers flush .-> Ring
    Ring -- "1. flush cached<br/>chunks first" --> STT
    Mic -- "2. then live chunks<br/>(direct)" --> STT
```

The mic feeds the ring buffer continuously (oldest chunk drops out
every 20 ms). When VAD fires, the whole buffer is flushed to STT
first — so STT sees the 300 ms that arrived *before* the VAD
decision — and live chunks then flow directly to STT.

## `MiniTurnDetector`

About 40 lines of actual logic. Three state transitions:

| From  | On              | Action                             |
|-------|-----------------|------------------------------------|
| idle  | VADStart        | Flush pre-roll, emit `speech_started` |
| speak | each chunk      | Emit `frame`                        |
| speak | VADStop         | Emit `speech_ended`, drop STT stream|
| idle  | each chunk      | Append to pre-roll ring             |

You are writing this yourself in ~40 lines. EasyCat's production
`TurnManager` (`src/easycat/turn_manager.py`) is a 5-state FSM
covering overlap with bot speech, cancellation, push-to-talk,
session actions. Every extra state defends against a specific
thing your `MiniTurnDetector` can't handle; after you finish this
chapter, open that file and pattern-match the extras to the
problems they solve.

## Try breaking it

Say the same breakers you tortured chapter 3 with ("the capital
of France is... uh... Paris", "apples, bananas, pears", a yes/no
question) and run the script **twice** — once with pre-roll on,
once with `--no-preroll`. Open both bundles:

```python
from easycat.debug.testing import load_bundle
for which in ("preroll", "nopreroll"):
    for b in Path("docs/teaching/04-vad-preroll/runs/").glob(
        f"ch04-vad-{which}-*.bundle"
    ):
        bundle = load_bundle(b)
        print(which, [
            r["data"].get("text") for r in bundle.records()
            if r["name"] == "turn.ended"
        ])
```

You should see:

- Fewer chopped first syllables in the `preroll` run.
- The "uh… Paris" breaker now survives (the "uh" has speech energy,
  so VAD stays on straight through).
- Lists are still fragile: commas are often below the speech
  threshold and VAD fires `VADStopSpeaking` between items.
- New failures: a cough, a door slam, or keyboard clicks can
  trigger false VAD-ons.

VAD is still a threshold. It just trips on a much better feature
than "has the STT stream been quiet?" The remaining false-fires
are noise-reduction's job — [chapter 10](../10-cleaning-signal/).

## What's next

[Chapter 5 — The blocking agent](../05-blocking-agent/) drops the
parrot and puts a real LLM at the heart of the loop. The turn
latency problem we traded *away* in chapter 3 comes back, worse,
in a new form.
