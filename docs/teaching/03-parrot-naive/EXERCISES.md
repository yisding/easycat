# Chapter 3 — Exercises

The whole chapter is one big exercise: feel a bad pipeline
viscerally so the next chapter's fix lands. The README walks you
through four sentences that break it; this file deepens that into
two tractable follow-ups.

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
- The first `stt.partial` *after* the parrot fired (the "Paris"
  the parrot ignored).

Compare `observed_silence_ms` with `configured_timeout_ms`. Why is
the former at least the latter rather than exactly equal? How large is
`post_fire_consumer_gap_ms`, and what work was the parrot loop doing
during that gap?

**Hints**

1. Every STT event resets `asyncio.wait_for`, including a final. The
   trigger is therefore the latest `stt.partial` or `stt.final`, not
   necessarily the latest partial.
2. `asyncio.wait_for(..., timeout=SILENCE_TIMEOUT_S)` resumes no
   earlier than the deadline. Event-loop scheduling adds the reported
   `scheduler_overshoot_ms`, so an exact equality is not a valid
   invariant.
3. The parrot awaits `speak()` before it consumes another queued STT
   event. `post_fire_consumer_gap_ms` therefore includes that blocked
   consumer time; it is not the provider-side arrival latency of the
   next partial.
4. The "Paris" partial consumed after the parrot's fire is evidence
   the user was *not done speaking*. Production
   pipelines (chapter 9) preserve that audio across barge-in;
   this naive one drops it on the floor.

## 3. Reject one synthesized chunk

**Task.** Run the provider-free output probe:

```bash
uv run python docs/teaching/03-parrot-naive/speak_acceptance_probe.py
```

Change the acceptance sequence to reject all three chunks, then accept all
three. Predict the counts each time. Which result proves that a speaker played
the audio?

**Hints**

1. `recipes.speak()` preserves every `send_audio()` acceptance result instead
   of treating a completed coroutine as delivery.
2. Rejection is actionable drop evidence. Acceptance means scheduled for
   delivery, not rendered by a device or heard by a person.

## Self-check

You should be unable to defend the silence-timeout architecture for a serious
voice product, actively reaching for "is the microphone currently carrying
speech?", and able to distinguish synthesized, accepted, and played audio.
