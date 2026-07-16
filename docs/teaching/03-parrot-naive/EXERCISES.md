# Chapter 3 — Exercises

<!-- BEGIN auto:navigation -->
[← Back to chapter](./README.md) · [Ladder index](../) · [Chapter 4 — VAD + Pre-roll →](../04-vad-preroll/)
<!-- END auto:navigation -->

The whole chapter is one big exercise: feel a bad pipeline
viscerally so the next chapter's fix lands. The README walks you
through four sentences that break it; this file deepens that into
two tractable follow-ups.

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

## 1. There is no good timeout value

**Task.** Change `SILENCE_TIMEOUT_S` at the top of `main.py` to
each of: `0.2`, `0.5` (default), `1.0`, `2.0`. For each value,
record yourself saying:

- *"The capital of France is... uh... Paris."*
- *"Apples, bananas, pears."*
- *"What time is it?"*

For each value × sentence pair, write down: **false fire?**
(parrot commits mid-sentence) or **sluggish?** (parrot waits >1s
after you finish).

<!-- BEGIN auto:exercise-hints -->
<details markdown="1">
<summary>Reveal hints after your first attempt</summary>

**Hints**

1. There is no value at which all six combinations succeed. That's
   the chapter.
2. The closest sweet spot for *your* voice and *your* environment
   is your personal compromise. Even that compromise is dominated
   by a real VAD on the same hardware.
3. The asymmetric pain: false fires interrupt the user (very bad
   UX); sluggish bots just feel slow (bad UX). Voice-product
   teams skew toward sluggish for that reason — the chapter 4 fix
   gets you out of the tradeoff entirely.

</details>
<!-- END auto:exercise-hints -->

## 2. Find the broken moment in the journal

**Task.** Pick a recording where the parrot fired during an "um."
Inspect the bundle with:

```bash
uv run python docs/teaching/03-parrot-naive/inspect_timeout.py PATH
```

Replace `PATH` with the emitted bundle path. For the relevant fire,
find:

- The `trigger_record`: the last `stt.partial` **or** `stt.final`
  before the parrot fired.
- The `parrot.fire` record itself.
- The following `parrot.delivery` record and its accepted/rejected counts.
- The first `stt.partial` *consumed* after the parrot fired and its correlated
  `next_partial_ingress` record (the "Paris" queued while the bot spoke).

Compare `observed_silence_ms` with `configured_timeout_ms`. Why is
the former at least the latter rather than exactly equal? Compare
`post_fire_ingress_gap_ms` with `post_fire_consumer_gap_ms`. How much of the
latter is `consumer_backlog_ms`, and what work was the parrot loop doing then?

<!-- BEGIN auto:exercise-hints -->
<details markdown="1">
<summary>Reveal hints after your first attempt</summary>

**Hints**

1. Every STT event resets `asyncio.wait_for`, including a final. The
   trigger is therefore the latest `stt.partial` or `stt.final`, not
   necessarily the latest partial.
2. `asyncio.wait_for(..., timeout=SILENCE_TIMEOUT_S)` resumes no
   earlier than the deadline. Event-loop scheduling adds the reported
   `scheduler_overshoot_ms`, so an exact equality is not a valid
   invariant.
3. `stt.received` marks provider ingress; the correlated `stt.partial` marks
   consumer dequeue. Their shared `event_id` prevents repeated text from being
   matched by guesswork.
4. The parrot awaits `speak()` before it consumes another queued STT event.
   `post_fire_consumer_gap_ms` therefore includes blocked consumer time;
   `post_fire_ingress_gap_ms` does not.
5. The "Paris" partial is delayed, not dropped. After TTS returns, the parrot
   consumes it and may fire again on that fragment. Production interruption
   handling in chapter 9 instead cancels or ignores current bot audio and
   routes the continuing user turn deliberately.

</details>
<!-- END auto:exercise-hints -->

## 3. Reject one synthesized chunk

**Task.** Run the provider-free output probe:

```bash
uv run python docs/teaching/03-parrot-naive/speak_acceptance_probe.py
```

Change the acceptance sequence to reject all three chunks, then accept all
three. Predict the counts each time. Which result proves that a speaker played
the audio?

<!-- BEGIN auto:exercise-hints -->
<details markdown="1">
<summary>Reveal hints after your first attempt</summary>

**Hints**

1. `recipes.speak()` preserves every `send_audio()` acceptance result instead
   of treating a completed coroutine as delivery.
2. Rejection is actionable drop evidence. Acceptance means scheduled for
   delivery, not rendered by a device or heard by a person.

</details>
<!-- END auto:exercise-hints -->

## 4. Preserve the scaffold while breaking the policy

**Task.** Run the provider-free lifecycle probe:

```bash
uv run python docs/teaching/03-parrot-naive/parrot_lifecycle_probe.py
```

Predict each event list first. Why does `normal_event_end` cancel the
microphone feeder without reporting an error, why does `failed_event_end`
propagate, and which two cases correctly omit `stt.end`?

<!-- BEGIN auto:exercise-hints -->
<details markdown="1">
<summary>Reveal hints after your first attempt</summary>

**Hints**

1. The transport and STT objects exist before `connect()`, so their final
   cleanup is registered first. A logical STT stream exists only after
   `start_stream()` returns.
2. `TaskGroup` cancels siblings when a child raises; it does not cancel them
   merely because one child returns normally.
3. The parrot consumer returns only after it drains the STT listener's `None`
   sentinel. Its wrapper then raises `ParrotEventStreamEndedError`, causing the
   infinite feeder to be cancelled and joined.
4. `except* ParrotEventStreamEndedError` handles that private terminal signal.
   A provider `Error` observed before stream exhaustion makes the listener
   raise the underlying failure instead of queueing the normal sentinel. A real
   feed, listener, or parrot failure remains in the exception group and still
   propagates after cleanup.
5. Removing the timeout would fix the intended Chapter 3 lesson. Removing
   `AsyncExitStack` or `TaskGroup` would instead reintroduce unrelated bugs.

</details>
<!-- END auto:exercise-hints -->

## Self-check

<!-- BEGIN auto:self-check-protocol -->
> **Closed-book retrieval gate**
>
> 1. Close the chapter narrative and every hint disclosure.
> 2. Answer each outcome below from memory, aloud or in writing.
> 3. Support the answer with at least one observed field, measurement, or behavior
>    from your attempt record.
>
> If an answer needs notes, reopen only the section that owns the weak concept,
> correct your explanation, close it, and retry. Continue only when you can answer
> without looking.
<!-- END auto:self-check-protocol -->

You should be unable to defend the silence-timeout architecture for a serious
voice product, actively reaching for "is the microphone currently carrying
speech?", able to distinguish provider ingress from consumer dequeue, and able
to distinguish synthesized, accepted, and played audio. You should also be
able to identify the silence-timeout policy—not cleanup, cancellation, or
normal stream exhaustion—as this chapter's deliberate defect.

<!-- BEGIN auto:exercise-completion -->
---
Self-check complete? Prepare the cumulative spine, then replay it through this chapter:

```bash
uv sync --extra quickstart --group dev
uv run python docs/teaching/offline_spine.py --run --through 3 --jobs 4 --show-evidence
```

- [Review the chapter narrative](./README.md)
- [Continue to Chapter 4 — VAD + Pre-roll →](../04-vad-preroll/)
<!-- END auto:exercise-completion -->
