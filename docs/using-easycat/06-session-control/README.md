# Chapter 6: Control a Session

`VoiceApp.run(...)` is the right default when the app only needs to start and
serve. Drop to the public `Session` when your code needs the object before
startup, must drive text turns itself, or owns lifecycle composition inside a
larger async application.

This chapter builds the same runtime through `EasyConfig`, but keeps the
resulting `Session` so it can subscribe, reset, wait, and stop deliberately.

## Prerequisites

- Complete [chapter 5](../05-agent-bridges/), or know which agent object you
  will pass to EasyCat.
- Run `uv sync --extra quickstart --group dev` from the repository root.
- `OPENAI_API_KEY` for live `voice` mode. Offline `text` mode uses a
  deterministic workflow and no audio or provider calls.
- A microphone and speakers for live mode.
- Run `uv run easycat doctor` after exporting the key. If it lives in `.env`,
  run `uv run easycat doctor --env-file .env`. Use
  `uv run easycat doctor --json` or
  `uv run easycat doctor --env-file .env --json` for parseable checks.
- If the key lives in `.env`, add `--env-file .env` after `uv run` for voice
  mode.

## Run the complete lifecycle offline

```bash
uv run python docs/using-easycat/06-session-control/main.py text
```

The script sends two turns, resets conversation state, sends another turn,
and exits the context manager. Its final lines are:

```text
Reply 1: Workflow turn 1: first message
Reply 2: Workflow turn 2: second message
Reply after reset: Workflow turn 1: after reset
Post-stop guard: Session has been stopped
```

The event lines above those replies come from the same `Session.on(...)`
surface used by a voice run. No STT, TTS, VAD, or transport is constructed in
a text session.

## Build, subscribe, run, stop

The lifecycle has one direction:

```text
EasyConfig -> create_session -> unstarted Session -> running Session -> stopped Session
                                 subscribe first     turns/events       journal/export only
```

`EasyConfig` contains app-facing provider descriptors and policies.
`create_session(config)` resolves them, wires live collaborators, and returns
an unstarted `Session`. Subscribe or attach app hooks before anything begins.

For a caller that does not own an event loop, `run_session(session)` preserves
the quickstart's signal handling and console feedback. Internally it uses the
public context-managed lifecycle. The live checkpoint does exactly that:

```bash
uv run python docs/using-easycat/06-session-control/main.py voice
```

If the key is in `.env`:

```bash
uv run --env-file .env python docs/using-easycat/06-session-control/main.py voice
```

Speak a few turns and press Ctrl+C when finished. The `finally` block removes
the example's subscriptions; `run_session` has already stopped the session.

`VoiceApp(config=config).session("local")` is another way to obtain the same
caller-owned local session, as chapter 4 showed. Multi-session browser,
WebSocket, and telephony modes do not have one global `Session`; their server
factory creates one per connection.

## Use one public teardown verb

When your application owns an event loop, prefer:

```python
session = create_session(config)
async with session:
    await session.wait_closed()
```

Entering starts a voice session. Exiting calls `stop(force=True)` so an
exception or cancelled owner task cannot leak providers. A text session does
not need or support `start()`; its context manager still guarantees teardown.

For manual ownership, `await session.stop()` is the single public teardown
verb:

- `stop()` / `stop(force=False)` is graceful and drains in-flight work.
- `stop(force=True)` cancels in-flight work first and is the bounded escape
  path for an interrupted owner or stuck provider.
- `stop()` is idempotent.
- A stopped session cannot restart; construct a new one.

`await session.wait_closed()` waits for another task, a session action, or a
transport failure to stop the session. It returns immediately after teardown,
which is why the offline checkpoint can call it after leaving `async with`.

## Choose the event subscription surface

The live checkpoint uses both public styles.

`session.subscribe_event(EventType, handler)` gives the handler the full typed
event, including correlation and timestamp fields. It returns an
`EventSubscription` with `unsubscribe()`:

```python
subscription = session.subscribe_event(
    STTFinal,
    lambda event: print(event.text),
)
```

`session.on(...)` covers common UI and application callbacks without event
imports. Each callback receives only useful fields:

```python
registrations = session.on(
    agent_response=lambda text: print(text),
    interruption=lambda: print("interrupted"),
)
```

Pass that returned registration list to
`session.unsubscribe_handlers(registrations)`. A third helper,
`subscribe_agent_events(...)`, groups full `AgentDelta`, `AgentFinal`, and tool
event handlers when an observer needs their typed payloads.

Handlers may be synchronous or asynchronous, but they run on the session's
event loop. Keep them fast; hand slow I/O to an application-owned task or
queue.

## Text turns use the real agent path

`create_text_session(...)` omits the audio pipeline and exposes
`await session.send_text(text)`. The turn still passes through agent
adaptation, runner timeouts, tool-event translation, event correlation,
journaling, and latency metrics.

This makes text sessions useful for chat UIs and deterministic development,
not just toy mocks. `send_text()` only works on a text session; voice sessions
receive user input from their transport.

Text sessions default to the in-memory `debug="light"` journal. This lesson
selects `debug="off"` so its credential-free guard records nothing at all.
Chapter 7 turns on durable `debug="full"` capture and inspects the resulting
journal and bundle.

## Reset a conversation without replacing the session

`await session.reset_state()` cancels current turn work, clears turn and
agent conversation state, and returns the session to an idle state. The
offline workflow implements `reset()`, so its counter visibly returns to one.

Use `reset_state()` for an explicit “new conversation” action. It is not a
teardown: the same text session can accept another `send_text`, and a running
voice session can continue listening.

For a narrower operation, `await session.cancel_turn()` stops the in-flight
agent/TTS turn without clearing the whole conversation or stopping the
session. Normal voice barge-in calls that turn-level cancellation path for
you.

## Stopped does not mean uninspectable

After a clean stop, live providers and writable debug backends are closed.
When debugging was enabled, EasyCat preserves a read-only postmortem view:

- `session.journal.read()` still returns recorded events, spans, and metrics.
- `session.export_debug_bundle(...)` can still create a replayable bundle.

Do not create separate close/destroy phases for those backends. `stop()` owns
their teardown and preserves only the supported read-only view.

Continue with [the exercises](./EXERCISES.md) to trace event order and choose
graceful versus forced ownership. Chapter 7 will use the preserved journal.

## What you should be able to answer now

> When should I use `create_session` instead of `VoiceApp.run`?

When you need the session before startup, own an async lifecycle, or need
direct event/turn control.

> Does a text session bypass agent bridges?

No. It bypasses audio while retaining the real agent turn path.

> Can I call `start()` again after `stop()`?

No. A stopped session is a postmortem object; construct a fresh session for a
new run.

## What's next

Chapter 7 enables durable debugging and follows one run through journals,
bundles, inspect, replay, diff, and the debugger.
