# Chapter 9 Exercises

The checkpoint is offline. No provider credentials are needed.

## 1. Trace factory allocation

Run:

```bash
uv run python docs/using-easycat/09-multi-caller/main.py
```

For each simulated request, record whether authentication passed, whether
capacity was reserved, whether the session constructor ran, and whether a
session started. Explain why rejected requests create no session.

## 2. Raise the limit

Change `max_sessions` from 1 to 2. Admit two caller keys without disconnecting
either. A third authorized caller should receive the `capacity` outcome.

Verify the first two sessions are distinct objects and both stop during
cleanup. Restore the original checkpoint afterward.

## 3. Read the auth outcomes

Add a request with `Authorization: Bearer wrong-token`. It should receive the
`auth:invalid` outcome, and the factory count should remain zero at that point.

At an application boundary, return one safe response to the client and retain
the structured `missing` versus `invalid` reason only for low-cardinality
metrics. Never log the supplied credential.

## 4. Test the fail-closed bind guard

In a temporary Python snippet, call:

```python
from easycat.server import enforce_bind_guard

enforce_bind_guard("0.0.0.0", auth=None)
```

Confirm it raises before opening a listener and names the token requirement.
Then pass a `BearerTokenAuth` policy and confirm the guard accepts it. Do not
start a public listener for this exercise.

## 5. Turn the checkpoint into a socket integration test

Replace `LocalSupervisor` with `serve_websocket_sessions` in a temporary test
and mark it with the repository's `integration_socket` marker. Use pytest's
unused-port fixture:

```bash
uv run pytest PATH_TO_TEST -m integration_socket
```

Keep one-second bounds around connection and close waits so a regression fails
instead of hanging CI.

## 6. Build a real config factory

Starting from an earlier chapter's agent and providers, sketch:

```python
def config_for(transport):
    return EasyConfig(transport=transport, agent=build_agent())
```

Classify every object captured by the closure as:

- immutable configuration that is safe to share;
- a documented concurrency-safe client;
- mutable per-session state that must be constructed inside the factory.

Do not run provider calls unless you deliberately enter a credentialed lane.

## 7. Design a drain budget

Choose hypothetical values for `drain_timeout_s`,
`force_shutdown_timeout_s`, and the process manager's termination grace
period. The outer period must cover listener shutdown, both EasyCat phases,
and scheduling margin.

Describe what readiness returns, what new WebSocket callers observe, and what
happens to a session that does not stop within the graceful window.

## 8. Plan horizontal scaling

Assume four worker processes with `max_sessions=20`. Answer:

- Is the hard process-wide total 20 or 80?
- Can worker A's in-memory registry stop a session owned by worker B?
- Which ingress signal prevents routing to a draining worker?
- When would sticky routing or an external session directory be necessary?

## Done when

You can explain:

- why the server accepts factories instead of one prebuilt session;
- why auth precedes provider/session allocation;
- why capacity rejection is safer than an unbounded wait;
- how a released connection returns its slot;
- how readiness, listener closure, graceful drain, and force escalation fit
  into one shutdown sequence.
