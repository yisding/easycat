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
Open the bundle. Find:

- The last `stt.partial` before the parrot fired.
- The `parrot.fire` record itself.
- The first `stt.partial` *after* the parrot fired (the "Paris"
  the parrot ignored).

How far apart in time are records 1 and 2? (It should be exactly
your `SILENCE_TIMEOUT_S`.) How far apart are 2 and 3? (This is
the latency the parrot *added* by firing early.)

**Hints**

1. Filter records by `name` starting with `"stt."` or `"parrot."`.
2. `offset_ms` on each record is monotonic — subtracting them
   gives real elapsed time.
3. The "Paris" partial that landed after the parrot's fire is
   evidence the user was *not done speaking*. Production
   pipelines (chapter 9) preserve that audio across barge-in;
   this naive one drops it on the floor.

## Self-check

You should be unable to defend the silence-timeout architecture
for a serious voice product, and you should be actively reaching
for "is the microphone currently carrying speech?" — which is
exactly what chapter 4 hands you.
