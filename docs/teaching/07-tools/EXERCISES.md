# Chapter 7 — Exercises

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

**Task.** Open `src/easycat/session/actions.py` and look at the
five action dataclasses (`EndCallAction`, `TransferCallAction`,
`SendDTMFAction`, `SendSMSAction`, `CustomAction`). For each one,
answer in one sentence: *why is this a session action and not a
tool?*

**Hints**

1. The test is **whether the LLM has anything useful to do with
   the return value.** If yes → tool (the result informs the next
   token). If no → session action (queue it, run it after the
   turn, don't feed anything back).
2. `EndCallAction` — there is nothing after the call ends. The
   LLM doesn't need to know "I successfully hung up."
3. `TransferCallAction` / `SendDTMFAction` — the call leg is gone
   or being modified; the LLM should not be generating more text.
4. `SendSMSAction` — fire-and-forget; the agent's response is
   what the user *hears*, the SMS is the side effect.
5. `CustomAction` — escape hatch. The discipline is "if you'd be
   tempted to feed the result back to the LLM, make it a tool
   instead."

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

## Self-check

You should be able to look at a voice agent's response time and
predict where filler utterances would help vs hurt, and explain
the *tool vs session action* distinction in one sentence without
opening the file.
