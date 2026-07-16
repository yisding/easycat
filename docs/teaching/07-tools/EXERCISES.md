# Chapter 7 — Exercises

<!-- BEGIN auto:navigation -->
[← Back to chapter](./README.md) · [Ladder index](../) · [Progress worksheet](../PROGRESS.md) · [Chapter 8 — Smart-turn →](../08-smart-turn/)
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

## 1. Add a "still working on it" update for slow tools

**Task.** Change `get_weather` to sleep 5 seconds. Run. One filler
phrase is no longer enough — there are 3.5 quiet seconds after the
filler ends and before the answer arrives. Add a "still working on
it" filler at the 2.5-second mark.

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 3</summary>

The cleanest place to add this is inside the tool-call branch
   of `run_agent_streaming`, after enqueueing the first filler. A
   `asyncio.create_task` that sleeps then enqueues a second
   filler works; cancel it once the tool returns.

</details>

<details markdown="1">
<summary>Hint 2 of 3</summary>

The journal already records `tool.call.started` and
   `tool.call.result`. Use the gap between them as the timing
   reference.

</details>

<details markdown="1">
<summary>Hint 3 of 3</summary>

Voice-UX research treats a single filler as enough up to ~2 s.
   Past that, periodic updates ("still checking", "almost there")
   are the right pattern. Avoid the temptation to *narrate* —
   don't say "I'm still calling the weather API."

</details>
<!-- END auto:exercise-hints -->

## 2. Why is each session action *not* a tool?

**Task.** Run the provider-free catalog, then open
`src/easycat/session/actions.py`:

```bash
uv run python docs/teaching/07-tools/action_catalog.py
```

The output is discovered from the concrete action classes currently
bound in the runtime module rather than a hand-maintained count. For
each of the seven action dataclasses, answer in one sentence: *why is
this a deferred session action rather than a tool whose result must
shape the current response?*

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 5</summary>

The test is whether the current response **depends on the result**.
   If yes, use an inline tool so the result informs the next token. If
   the operation is a deferred session/transport/compliance side effect,
   enqueue an action after the turn and observe its lifecycle separately.

</details>

<details markdown="1">
<summary>Hint 2 of 5</summary>

`EndCallAction` — there is nothing after the call ends. The
   LLM doesn't need to know "I successfully hung up."

</details>

<details markdown="1">
<summary>Hint 3 of 5</summary>

`TransferCallAction`, `SendDTMFAction`, and `SendSMSAction` are
   transport side effects performed after the spoken turn. Their success
   or failure is journaled; it does not shape the already-produced reply.

</details>

<details markdown="1">
<summary>Hint 4 of 5</summary>

`AddToDNCAction` / `RemoveFromDNCAction` mutate the compliance
   store through `CoreSessionActionExecutor`. The user-facing
   acknowledgement comes first; the auditable store write follows.

</details>

<details markdown="1">
<summary>Hint 5 of 5</summary>

`CustomAction` is an escape hatch, not an automatic classification.
   The discipline is: if you'd be tempted to feed the result back to
   the LLM, make it a tool instead.

</details>
<!-- END auto:exercise-hints -->

## 3. Plug a JSON-leak

**Task.** Make a tool that returns a 5 KB JSON blob (mock weather
forecast). Verify none of it reaches TTS.

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 3</summary>

The chapter's `run_agent_streaming` already routes tool deltas
   away from `sentence_queue` — `delta.tool_calls` accumulates
   into a separate buffer (`tool_calls`), `delta.content` goes to
   the sentence splitter. Confirm this in the code.

</details>

<details markdown="1">
<summary>Hint 2 of 3</summary>

If a leak happened in your own code, the symptom would be TTS
   reading `{ "temperature": 17 }` aloud — curly braces and all.
   Walk the stream until you find a branch that's accumulating
   tool deltas into the same buffer as content.

</details>

<details markdown="1">
<summary>Hint 3 of 3</summary>

The structural defense is `MarkdownStripProcessor` (and friends
   in chapter 14's output-processor stack) — but the *real*
   defense is keeping the streams separate at parse time. By the
   time it's reached TTS it's already too late.

</details>
<!-- END auto:exercise-hints -->

## 4. Reject the filler, not the reply

**Task.** Run the provider-free filler attribution probe:

```bash
uv run python docs/teaching/07-tools/filler_delivery_probe.py
```

Then change the slow case's transport decisions from `[False, True]`
to `[True, False]`. Predict the first-audio kind and the two TTS
records before re-running it.

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 4</summary>

`filler_enqueued` stays true in both runs. It records the queueing
   policy, not transport acceptance.

</details>

<details markdown="1">
<summary>Hint 2 of 4</summary>

With `[False, True]`, the filler record proves one rejected chunk;
   the first accepted reply chunk owns `tts.first_audio`.

</details>

<details markdown="1">
<summary>Hint 3 of 4</summary>

With `[True, False]`, the filler is scheduled for delivery and owns
   `tts.first_audio`, while the final reply is rejected. Neither case
   proves what the speaker rendered or the user heard.

</details>

<details markdown="1">
<summary>Hint 4 of 4</summary>

Follow `tool_call_id` from `tool.call.started` to
   `tool.call.result` and the filler-kind `stage.tts.execute` record.
   Reply-kind TTS records intentionally have no tool-call ID.

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
>
> If an answer needs notes, reopen only the section that owns the weak concept,
> correct your explanation, close it, and retry. Continue only when you can answer
> without looking.
<!-- END auto:self-check-protocol -->

1. At which observed response times would a filler help or hurt, and why?
2. What is the tool-versus-session-action distinction, and which lifecycle
   records demonstrate it?
3. Which fields prove that filler was requested and that its audio was
   accepted, and why does neither prove it was heard?

<!-- BEGIN auto:exercise-completion -->
---
Self-check complete? Prepare the cumulative spine, then replay it through this chapter:

```bash
uv sync --extra quickstart --group dev
uv run python docs/teaching/offline_spine.py --run --through 7 --jobs 4 --show-evidence
```

- [Review the chapter narrative](./README.md)
- [Update the progress worksheet](../PROGRESS.md)
- [Continue to Chapter 8 — Smart-turn →](../08-smart-turn/)
<!-- END auto:exercise-completion -->
