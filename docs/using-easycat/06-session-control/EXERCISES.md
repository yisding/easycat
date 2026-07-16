# Chapter 6 Exercises

These exercises use the credential-free text mode first, then transfer the
same lifecycle decisions to voice.

## 1. Trace event order

Run:

```bash
uv run python docs/using-easycat/06-session-control/main.py text
```

For each turn, locate `turn started`, `agent`, and `turn ended`. Verify that
the third response is workflow turn one even though the `Session` object did
not change.

Add a `tool_started` callback to `session.on(...)`. It should stay silent
because `CounterWorkflow` uses no tools; registering an observer does not
create the event it observes.

## 2. Compare typed and simple callbacks

Replace the offline `agent_response` callback with a typed subscription:

```python
from easycat import AgentFinal

subscription = session.subscribe_event(
    AgentFinal,
    lambda event: print(event.text, event.turn_id),
)
```

Unsubscribe after the second turn, before reset. Confirm that the third reply
still returns even though that observer is gone.

## 3. Reset versus stop

Move `await session.reset_state()` to immediately after the first turn. Predict
the next two counter values, then run the script.

Next, replace reset with `await session.stop()` inside the context and attempt
another `send_text`. Why is the resulting error different from a reset? Restore
the original code afterward.

## 4. Prove post-stop guards

The script already catches the error from a post-stop `send_text`. Remove the
`try`/`except` temporarily and observe the traceback.

Then call `await session.stop()` a second time instead. It should be a no-op:
teardown is idempotent even though new turns are forbidden.

## 5. Subscribe before live startup

Run:

```bash
uv run python docs/using-easycat/06-session-control/main.py voice
```

Confirm that typed user transcripts and simple agent responses appear. Press
Ctrl+C and verify the process exits without an audio-provider traceback.

Why must `subscribe_event(...)` happen before `run_session(session)` if the
observer needs the first turn? Why is a module-level session wrong for browser
or telephony servers?

## 6. Choose graceful or forced teardown

For each owner, choose `stop()` or `stop(force=True)` and explain why:

1. A normal call-complete tool should let the farewell audio drain.
2. A test context is exiting after an assertion failure.
3. The process is shutting down but providers are healthy.
4. A provider task is stuck and the shutdown deadline has expired.

Remember that `async with session:` uses the forced form on exit. Application
code that wants a graceful closing phase should call `stop()` deliberately.

## 7. Prepare for postmortem inspection

Change the text session from `debug="off"` to `debug="light"`. After the
context exits, print the names from `session.journal.read()`.

Do not add a second backend close. The session's stop path already converted
the journal into its supported read-only postmortem view. Restore
`debug="off"` so the chapter's offline guard remains artifact-free.

## Done when

You can explain:

- when a `Session` is unstarted, running, and permanently stopped;
- how typed and ergonomic subscriptions differ;
- why text turns still exercise the agent bridge;
- when to reset, cancel one turn, stop gracefully, or force teardown;
- what remains available for postmortem inspection.
