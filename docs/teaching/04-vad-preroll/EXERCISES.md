# Chapter 4 — Exercises

[← Back to chapter](./README.md) · [Ladder index](../)

## 1. Diff your breakers across preroll on/off

**Task.** First run the provider-free frame probe:

```bash
uv run python docs/teaching/04-vad-preroll/preroll_probe.py
```

Explain why `cached-1` and `cached-2` appear after
`speech_started` only in `with_preroll`, while `trigger` and `live`
appear in both traces. Then say chapter 3's breakers — *"the capital
of France is… uh… Paris"*, *"apples, bananas, pears"*, a yes/no
question — through `main.py` **with** and **without** pre-roll, and
read both bundles side-by-side.

```python
from pathlib import Path
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

**Hints**

1. The deterministic guarantee is frame routing: without pre-roll,
   audio received before `VADStartSpeaking` is absent from the STT
   stream; with pre-roll, up to the configured 300 ms is replayed in
   order before the trigger frame. How much leading speech VAD misses
   varies by utterance, backend, and audio conditions—it is not a fixed
   ~100 ms.
2. Transcript and confidence changes are observations, not invariants.
   The no-pre-roll run may mis-hear a leading word ("Hello" → "Elo"),
   while another run may transcribe both versions identically.
3. Pre-roll fixes turn **start** clipping; it does not change endpointing.
   The "uh… Paris" breaker stays in one turn only if VAD remains active
   through the pause. If VAD emits `VADStopSpeaking`, the detector still
   splits it.
4. The comma list ("apples, bananas, pears") is still fragile —
   commas are often 300-500 ms of *real* silence below the speech
   threshold, so VAD drops out between items. Smart-turn (ch 8)
   is the right fix for that one.
5. The new failure mode: VAD false-fires on coughs, door slams,
   keyboard typing. Chapter 10's NR is the answer.

## 2. Compare against `naive_threshold.py`

**Task.** Run `naive_threshold.py` and try the same breakers.

```bash
uv run python docs/teaching/04-vad-preroll/naive_threshold.py
```

For each breaker, note: did the threshold fire early, fire late,
or fire correctly? Then explain in one sentence why a real VAD
(Silero) gets the same case right.

**Hints**

1. The threshold has no learned model of speech vs noise — it just
   measures `sqrt(mean(x**2))`. Anything energetic gets through;
   anything quiet gets dropped.
2. Silero is a small neural net trained on speech vs not-speech. A
   fan is noisy but has a *different spectrum* from speech, so
   Silero ignores it; the threshold can't tell them apart.
3. The journal records both backends' verdicts per chunk. If you
   produce both bundles and overlay them on the same input
   (recorded `.wav`), you'll see Silero's verdicts arrive 50-100
   ms later (it needs context) but be vastly more accurate.

## 3. Read the production turn manager

**Task.** Open `src/easycat/turn_manager.py` and find each of the
five states (`IDLE`, `USER_SPEAKING`, `USER_PAUSED`, `PROCESSING`,
`BOT_SPEAKING`). For each state, name the *single thing* it
defends against that your `MiniTurnDetector` can't handle.

**Hints**

1. `USER_PAUSED` is for the comma-list problem from exercise 1.
2. `PROCESSING` separates "user done speaking" from "bot
   answering" — the gap measured in chapter 5.
3. `BOT_SPEAKING` is the thing that makes chapter 9's barge-in
   possible — the FSM needs to *know* the bot is speaking to know
   that a new VAD-on is an interruption.
4. The transitions matter as much as the states. The README on
   chapter 4's `MiniTurnDetector` only has 4 transitions. The
   production FSM has ~15.

## Self-check

You should be able to look at a VAD-based pipeline and predict
which utterances will break it (lists, soft talkers, leading
quiet syllables) without running them.
