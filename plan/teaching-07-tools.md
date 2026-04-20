# Chapter 7 — Tools, mid-stream

> The agent doesn't just talk. It calls a tool, waits for the
> result, and keeps talking. This raises questions the chat-only
> world doesn't have.

## Prerequisites

- Chapter 6 (streaming agent + sentence-boundary TTS)
- An LLM that supports tool/function calling (OpenAI, Anthropic,
  Google all do via the providers EasyCat ships)

## Learning objectives

1. Wire a single tool into the streaming agent and call it from a
   real conversation turn.
2. Reason about the **filler-utterance question**: when the agent
   pauses to call a tool, do you stay silent or say "let me check
   that for you"?
3. Distinguish between **inline tools** (run during the turn,
   result fed back to the LLM) and **session actions** (run *after*
   the turn — `EndCallAction`, `TransferCallAction`, `SendDTMFAction`,
   `SendSMSAction` in `easycat.session.actions`).
4. Read the journal to understand the timeline of a tool-bearing
   turn: agent → tool start → tool result → agent (resumed) → TTS.

## What you build

`docs/teaching/07-tools/main.py`:

- Starts from a copy of `docs/teaching/06-streaming-agent/main.py`.
- Adds **two** demo tools to the agent:
  - `get_weather(city: str) -> str` — a slow (~1.5s) async tool
    that returns a fake weather string. The latency is on purpose.
  - `set_timer(minutes: int) -> str` — a fast (~50ms) tool that
    schedules a timer (mock).
- Adds a small **filler manager** that decides whether to play a
  filler utterance ("let me check that for you") based on the
  expected tool latency.
- Same pipeline as ch 6 for everything else; bundles land in
  `docs/teaching/07-tools/runs/`.

## Narrative arc

1. **Why this isn't ch 6 plus a function.** A tool call splits one
   conversational turn into three phases: (a) agent speaks
   pre-tool text, (b) tool runs while agent is paused, (c) agent
   resumes with tool output. Each phase has its own user
   perception.
2. **The 1.5s problem.** Run `get_weather` and listen. There's a
   void between the agent's last word and the tool result. Voice
   UX research treats >800ms gaps as broken-feeling. Fillers fix
   the perception, not the latency.
3. **Filler decision.** Walk through the heuristic:
   - Tool expected to take <300ms? Don't bother — by the time TTS
     starts a filler, the result is already back.
   - 300ms-2s? Filler is high-leverage. "One moment", "let me
     check that", "looking that up for you."
   - >2s? Filler + periodic update. "Still working on it…"
   The reader implements the simple version (300ms-2s window).
4. **Session actions are different.** Walk through
   `easycat.session.actions`. `EndCallAction` doesn't return data
   to the agent; it terminates the call. `TransferCallAction`
   hands off to a human. These are not tools — they are
   side-effects the agent requests *after* its turn. Show the
   action queue (`SessionActions`) and the executor surface.
5. **Streaming events for tools.** Walk through
   `AgentStreamEventType.TOOL_CALL_STARTED`,
   `TOOL_CALL_DELTA`, `TOOL_CALL_RESULT` in
   `src/easycat/integrations/agents/_legacy_types.py`. Show how
   `consume_agent_stream` (chapter 6's reference reading) handles
   each.
6. **A common bug: speaking the tool result text.** Some agents
   leak the JSON back into the response stream. Demo it.
   Filter rule: tool deltas go to the journal, not to TTS.

## Key concepts

- `easycat.events.ToolCallStarted` / `ToolCallDelta` /
  `ToolCallResult`
- `easycat.integrations.agents._legacy_types.AgentStreamEventType`
  — the `TOOL_*` branch
- `easycat.session.actions` — `SessionAction`, `EndCallAction`,
  `TransferCallAction`, `SendDTMFAction`, `SendSMSAction`,
  `SessionActions`
- The filler-vs-silence decision as a UX choice, not a technical
  one
- Tool latency budget: the user only tolerates so much silence

## Exercises

1. Make `get_weather` take 5 seconds. What does the filler do? Add
   a periodic "still working on it" at the 2.5s mark.
2. Wire the agent to call `EndCallAction` when the user says
   "goodbye." What changes about the journal? Why does the audio
   stop differently than a regular turn end?
3. Add a tool that returns a 5KB JSON blob. Verify the JSON does
   not get spoken. If it does, find where the leak is in your code.

## Journal highlights

- `agent.tool_call.started` records with the tool name and args
- A measurable gap between `tool_call.started` and
  `tool_call.result` — this is the "filler window"
- `agent.tool_call.result` records (data redacted by default in
  the journal — read `peripheral-redaction.md` for the full story)
- For `SessionAction` flows: `session.action.requested` →
  `session.action.started` → `session.action.completed` /
  `session.action.failed`
- Filler-utterance TTS spans, distinguishable from the main
  response by a tag on the journal record

## Files created

- `docs/teaching/07-tools/main.py` (~150 lines: tools + filler
  manager + same agent stream consumer as ch 6)
- `docs/teaching/07-tools/README.md`

## Success criteria

- The reader has heard the difference between "tool with filler"
  and "tool without filler" on the same prompt and can defend the
  choice for each.
- The reader can name the four `SessionAction` types and explain
  why each one is a session action rather than a tool.
- The reader has caught and fixed at least one "tool result leaks
  to TTS" bug — either induced by an exercise or in their own
  implementation.

## Links forward

Chapter 8 returns to the latency story: smart-turn detection cuts
the *user-finished-speaking* gap the way streaming TTS cut the
agent-finished-thinking gap.
