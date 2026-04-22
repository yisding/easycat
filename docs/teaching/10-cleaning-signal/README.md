# Chapter 10 — Cleaning the Signal

> Two problems often confused as one. **Noise reduction** removes
> uncorrelated background sound (fan, keyboard, baby).
> **Echo cancellation** removes the bot's own voice coming back
> through the microphone. Same pipeline slot; fundamentally
> different techniques.

## Prerequisites

- [Chapter 9](../09-interruption/)
- For real NR: `uv pip install -e '.[rnnoise]'` (permissive
  RNNoise) or Krisp per its own SDK.
- For real AEC: `uv pip install -e '.[aec]'` (LiveKit APM).
- `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`.

Both factories **silently fall back to passthrough** when their
deps are missing. The script prints and journals the live backend
so you know which one you're actually hearing.

**Scope note.** This chapter isolates the NR/AEC axis. It drops
chapter 9c's `TurnLedger` / history rewrite (the LLM's memory
goes back to one-shot) so you can focus on the signal cleaning
without extra moving parts. If you want both, merge the two
files — nothing prevents it.

## Two ways to run this chapter

### A — live (speakerphone + real voice)

```bash
uv run python docs/teaching/10-cleaning-signal/main.py --nr off --aec off
uv run python docs/teaching/10-cleaning-signal/main.py --nr on  --aec off
uv run python docs/teaching/10-cleaning-signal/main.py --nr off --aec on
uv run python docs/teaching/10-cleaning-signal/main.py --nr on  --aec on
```

**For the AEC cell you need mic + speaker in the same laptop, no
headphones** — if the bot's audio never reaches the mic, AEC has
nothing to cancel. Chapter 9 asked you to use headphones; for
this chapter's AEC demo, take them off.

### B — offline replay (deterministic fixtures)

Generate a synthetic fixture set once, then replay any condition
through `replay.py`:

```bash
uv run python docs/teaching/10-cleaning-signal/generate_fixtures.py
uv run python docs/teaching/10-cleaning-signal/replay.py \
    --mic recordings/speakerphone_loop.mic.wav \
    --ref recordings/speakerphone_loop.ref.wav \
    --nr on --aec on
```

The fixtures are toy signals (sine-wave "voice," deterministic
white noise, a 30 ms echo at -18 dB) — enough to exercise the
lockstep `feed_reference` path and dump bundles the journal can
compare. They are **not** a substitute for a real speech test
set. Replace the WAV pairs with your own recordings for a real
eval.

## The pipeline

```
               raw mic
                 │
                 ▼
             ┌───────┐      ┌───────┐     ┌─────┐     ┌─────┐
             │  NR   │ ───► │  AEC  │───► │ VAD │───► │ STT │──► agent
             └───────┘      └───────┘     └─────┘     └─────┘
              (fan,          ▲                                       │
              keyboard,      │  reference = what we                  │
              baby)          │  asked the speaker to play           TTS
                             │                                       │
                             └──────────────── aec.feed_reference ◄──┘
```

- **NR** is *single-input*. It sees only the mic and subtracts a
  learned model of stationary noise. It does **not** know what
  the bot is saying. From NR's perspective the bot's voice coming
  back through the speaker is *signal* — real speech.
- **AEC** is *dual-input*. It sees the mic *and* the far-end
  reference — the exact PCM we sent to the speaker. It correlates
  the two and subtracts the echo path's filtered version of the
  reference from the mic. That's why the chapter-10 code has a
  new line in `drain_to_speaker`:

  ```python
  await transport.send_audio(event.audio)
  aec.feed_reference(event.audio)   # ← only AEC needs this
  ```

### Why this order

1. **NR before AEC.** AEC's adaptive filter still converges
   because it sees the raw reference on one side and the
   NR-processed mic on the other — it learns the combined
   (echo-path ∘ NR) mapping. NR-first lets NR see the rawest
   possible noise spectrum.
2. **VAD after both.** Before NR, VAD false-triggers on
   stationary noise. Before AEC, VAD false-triggers on the bot's
   own voice. After both, VAD only fires on the user.

Swap either and something specific breaks. Try it.

### Reference-timing caveat

`feed_reference` is called when we *send* a TTS chunk to the
transport, not when the speaker actually radiates it. The physical
echo will arrive at the mic tens of milliseconds later. LiveKit
APM's adaptive filter learns this delay as part of the echo path,
so small misalignments are fine. A large misalignment — e.g. the
TTS stream outruns the mic loop by hundreds of ms — breaks
convergence and you hear audible echo. Production pipelines
compensate with playback-ack marks.

## What's in the journal

Every run writes an `audio.config` record with the live backends:

```python
from pathlib import Path
from easycat import load_bundle
for b in Path("docs/teaching/10-cleaning-signal/runs/").glob("*.bundle"):
    bundle = load_bundle(b)
    for r in bundle.records():
        if r["name"] == "audio.config":
            print(b.name, r["data"])
```

Expect entries like `{"stage": "audio", "nr": "rnnoise", "aec": "livekit"}`
or `{"stage": "audio", "nr": "passthrough", "aec": "off"}` if the
extras weren't installed — *that* is where you catch the silent
fallback.

## Half-duplex vs. full-duplex

A regular telephone speakerphone is half-duplex by hardware: only
one direction transmits at a time. That's why older speakerphones
"clip" when both people talk — the device is literally throwing
one direction away.

AEC is the technique that lets a modern speakerphone *feel*
full-duplex. The speaker's output is subtracted from the mic so
both can be live at once. When AEC is the only thing making a
device feel modern, disabling it in software is the same as
downgrading to 1980s phone hardware.

Headsets sidestep the whole problem: no acoustic path from
speaker to mic.

## Double-talk: the AEC failure mode

When the bot and the user speak at the *same time*, AEC's
adaptive filter has a moving target. Mainstream AECs (LiveKit APM
included) have a "double-talk detector" that freezes filter
adaptation during overlap; aggressive tuning clips the user's
voice audibly. This is the same physical problem as chapter 9's
barge-in, viewed from the other side. Tuning is per-deployment.

## Try breaking it

1. Type loudly on your keyboard while saying "hello." Run each of
   the four modes. Where does VAD fire in each?
2. Run on speakerphone (no headphones) with `--aec off`. The bot
   interrupts itself on chapter 9's `cancel.py` style pipeline.
   Then enable AEC. Compare.
3. Set NR to `off` and AEC to `on` with the `livekit` extra
   installed. AEC runs, but the signal it sees still has fan
   noise. Does the bot sound better, worse, or identical compared
   to NR on + AEC off? Why?

## What's next

[Chapter 11 — The journal as mental model](../11-journal/). The
ladder stops building and starts reading — teaching you the
single query surface the last ten chapters have been dumping
into.
