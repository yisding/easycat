# Chapter 9 — Exercises

<!-- BEGIN auto:navigation -->
[← Back to chapter](./README.md) · [Ladder index](../) · [Progress worksheet](../PROGRESS.md) · [Chapter 10 — Cleaning the Signal →](../10-cleaning-signal/)
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

## 1. Probe the over-/under-shoot of `heard_text`

**Task.** Run `estimate.py`. Interrupt the bot as close as you can
after hearing one word; repeat several times because a human reaction
is not an exact clock. Open each bundle — does `heard_text` end at that
word, or does it over- or under-shoot? Then inspect the production
transport capabilities without opening audio devices:

```bash
uv run python docs/teaching/09-interruption/playback_evidence.py
```

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 5</summary>

The toy estimator multiplies `bytes_accepted` by ~15 chars / 48000
   bytes / second (24 kHz × 2 bytes/sample). The constant is an
   *average* — a fast word like "yes" lasts ~150 ms but the formula
   assigns it ~500 ms; a slow word like "elephant" lasts ~600 ms
   and gets the same ~500 ms.

</details>

<details markdown="1">
<summary>Hint 2 of 5</summary>

The playback queue also lies: `transport.send_audio=True`
   means accepted, not heard. `clear_audio()` drops queued chunks,
   so `bytes_accepted` *overcounts* by that unplayed backlog. A
   `False` return is different: that chunk was rejected and the
   corrected toy does not count it at all.

</details>

<details markdown="1">
<summary>Hint 3 of 5</summary>

Net effect: `heard_text` *usually* overshoots by 0-2 words. On
   a *slow* word at the start of a sentence it can undershoot.

</details>

<details markdown="1">
<summary>Hint 4 of 5</summary>

Production uses the strongest progress evidence each transport
   exposes. `LocalTransport` and WebRTC emit `TransportAudioDelivered`
   when their output callback/track consumes a chunk. Twilio sends
   playback marks and emits `PlaybackMarkAck` when Twilio acknowledges
   reaching them. Other transports fall back to a serial-playout timing
   estimate from the send log.

</details>

<details markdown="1">
<summary>Hint 5 of 5</summary>

These signals are stronger than `send_audio=True`, but none is
   literal ground truth at the human ear: device, network, and acoustic
   delays can remain after the transport milestone.

</details>
<!-- END auto:exercise-hints -->

## 2. Make markdown break the estimator

**Task.** Have the agent reply with markdown-heavy output (ask it
for a table or a bulleted list). The text fed to TTS is
`strip_markdown(text)` — shorter than the original. How does this
affect `heard_text` vs reality?

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 3</summary>

`sentences_sent` records the *stripped* text (what TTS actually
   spoke). `bytes_accepted` is accepted bytes of the stripped audio.
   So `bytes_accepted → heard_chars` is internally consistent *on
   the stripped text*, before the playback-queue correction.

</details>

<details markdown="1">
<summary>Hint 2 of 3</summary>

The bug arises when you append `heard_text` back into the
   conversation history — should it be the stripped version (what
   the user heard) or the original (what the LLM produced)? The
   toy uses stripped, which is correct for the *next turn's
   prompt* but loses the markdown structure.

</details>

<details markdown="1">
<summary>Hint 3 of 3</summary>

Production `interruption.py` keeps both: stripped for the user
   model, original for any tool that wants the structured text.

</details>
<!-- END auto:exercise-hints -->

## 3. Why does AEC fix self-interruption?

**Task.** Run `estimate.py` on speakerphone (no headphones). The
bot interrupts itself. Why does AEC fix this, and why is VAD alone
not enough?

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 4</summary>

VAD's job is "is this frame speech?" — it can't distinguish
   the user's speech from the bot's speech radiated back through
   the speaker. From VAD's perspective, both are equally
   "speech."

</details>

<details markdown="1">
<summary>Hint 2 of 4</summary>

AEC takes the TTS audio we sent to the speaker as a
   *reference*, and subtracts the echo path's filtered version of
   that reference from the mic. The result is a mic signal that
   no longer contains the bot's voice — only the user's.

</details>

<details markdown="1">
<summary>Hint 3 of 4</summary>

AEC is *dual-input* (mic + reference); VAD is *single-input*
   (mic only). No amount of better VAD will fix the loop, because
   the information VAD needs isn't in its input.

</details>

<details markdown="1">
<summary>Hint 4 of 4</summary>

This is the preview of chapter 10.

</details>
<!-- END auto:exercise-hints -->

## 4. Trace the turn that triggers barge-in

**Task.** Run the provider-free continuity probe:

```bash
uv run python docs/teaching/09-interruption/barge_in_turn_probe.py
```

Explain why the triggering `speech_started` event must not be consumed
by the cancellation branch, and why the mic frames are still available
after the coordinator waits for the old bot task.

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 4</summary>

`route_barge_in` returns `event_consumed: false`. The coordinator
   therefore falls through to the same STT-start branch used by an
   ordinary user turn.

</details>

<details markdown="1">
<summary>Hint 2 of 4</summary>

`mic_producer` is a separate coroutine. It keeps adding pre-roll and
   live frames to `mic_queue` while cooperative bot shutdown finishes.

</details>

<details markdown="1">
<summary>Hint 3 of 4</summary>

Change the probe's fallthrough condition to act as if the event were
   consumed. The STT lifecycle disappears, which is the old behavior:
   the bot stops, but the user must repeat the interruption.

</details>

<details markdown="1">
<summary>Hint 4 of 4</summary>

On process shutdown, the coordinator must stop both possible
   in-flight owners—STT and the bot task—before the shared TTS/client/VAD
   stack closes.

</details>
<!-- END auto:exercise-hints -->

## 5. Separate cancel control from audible silence

**Task.** Run the deterministic cancellation-latency probe:

```bash
uv run python docs/teaching/09-interruption/cancel_latency_probe.py
```

Then change the scripted bot return from 80 ms to 150 ms without
changing the transport's 30 ms return. Predict the completion record
before re-running it.

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 4</summary>

`cancel_to_clear_audio_return_ms` should remain 30 ms. It measures
   when the transport's queue-clear call returns.

</details>

<details markdown="1">
<summary>Hint 2 of 4</summary>

`cancel_to_bot_task_return_ms` should become 150 ms. Cooperative
   cancellation is not complete until the old task exits and its result
   is observed.

</details>

<details markdown="1">
<summary>Hint 3 of 4</summary>

The triggering `speech_started` still has `event_consumed: false`;
   cancellation latency must not discard the next user turn.

</details>

<details markdown="1">
<summary>Hint 4 of 4</summary>

A fast `clear_audio()` return does not prove acoustic silence at the
   human ear. It is a software control milestone, not playback or
   perception evidence.

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

1. What changes between interruption versions A, B, and C, and which event
   order distinguishes them?
2. Why do bytes accepted not equal bytes heard, and how does rejected audio
   differ from accepted-but-queued audio in the records?
3. Why is `CancelToken` shared state rather than an exception?
4. How does the triggering `speech_started` event survive cleanup and enter the
   next STT turn?
5. Which milestones distinguish clear-audio return, bot-task return,
   transport playback evidence, and actual human hearing?

<!-- BEGIN auto:exercise-completion -->
---
Self-check complete? Prepare the cumulative spine, then replay it through this chapter:

```bash
uv sync --extra quickstart --group dev
uv run python docs/teaching/offline_spine.py --run --through 9 --jobs 4 --show-evidence
```

- [Review the chapter narrative](./README.md)
- [Update the progress worksheet](../PROGRESS.md)
- [Complete the Build phase review](../PROGRESS.md#build-phase-review)
- [Continue to Chapter 10 — Cleaning the Signal →](../10-cleaning-signal/)
<!-- END auto:exercise-completion -->
