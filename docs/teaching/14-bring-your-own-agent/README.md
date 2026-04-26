# Chapter 14 — Bring your own agent

> Chapter 13's `build_agent()` returned an `agents.Agent(...)` from
> the OpenAI Agents SDK. `create_session` silently wrapped it in an
> `OpenAIAgentsBridge`. In this chapter we drop the framework
> entirely and plug in a plain async class — same Session code,
> different brain.

## Prerequisites

- [Chapter 13.](../13-swap-providers-and-transports/)
- `uv sync --extra quickstart --group dev`.
- `OPENAI_API_KEY`.

## Run

```bash
uv run python docs/teaching/14-bring-your-own-agent/main.py
```

Talk to it. Say **"goodbye"** to watch the session-action flow fire
— the workflow enqueues `EndCallAction`, `CoreSessionActionExecutor`
dispatches it, the session stops after the current turn.

## The bridge layer you didn't know was there

```
    user code ──▶ EasyConfig(agent=...)
                         │
                         ▼
               auto_adapt_agent()
                         │
             ┌───────────┼──────────────────────────┐
             ▼           ▼                          ▼
     OpenAIAgentsBridge  PydanticAIBridge   GenericWorkflowBridge
             │           │                          │
             └───────────┴──────────┬───────────────┘
                                    ▼
                          BridgeAdapterShim
                                    │
                                    ▼
                              Session.run()
```

Every `agent=` value the config accepts is routed through
`auto_adapt_agent()`, which picks the right concrete bridge and
wraps it in `BridgeAdapterShim`. The shim is the thing `Session`
actually calls `run_streaming()` on. So the "Session orchestration"
in chapters 2-13 has always been framework-agnostic; bridges are
the seam.

## The three things ch 14's script shows

### 1. `GenericWorkflowBridge` in deep mode

Deep mode is opt-in via signature: name `recorder` as a parameter
on `on_user_turn` and the bridge runs you in deep mode.

```python
class MyWorkflow:
    async def on_user_turn(self, text, *, recorder, cancel_token):
        stream = await client.chat.completions.create(..., stream=True)
        async for chunk in stream:
            if cancel_token.is_cancelled:
                break
            yield chunk.choices[0].delta.content or ""
```

Deep mode matters because it is the only way mid-turn barge-in
Just Works. Shallow mode (`on_user_turn(text) -> str`) has no
visibility into the workflow's internals, so when the user
interrupts, the bridge can only apply end-of-turn cancellation —
the current turn runs to completion before the next user turn
begins. When this happens the runtime writes a `ControlSignalRecord`
with `cause="shallow_mode_downgrade"` so you know why the bot
didn't stop.

### 2. Session actions

Tools inside your agent can't reach the live `Session` — they
live inside the framework's own event loop. Instead, they enqueue
typed actions on a shared `SessionActions` queue. The session
drains the queue after the turn, dispatching each action to the
first executor that claims it via `supports()`.

```python
actions.enqueue(EndCallAction(reason="user requested hang-up"))
```

The five action types:

| Action | Typical executor |
|---|---|
| `EndCallAction` | `CoreSessionActionExecutor` (stops the session) |
| `TransferCallAction` | `TwilioSessionActionExecutor` (REST dial) |
| `SendDTMFAction` | `TwilioSessionActionExecutor` (IVR) |
| `SendSMSAction` | `TwilioSessionActionExecutor` |
| `CustomAction` | whatever you write |

The Twilio executor lives in `src/easycat/telephony/session_actions.py`
and needs `call_sid` off the transport — it's only useful on the
Twilio transport. The core executor is provider-neutral and handles
`EndCallAction` alone.

### 3. Output processors (the pronunciation pipeline)

> **Name note.** The source module is `llm_output_processing.py`
> and the stack is called *output processors* — we call it the
> *pronunciation pipeline* because phonetic replacement and pauses
> are what the feature buys you for voice. Grep for
> `LLMOutputProcessor` / `output_processors`, not "pronunciation."

Every committed assistant utterance runs through
`config.output_processors` before reaching TTS. Four first-class
processors live in `src/easycat/llm_output_processing.py`:

| Processor | Purpose |
|---|---|
| `MarkdownStripProcessor` | Strip `**bold**` / lists / code spans for voice |
| `PhoneticReplacementProcessor` | Case-insensitive whole-word swap |
| `PauseProcessor` | Regex-match → insert SSML `<break>` between matched units |
| `LLMOutputProcessor` | Protocol — roll your own |

Processors run serially, fail-open: an exception in one is logged
and the next one still runs. The Session applies the full chain to
the **TTS payload**. Only `strip_markdown` is also written back to
the bridge's chat history (via `replace_last_assistant_text`), so
phonetic replacements and pauses shape what the user *hears* but
the LLM still sees the original text next turn.

`default_pronunciation_processors(...)` is a factory that wires the
common stack (phonetic swaps + phone-number pauses) if you don't
want to hand-build the list.

## MCP (a short sidebar)

MCP — Model Context Protocol — servers are first-class:

```python
EasyConfig(
    agent=my_agent,
    mcp_servers=["stdio://path/to/mcp-server", "sse://localhost:4000"],
)
```

The validator accepts `stdio://`, `sse://`, `http://`, `https://`.
`BridgeAdapterShim` forwards the list into `RecorderContext`, and
each bridge injects it into its framework's agent object
(`agent.mcp_servers = [...]` before `run_streamed()`). Shallow-mode
`GenericWorkflowBridge` logs a warning because it has no way to
wire MCP into your hand-rolled workflow — deep mode makes it your
responsibility.

## Try breaking it

1. Change `on_user_turn` to `async def on_user_turn(self, text)`
   — drop `recorder` / `cancel_token`. You just demoted to shallow
   mode. Run the script and try to interrupt the bot mid-sentence.
   What do you see in the journal? (Hint: grep for
   `shallow_mode_downgrade`.)
2. Add a `CustomAction` and a 10-line executor that prints it.
   Trigger it from the workflow. How does the journal record the
   action's lifecycle?
3. Register the `default_pronunciation_processors()` stack and say
   "Call me at 555-867-5309." Listen for the pause. Now drop the
   `PauseProcessor` and say it again. How does the stress pattern
   change?

## What's next

[Chapter 15 — Operate in production](../15-operate-in-production/)
takes the single-session demo you've been running since chapter 0
and shows what it takes to run N of them at once: `SessionManager`,
the lifecycle methods, the debugger UI, and the CLI.
