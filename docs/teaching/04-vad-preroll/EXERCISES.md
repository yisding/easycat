# Chapter 4 — Exercises

<!-- BEGIN auto:navigation -->
[← Back to chapter](./README.md) · [Ladder index](../) · [Progress worksheet](../PROGRESS.md) · [Chapter 5 — The Blocking Agent →](../05-blocking-agent/)
<!-- END auto:navigation -->

<!-- BEGIN auto:exercise-protocol -->
> **Completion evidence for every task**
>
> 1. **Before hints:** keep your initial prediction or plan.
> 2. **After the attempt:** keep the exact command or change and one observed field,
>    measurement, or behavior.
> 3. **Before moving on:** explain in one sentence why the evidence supports or changes
>    your model.
>
> A task is complete when all three are present. Keep a wrong first answer visible;
> it is evidence to explain after revealing hints, not an answer to rewrite.
<!-- END auto:exercise-protocol -->

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

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 5</summary>

The deterministic guarantee is frame routing: without pre-roll,
   audio received before `VADStartSpeaking` is absent from the STT
   stream; with pre-roll, up to the configured 300 ms is replayed in
   order before the trigger frame. How much leading speech VAD misses
   varies by utterance, backend, and audio conditions—it is not a fixed
   ~100 ms.

</details>

<details markdown="1">
<summary>Hint 2 of 5</summary>

Transcript and confidence changes are observations, not invariants.
   The no-pre-roll run may mis-hear a leading word ("Hello" → "Elo"),
   while another run may transcribe both versions identically.

</details>

<details markdown="1">
<summary>Hint 3 of 5</summary>

Pre-roll fixes turn **start** clipping; it does not change endpointing.
   The "uh… Paris" breaker stays in one turn only if VAD remains active
   through the pause. If VAD emits `VADStopSpeaking`, the detector still
   splits it.

</details>

<details markdown="1">
<summary>Hint 4 of 5</summary>

The comma list ("apples, bananas, pears") is still fragile —
   commas are often 300-500 ms of *real* silence below the speech
   threshold, so VAD drops out between items. Smart-turn (ch 8)
   is the right fix for that one.

</details>

<details markdown="1">
<summary>Hint 5 of 5</summary>

The new failure mode: VAD false-fires on coughs, door slams,
   keyboard typing. Chapter 10's NR is the answer.

</details>
<!-- END auto:exercise-hints -->

## 2. Compare against `naive_threshold.py`

**Task.** Run `naive_threshold.py` and try the same breakers.

```bash
uv run python docs/teaching/04-vad-preroll/naive_threshold.py
```

For each breaker, note: did the threshold fire early, fire late,
or fire correctly? Then explain in one sentence why a real VAD
(Silero) gets the same case right.

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 3</summary>

The threshold has no learned model of speech vs noise — it just
   measures `sqrt(mean(x**2))`. Anything energetic gets through;
   anything quiet gets dropped.

</details>

<details markdown="1">
<summary>Hint 2 of 3</summary>

Silero is a small neural net trained on speech vs not-speech. A
   fan is noisy but has a *different spectrum* from speech, so
   Silero ignores it; the threshold can't tell them apart.

</details>

<details markdown="1">
<summary>Hint 3 of 3</summary>

The journal records both backends' verdicts per chunk. If you
   produce both bundles and overlay them on the same input
   (recorded `.wav`), you'll see Silero's verdicts arrive 50-100
   ms later (it needs context) but be vastly more accurate.

</details>
<!-- END auto:exercise-hints -->

## 3. Read the production turn manager

**Task.** Open `src/easycat/turn_manager.py` and find each of the
five states (`IDLE`, `USER_SPEAKING`, `USER_PAUSED`, `PROCESSING`,
`BOT_SPEAKING`). For each state, name the *single thing* it
defends against that your `MiniTurnDetector` can't handle.

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 4</summary>

`USER_PAUSED` is for the comma-list problem from exercise 1.

</details>

<details markdown="1">
<summary>Hint 2 of 4</summary>

`PROCESSING` separates "user done speaking" from "bot
   answering" — the gap measured in chapter 5.

</details>

<details markdown="1">
<summary>Hint 3 of 4</summary>

`BOT_SPEAKING` is the thing that makes chapter 9's barge-in
   possible — the FSM needs to *know* the bot is speaking to know
   that a new VAD-on is an interruption.

</details>

<details markdown="1">
<summary>Hint 4 of 4</summary>

The transitions matter as much as the states. The README on
   chapter 4's `MiniTurnDetector` only has 4 transitions. The
   production FSM has ~15.

</details>
<!-- END auto:exercise-hints -->

## 4. Cancel before speech ends

**Task.** Run the provider-free cleanup probe:

```bash
uv run python docs/teaching/04-vad-preroll/stt_cleanup_probe.py
```

Remove the outer `finally` in `parrot()` and rerun. Which counter changes on
the cancelled path? Restore it and explain why `end_stream()` and final
provider cleanup are separate operations.

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 3</summary>

The normal path ends and closes exactly once after draining STT events.

</details>

<details markdown="1">
<summary>Hint 2 of 3</summary>

The cancelled path never receives `speech_ended`, so only the outer
   `finally` can end and close its active provider.

</details>

<details markdown="1">
<summary>Hint 3 of 3</summary>

`close_if_supported()` is capability-based: providers without a cleanup
   hook remain valid, while persistent providers release their resources.

</details>
<!-- END auto:exercise-hints -->

## 5. Preserve output evidence after fixing input

**Task.** Run the provider-free delivery probe:

```bash
uv run python docs/teaching/04-vad-preroll/delivery_probe.py
```

Explain why `turn.ended` appears before `parrot.delivery`, what the one rejected
chunk proves, and what the two accepted chunks do **not** prove. Then remove
the assignment around `await speak(...)` in a scratch copy of `main.py`. Which
postmortem question becomes unanswerable?

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 5</summary>

`turn.ended` is input-side evidence: VAD ended the turn and STT produced the
   final text.

</details>

<details markdown="1">
<summary>Hint 2 of 5</summary>

`parrot.delivery` is output-side evidence: the same committed text was
   synthesized and offered to the transport.

</details>

<details markdown="1">
<summary>Hint 3 of 5</summary>

A rejection proves a drop. Acceptance proves scheduling, not rendering or
   audibility.

</details>

<details markdown="1">
<summary>Hint 4 of 5</summary>

Fixing start-of-utterance clipping does not let the lesson discard the
   delivery boundary established in chapter 3.

</details>

<details markdown="1">
<summary>Hint 5 of 5</summary>

Chapter 9 adds playback-progress evidence; until then, do not relabel
   accepted chunks as played audio.

</details>
<!-- END auto:exercise-hints -->

## Self-check

<!-- BEGIN auto:self-check-protocol -->
> **Closed-book retrieval gate**
>
> 1. Close the chapter narrative and every hint disclosure.
> 2. Answer every numbered question below from memory, aloud or in writing.
> 3. Support each answer with at least one observed field, measurement, or behavior
>    from your attempt record.
> 4. Mark each answer **pass** or **retry** in your progress record.
>
> If an answer needs notes, reopen only the section that owns the weak concept,
> correct your explanation, close it, and retry. Continue only when every answer
> passes without looking.
<!-- END auto:self-check-protocol -->

1. Which list, soft-talker, or leading-quiet-syllable utterances are most likely
   to break this VAD gate, and which frame-order evidence explains why?
2. Who ends each per-turn STT stream, who closes its provider, and which
   lifecycle records establish that order?
3. How do you preserve transport rejection evidence while avoiding the claim
   that accepted audio was necessarily played?

<!-- BEGIN auto:exercise-completion -->
---
Self-check complete? Prepare the cumulative spine, then replay it through this chapter:

```bash
uv sync --extra quickstart --group dev
uv run python docs/teaching/offline_spine.py --run --through 4 --jobs 4 --show-evidence
```

- [Review the chapter narrative](./README.md)
- [Update the progress worksheet](../PROGRESS.md)
- [Continue to Chapter 5 — The Blocking Agent →](../05-blocking-agent/)
<!-- END auto:exercise-completion -->
