# Chapter 4 — VAD + Pre-roll

> Real speech detection. And why the buffer *before* the detection
> matters as much as the detection itself.

## Prerequisites

- [Chapter 3](../03-parrot-naive/) (ideally with breaker recordings
  in your ears)
- `uv sync --extra quickstart --group dev` — the `quickstart`
  extra pulls in `onnxruntime`, which Silero VAD needs.
- `OPENAI_API_KEY` (TTS) and `DEEPGRAM_API_KEY` (STT).

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

```
time ──►
                             VAD: "speech!"
                                  ▼
  ┌──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┐ │ ┌──┬──┬──┐
  │──── pre-roll ring buffer (300 ms) ─────────┤ │ │ live  │
  └─────────────────────────────────────────────┘ │ └───────┘
          ▲                                       ▼
     flush these to STT before              then keep
     forwarding live audio                   streaming
```

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
