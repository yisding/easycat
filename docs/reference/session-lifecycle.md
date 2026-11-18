# Session Lifecycle Reference

This page documents the lifecycle of a `Session` built by
`create_session(EasyConfig(...))` or `Session.from_providers(...)`: starting,
the two stop modes, and what still works after teardown.
Run `uv run easycat explain journal` for a terminal summary of the journal
side of this page.

## Build, Then Start

`create_session()` returns a fully wired but **not started** session. Do
pre-start work first — subscribe events, attach the debugger, register
telephony helpers — then run it:

```python
from easycat import EasyConfig, STTFinal, create_session
from easycat.helpers import run_session

session = create_session(EasyConfig(agent=my_agent, debug="full"))
subscription = session.subscribe_event(STTFinal, lambda e: print(e.text))
run_session(session)  # start, wait for shutdown signal, stop
```

Construction is safe off the event-loop thread. Async servers that build
sessions on a connection hot path may use
`session = await asyncio.to_thread(create_session, config)` to keep filesystem
and optional model initialization from blocking unrelated calls. Start, use,
and stop the returned session on the owning event loop.

For manual control, `await session.start()` begins audio capture and
`await session.stop()` ends it. `async with session:` is the preferred idiom
for tests and scripts — it calls `stop(force=True)` on exit.

## Stopping: `stop()` and `force`

`await session.stop()` is the preferred public teardown verb:

- `stop()` / `stop(force=False)` — graceful: drains in-flight work (the
  current agent turn, queued TTS, transport playback) before tearing down.
- `stop(force=True)` — aggressive: cancels in-flight work first via the
  cooperative `CancelToken` machinery, then tears down. This is what
  `async with session:` uses on exit.

Backend teardown (SQLite/Litestream/libSQL journal backends and artifact
stores) and the journal clean-close marker are handled internally by
`stop()`. There are no separate public close/destroy phases: callers choose
graceful or forceful cancellation through `stop(force=...)`, and both modes
perform the same complete resource teardown.

`stop()` is idempotent — calling it again after teardown is a no-op.

## Postmortem: the Journal Outlives the Session

After a clean `stop()`, the session keeps a preserved read-only view of its
journal, so postmortem flows still work:

- `session.journal.read()` — iterate the recorded events, spans, and
  metrics.
- `session.export_debug_bundle(path)` — export a replayable bundle for
  `easycat bundles show`, `easycat inspect`, `easycat replay`, and the
  debugger UI.

Set `EasyConfig(record_to="bundles/")` to export a timestamped bundle
automatically on every stop — the "always be recording" flow. Both require
`debug != "off"` so a journal exists; see the
[EasyConfig reference](easyconfig.md) for the journal backend and retention
knobs and [observability](../observability.md) for the inspection tooling.

## Mid-Session Control

Between start and stop, the session exposes turn-level controls:

- `await session.cancel_turn()` — cancel the in-flight turn (agent + TTS) without
  stopping the session.
- `await session.prompt_agent(text, role="system", speak=True)` — run a
  journaled application-initiated agent turn. Spoken prompts use the normal
  cancellable TTS/playback path and require a started, non-text session; set
  `speak=False` when the application needs only the response text (this also
  works before `start()` and in `text_session` mode).
- `session.set_audio_capture_enabled(enabled)` — pause or resume persisting
  audio artifacts while leaving transcripts and journal events enabled. A
  callable consent policy remains authoritative; pass `None` to clear the
  runtime override.
- `await session.reset_state()` — clear turn state and conversation pointers.
- `await session.send_text(text)` — inject a user turn without audio on a
  session built by `create_text_session(...)`. Voice sessions reject this
  method and continue to take user input from their transport.
- `await session.play_dtmf_tones(digits, inter_digit_delay_ms=0,
  tone_config=None, fence_timeout_s=10.0)` — play DTMF key presses (`0-9`,
  `*`, `#`, `A-D`, and `w`/`W` pauses) as in-band ITU-T Q.23 tones on the
  live media stream, after any queued bot speech. The session keeps running.
  It returns a `DTMFTonePlayback` once the tone frames are accepted; await
  `playback.wait(timeout)` for `"played"`, `"timeout"`, `"disconnected"`, or
  `"estimated"` (the transport cannot acknowledge playback). Cancelling the
  caller, for example by a barge-in, does not cut the sequence short. On a
  transport with the `ProtectedPlayback` capability (Twilio Media Streams), a
  barge-in `clear` waits until the tones have played. It raises
  `RuntimeError` before `start()`, after `stop()` has begun, in
  `text_session` mode, or when the transport rejects a tone frame, and
  `ValueError` for invalid digits without echoing them. The journal records
  `dtmf_tones_sent` and `dtmf_tones_completed` without the digits. Tones are
  sent as 20 ms frames in the transport's preferred TTS output format (else
  its `audio_format`, else 8 kHz mono), so transports that play at a fixed
  device rate, such as `LocalTransport`, still hear valid key frequencies.

`stop(force=True)` aborts a protected tone burst immediately and resolves its
playback handle as `"disconnected"`. A graceful `stop()` lets an in-progress
burst finish first, which can take up to `fence_timeout_s` longer when the
transport never acknowledges the fence mark; a burst that outlives its budget
(drain wait, fence timeout, tone length and guard margin) is cancelled. A
`stop()` called from an `AudioOut` or `Error` handler on the outbound send
path cancels pending bursts instead of waiting for them.

## Related Pages

- [EasyConfig field reference](easyconfig.md) — every construction knob.
- [Events reference](events.md) — the lifecycle events
  (`TurnStarted`, `BotStoppedSpeaking`, …) emitted between start and stop.
- [Architecture](../architecture.md) — the collaborators `stop()` tears
  down.
