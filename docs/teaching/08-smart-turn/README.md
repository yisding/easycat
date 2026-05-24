# Chapter 8 — Smart-turn

> A tiny ML model that knows you're done talking before the silence
> confirms it.

## Prerequisites

- [Chapter 7](../07-tools/)
- `uv sync --extra quickstart --group dev` — the `quickstart`
  extra installs `numpy` + `onnxruntime`, which smart-turn needs.
  The 8 MB ONNX model ships bundled in `src/easycat/models/`.
- `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`.

> **Minimum to skip the ladder:** chapter 4 (VAD basics).
> Smart-turn is independent of chapters 5-7 — drop it on top of
> any VAD-gated pipeline.

## Diff from chapter 7

- **Added:** `SmartTurnONNX.detect()` invocation on every
  `VADStopSpeaking`; a `pending` state in `MiniTurnDetector`;
  `smart_turn.classify` journal records; `--backend {vad,smart}`
  CLI for an A/B comparison; `SMART_THRESHOLD` / `SMART_FALLBACK_MS`
  knobs.
- **Modified:** turn commits on classifier probability instead of
  a fixed silence wait.
- **Removed:** tools — to isolate the endpoint-classification
  concept (one axis per chapter).

## VAD silence is a timeout in disguise

Chapter 4's VAD is great at "this frame contains speech." It is
*not* good at "was that the end of a sentence?" To be safe, a VAD
pipeline waits ~800 ms of silence before calling the turn over.
Most of that 800 ms is slack — the user was done 300-500 ms ago.

Humans don't cue off silence; we cue off **intonation**.

> *"I think we should go."*  — pitch falls at "go" → done.
>
> *"I think we should…"*   — pitch stays level → not done.

Smart-turn is an 8 MB ONNX classifier trained on exactly this
signal. Input: the recent audio. Output: `P(end-of-turn)`.

## Architecture

```
                 every VADStopSpeaking event
                           │
                           ▼
  ┌──────────────┐  audio-so-far  ┌──────────────┐
  │  Vocal track │───────────────►│  Smart-turn  │────► P(done)
  │  (turn_audio)│                │  (ONNX, 8MB) │
  └──────────────┘                └──────────────┘
            ▲                            │
            │                            ▼
  "speaking" deque of              threshold → commit turn
  AudioChunks                      (speech_ended fires now,
                                    not 600 ms later)
```

## Run it both ways

```bash
# Baseline: 800 ms silence timeout, no smart-turn.
uv run python docs/teaching/08-smart-turn/main.py --backend vad

# Smart-turn: 200 ms silence timeout + classifier confirmation.
uv run python docs/teaching/08-smart-turn/main.py --backend smart
```

Ask the same question under each. Read both bundles. Expect
~500-600 ms faster first-audio in the `smart` run on clean
declarative utterances.

## The state machine

`MiniTurnDetector` now has three states:

```
             VADStart                 VADStart
   ┌──────┐──────────►┌──────────┐◄─────────────┐
   │ idle │           │ speaking │              │
   └──────┘◄──────────┴──────────┘──────────┐   │
       ▲     speech_ended         VADStop    │   │
       │     (commit)       (ask smart-turn) │   │
       │                                     ▼   │
       │                              ┌─────────────┐
       └────────── fallback_ms ───────│   pending   │
                  silence  timeout    │ (not done)  │
                                      └─────────────┘
```

Every chunk during a speech or pending segment goes into
`self._turn_audio`. On `VADStopSpeaking`, we call
`smart_turn.detect(turn_audio)` — inference runs via
`asyncio.loop.run_in_executor` inside `SmartTurnONNX.detect`, so
ONNX doesn't block the event loop. Typical cost: 30-50 ms per
call.

- Probability ≥ threshold → commit: emit `speech_ended` now.
- Probability < threshold → pending: do **not** emit; keep
  accumulating audio. If a new `VADStartSpeaking` arrives, resume
  the same turn. If no new speech arrives for `SMART_FALLBACK_MS`
  (800 ms), force-commit.

Every classify call writes a `smart_turn.classify` record to the
journal with `probability`, `prediction`, `confirmed`, and
`inference_ms`.

## Read the journal

```python
from pathlib import Path
from easycat.debug.testing import load_bundle
for b in sorted(Path("docs/teaching/08-smart-turn/runs/").glob("*.bundle")):
    bundle = load_bundle(b)
    for r in bundle.records():
        if r["name"] == "smart_turn.classify":
            d = r["data"]
            print(f"  {b.name}  prob={d['probability']:.2f}  "
                  f"pred={d['prediction']}  infer={d['inference_ms']:.0f}ms")
        if r["name"] == "turn.gap":
            print(f"  {b.name}  turn_gap={r['data']['total_gap_ms']:.0f}ms")
```

## The failure modes

Smart-turn is a classifier, not an oracle. Expected misfires:

- **Rising intonation ("…and?")** — the model may say "not done"
  and force the full timeout fallback. Fine.
- **Lists with level intonation ("apples, bananas, pears")** —
  the model may say "done" after "bananas" if you paused there
  flat. This is a real interrupt-the-user bug.
- **Ambient noise** — the model weights pitch strongly; a
  background hum can confuse it. Chapter 10 cleans this up.

## Production reference

`TurnManager.on_vad_stop` in `src/easycat/turn_manager.py` wires
smart-turn through the same 5-state FSM we pointed at in
chapter 4. The core idea is identical to what we just built;
the production version coordinates with barge-in (chapter 9),
cancel tokens, and the action queue.

## Try breaking it

1. Drop `SMART_THRESHOLD` to `0.3`. Re-run. How often does the
   bot interrupt you now?
2. Record *"I was thinking… we should order pizza."* Run
   `--backend smart`. Read the journal. Did smart-turn say done
   during the "…" pause? (If yes, that's a 300-500 ms latency
   win. If no, the fallback silence timeout will still commit.)
3. Find an utterance where the `vad` backend gets it right and
   `smart` gets it wrong. Keep the bundle — it's a real-world
   misfire to refer back to when you read
   [chapter 12](../12-evals-and-latency/) on eval sets.

## What's next

[Chapter 9 — Interruption / barge-in](../09-interruption/). What
if the user starts talking while the bot is talking? Three wrong
versions first.
