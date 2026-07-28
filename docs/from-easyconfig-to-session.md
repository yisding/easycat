# From EasyConfig to Session

The quickstart front door is one call: `run(EasyConfig.mic(agent=...))`.
This guide is the graduation path from that one-liner to the production
`Session` API — the same object `run()` builds internally — so you can
subscribe to events, drive turns from your own code, queue session
actions from agent tools, and capture debug bundles you can replay.

Each section below adds one capability. Stop at the first rung that
covers your app; the later rungs exist for narrower needs.

## 1. Where you start: the quickstart

```python
from agents import Agent

from easycat import EasyConfig, run

run(
    EasyConfig.mic(
        agent=Agent(name="assistant", instructions="You are a helpful voice assistant.")
    )
)
```

`run(...)` hides the asyncio entry point, signal handling, and teardown
ceremony. `EasyConfig` auto-wires OpenAI STT/TTS from `OPENAI_API_KEY`.
Graduate past this rung only when you need the session object itself.

## 2. Get the session object: `create_session` and the lifecycle

`create_session(...)` builds the same fully wired `Session` that `run()`
uses internally. Create the session first when you need it before
startup — for event subscriptions, a debugger UI, or app-specific hooks —
and hand it to `run_session(...)` to keep the same signal handling and
teardown path:

```python
from agents import Agent

from easycat import EasyConfig, STTFinal, create_session
from easycat.helpers import run_session

agent = Agent(name="Support", instructions="Help customers with account issues.")


def main() -> None:
    session = create_session(EasyConfig.mic(agent=agent))
    subscription = session.subscribe_event(STTFinal, lambda e: print("You said:", e.text))
    try:
        run_session(session)
    finally:
        subscription.unsubscribe()


main()
```

If you already own an event loop, use the one public teardown idiom
directly — `async with session:` starts the session on entry and calls
`stop(force=True)` on exit:

```python
async def serve() -> None:
    session = create_session(EasyConfig.mic(agent=agent))
    async with session:
        await session.wait_closed()
```

Outside the context manager, `await session.stop()` is the single public
teardown verb: the default drains in-flight work gracefully, and
`stop(force=True)` cancels it first. After a clean stop, the journal and
debug-bundle export below still work through the preserved read-only
postmortem view.

## 3. Subscribe to events

`session.subscribe_event(event_type, handler)` attaches a sync or async
handler on the session's `EventBus` and returns a subscription you can
`unsubscribe()`. The events you will reach for first:

```python
from easycat import AgentFinal, Interruption, STTFinal, TurnEnded, TurnStarted

session.subscribe_event(STTFinal, lambda e: print("user:", e.text))
session.subscribe_event(AgentFinal, lambda e: print("bot:", e.text))
session.subscribe_event(TurnStarted, on_turn_started)  # sync or async handler
session.subscribe_event(Interruption, on_barge_in)
```

For agent and tool-call streams there is a one-call helper that registers
`AgentDelta` / `AgentFinal` / tool-call handlers together:

```python
registrations = session.subscribe_agent_events(
    on_delta=lambda e: print(e.text, end="", flush=True),
    on_final=lambda e: print(),
)
```

Handlers run on the session's event loop, so keep them fast; offload
slow work with `asyncio.create_task`.

## 4. Drive turns yourself: `send_text` and session actions

For request/response agent interaction without the audio pipeline —
tests, evals, text UIs — create a text session. It runs the same agent
bridge stack, and `session.send_text(...)` returns the agent's reply:

```python
from easycat import create_text_session

session = create_text_session(agent=agent, debug="full")
async with session:
    reply = await session.send_text("What are your hours?")
```

`send_text()` is only available on sessions built with
`create_text_session(...)`; voice sessions take their input from the
transport.

Agent tools cannot touch the live session directly. Instead they enqueue
typed actions on a shared `SessionActions` queue, which the session
drains after the current turn completes:

```python
from easycat import EasyConfig, SessionActions, run

actions = SessionActions()
# Inside a tool: actions.end_call(reason="user said goodbye")
# Also available: transfer_call(), send_dtmf(), send_sms(), request().

run(EasyConfig.mic(agent=agent, session_actions=actions))
```

See `examples/session_actions_openai.py` for the full tool wiring,
including how the same `actions` object reaches the tool via the agent
context.

## 5. Debug it: `debug="full"`, bundles, and replay

Pass `debug="full"` on `EasyConfig` (or `create_text_session`) and every
stage writes to an execution journal — the single source of truth for
observability. Add `record_to="runs"` to auto-export a timestamped debug
bundle at teardown, or export one yourself at any point:

```python
session = create_session(EasyConfig.mic(agent=agent, debug="full", record_to="runs"))
# ... after the run, or any time during it:
session.export_debug_bundle("support-call.zip")
```

Then inspect and replay from the CLI:

```bash
uv run easycat replay PATH
uv run easycat replay PATH --json
uv run easycat inspect .easycat/journals/<session_id>.sqlite
```

Replace `PATH` with the exported bundle (or a journal path), and
`<session_id>` with the id printed at session start. The
[observability guide](observability.md) covers the debugger UI, metrics,
and traces on top of the same journal.

## When you need the explicit `SessionConfig` escape hatch

Everything above used `EasyConfig`, whose fields take provider
*descriptors* — shortcut strings like `stt="deepgram/flux"` or config
dataclasses — that `create_session(...)` resolves and wires for you. The
bottom rung swaps descriptors for live provider *instances*. Reach for it
when `EasyConfig` cannot express your wiring: you construct providers
yourself (custom credentials flow, a provider EasyCat does not ship, a
test double), or you need raw pipeline fields like a shared `EventBus`,
a custom `TurnManager`, your own journal, or an injected outbound audio
queue. If you only need to hand-build the providers, use
`Session.from_providers(...)`; spell out `SessionConfig` only when you
need every raw field:

```python
from easycat import Session, SessionConfig

session = Session(
    SessionConfig(
        stt=my_stt,  # live provider instances, not descriptors
        tts=my_tts,
        vad=my_vad,
        transport=my_transport,
        agent=my_agent,
        event_bus=shared_bus,
    )
)
```

The lifecycle, event, and debug surfaces from sections 2-5 are identical
from here — `SessionConfig` only changes who constructs the providers.

## Where to go next

- [Teaching chapter 13](teaching/13-swap-providers-and-transports/)
  runs the same `create_session()` wiring across the provider and
  transport matrix.
- The [examples matrix](../examples/README.md) has runnable apps for
  every transport, including event-subscription and session-action
  examples.
- The [public API contract](public-api.md) lists the stable imports used
  on every rung.
- For the app-builder route map, run
  `uv run easycat docs --audience app-builders`, or
  `uv run easycat docs --audience app-builders --json` when automation
  needs the same routes.
