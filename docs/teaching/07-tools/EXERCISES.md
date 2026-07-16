# Chapter 7 — Exercises

<!-- BEGIN auto:navigation -->
[← Back to chapter](./README.md) · [Ladder index](../) · [Chapter 8 — Smart-turn →](../08-smart-turn/)
<!-- END auto:navigation -->

## 1. Add a "still working on it" update for slow tools

**Task.** Change `get_weather` to sleep 5 seconds. Run. One filler
phrase is no longer enough — there are 3.5 quiet seconds after the
filler ends and before the answer arrives. Add a "still working on
it" filler at the 2.5-second mark.

**Hints**

1. The cleanest place to add this is inside the tool-call branch
   of `run_agent_streaming`, after enqueueing the first filler. A
   `asyncio.create_task` that sleeps then enqueues a second
   filler works; cancel it once the tool returns.
2. The journal already records `tool.call.started` and
   `tool.call.result`. Use the gap between them as the timing
   reference.
3. Voice-UX research treats a single filler as enough up to ~2 s.
   Past that, periodic updates ("still checking", "almost there")
   are the right pattern. Avoid the temptation to *narrate* —
   don't say "I'm still calling the weather API."

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

**Hints**

1. The test is whether the current response **depends on the result**.
   If yes, use an inline tool so the result informs the next token. If
   the operation is a deferred session/transport/compliance side effect,
   enqueue an action after the turn and observe its lifecycle separately.
2. `EndCallAction` — there is nothing after the call ends. The
   LLM doesn't need to know "I successfully hung up."
3. `TransferCallAction`, `SendDTMFAction`, and `SendSMSAction` are
   transport side effects performed after the spoken turn. Their success
   or failure is journaled; it does not shape the already-produced reply.
4. `AddToDNCAction` / `RemoveFromDNCAction` mutate the compliance
   store through `CoreSessionActionExecutor`. The user-facing
   acknowledgement comes first; the auditable store write follows.
5. `CustomAction` is an escape hatch, not an automatic classification.
   The discipline is: if you'd be tempted to feed the result back to
   the LLM, make it a tool instead.

## 3. Plug a JSON-leak

**Task.** Make a tool that returns a 5 KB JSON blob (mock weather
forecast). Verify none of it reaches TTS.

**Hints**

1. The chapter's `run_agent_streaming` already routes tool deltas
   away from `sentence_queue` — `delta.tool_calls` accumulates
   into a separate buffer (`tool_calls`), `delta.content` goes to
   the sentence splitter. Confirm this in the code.
2. If a leak happened in your own code, the symptom would be TTS
   reading `{ "temperature": 17 }` aloud — curly braces and all.
   Walk the stream until you find a branch that's accumulating
   tool deltas into the same buffer as content.
3. The structural defense is `MarkdownStripProcessor` (and friends
   in chapter 14's output-processor stack) — but the *real*
   defense is keeping the streams separate at parse time. By the
   time it's reached TTS it's already too late.

## 4. Reject the filler, not the reply

**Task.** Run the provider-free filler attribution probe:

```bash
uv run python docs/teaching/07-tools/filler_delivery_probe.py
```

Then change the slow case's transport decisions from `[False, True]`
to `[True, False]`. Predict the first-audio kind and the two TTS
records before re-running it.

**Hints**

1. `filler_enqueued` stays true in both runs. It records the queueing
   policy, not transport acceptance.
2. With `[False, True]`, the filler record proves one rejected chunk;
   the first accepted reply chunk owns `tts.first_audio`.
3. With `[True, False]`, the filler is scheduled for delivery and owns
   `tts.first_audio`, while the final reply is rejected. Neither case
   proves what the speaker rendered or the user heard.
4. Follow `tool_call_id` from `tool.call.started` to
   `tool.call.result` and the filler-kind `stage.tts.execute` record.
   Reply-kind TTS records intentionally have no tool-call ID.

## Self-check

You should be able to look at a voice agent's response time and
predict where filler utterances would help vs hurt, and explain
the *tool vs session action* distinction in one sentence. You should
also be able to prove whether a filler was requested and whether its
audio was accepted without conflating either fact with “heard.”

<!-- BEGIN auto:exercise-completion -->
---
Self-check complete? Replay the hardware-free spine through this chapter:

```bash
uv run python docs/teaching/offline_spine.py --run --through 7 --jobs 4
```

- [Review the chapter narrative](./README.md)
- [Continue to Chapter 8 — Smart-turn →](../08-smart-turn/)
<!-- END auto:exercise-completion -->
