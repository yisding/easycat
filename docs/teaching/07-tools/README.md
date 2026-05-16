# Chapter 7 — Tools, Mid-stream

> The agent pauses to fetch something. The user hears silence — or
> hears "let me check that for you." That choice is the whole
> chapter.

## Prerequisites

- [Chapter 6](../06-streaming-agent/)
- `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`.

## Run it

```bash
uv run python docs/teaching/07-tools/main.py
```

Ask *"What's the weather in Tokyo?"* and then *"Set a 5-minute
timer."* Listen to the difference.

## A turn with a tool has three phases

```
  [user speech]──►[agent thinks]──►[TOOL runs]──►[agent resumes]──►[TTS]
                        │              │                │
                        ▼              ▼                ▼
                 maybe some      1.5 s silence     final answer
                 pre-tool text   (filler window)
```

Phase (b) — the 1.5 s while the tool runs — is the new problem.
Voice-UX research treats gaps longer than about 800 ms as
broken-feeling. A filler utterance doesn't reduce the latency; it
changes what the user hears during it.

## The filler heuristic

`should_play_filler(tool_name)` in `main.py`:

| Expected tool time | Decision | Why |
|---|---|---|
| < 300 ms | Don't bother | Filler would race the result |
| 300 ms – 2 s | Play a filler | The sweet spot |
| > 2 s | Filler + periodic update | "Still working…" at 2.5 s |

This is a UX decision, not a technical one. The teaching version
implements the middle band. The >2 s case is exercise 1.

## Inline tools vs. session actions

Two different things live in `easycat.session.actions`:

- **Inline tools** (what this chapter ships): run *during* the
  turn. The result is fed back to the LLM, which then speaks an
  answer informed by the tool output.
- **Session actions**: requested by the agent, run *after* the
  turn, and do *not* return data to the LLM. Five types ship:

| Action | What it does |
|---|---|
| `EndCallAction` | Terminates the call / session |
| `TransferCallAction` | Hands off to a human or another number |
| `SendDTMFAction` | Plays DTMF tones on a telephony leg |
| `SendSMSAction` | Sends a text-message side effect |
| `CustomAction` | Escape hatch — anything else |

A weather lookup is a tool. "Hang up" is not a tool — there is
nothing to return and nothing to resume. Put it in the action
queue.

> **Journaling gap.** `ToolCallStarted/Result` are journaled (we
> write them from this script). `SessionActionRequested/...` are
> emitted on the EventBus but not yet journaled by default. If you
> need to inspect action timelines, subscribe on the bus or wait
> for a planned journaling surface — don't assume records that
> don't exist.

## A common bug: speaking the tool result

Some agent stacks leak JSON back into the response stream. The
tool says `{"temp": 17, "sky": "cloudy"}` and the TTS literally
reads the braces and quote marks. Our `run_agent_streaming` routes
tool deltas to the journal *only* — they never go through
`sentence_queue`, which is the TTS pipe. If you hit a leak in your
own code, walk the stream until you find a `delta.tool_calls`
branch that accidentally accumulates into the same buffer as
`delta.content`.

## Read the journal

```python
from pathlib import Path
from easycat.debug.testing import load_bundle
b = load_bundle(next(Path("docs/teaching/07-tools/runs/").glob("*.bundle")))
for r in b.records():
    if r["name"] in ("tool.call.started", "tool.call.result"):
        print(r["name"], r["data"].get("name"), r["data"].get("elapsed_ms", ""))
    if r["name"] == "stage.tts.execute":
        print(f"  tts [{r['data']['kind']:>6}] {r['data']['text']}")
```

You will see `filler`-kind TTS spans interleaved with
`reply`-kind ones. That interleaving is the filler window made
visible.

## Try breaking it

1. Change `get_weather` to sleep 5 s. Listen — one filler is no
   longer enough. Add a "still working on it" at the 2.5 s mark.
2. Open `src/easycat/session/actions.py` and read the five
   action dataclasses. For each one, answer in one sentence:
   *why is this a session action and not a tool?* (The test is
   whether the LLM has anything useful to do with the return
   value.) The chapter ships no concrete action wiring because
   the executors live at the Session layer, which we don't have
   yet — but the reasoning is the payload.
3. Make a tool that returns a 5 KB JSON blob. Verify none of it
   reaches TTS. If it does, find the leak.

## What's next

[Chapter 8 — Smart-turn](../08-smart-turn/) returns to the
latency story: endpoint classification cuts the *user-finished-
speaking* gap the way streaming TTS cut the *agent-finished-
thinking* gap.
