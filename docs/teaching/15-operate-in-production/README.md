# Chapter 15 — Operate in production

> Chapters 0-14 built and generalised a single session. Production
> means running N of them at once, tearing them down cleanly, and
> being able to debug the one that misbehaved yesterday. This
> chapter is about the operational surface: `SessionManager`, the
> lifecycle methods, the debugger UI, and the CLI.

## Prerequisites

- [Chapter 14.](../14-bring-your-own-agent/)
- `uv sync --extra quickstart --group dev`.
- `OPENAI_API_KEY`.

> **Minimum to skip the ladder:** chapter 13 (you need a
> `create_session` user) and chapter 11 (the debugger UI consumes
> journals). Chapter 14's bridge layer is helpful background but
> not required to use `SessionManager`.

## Diff from chapter 14

- **Added:** `SessionManager` and its `add` / `remove` /
  `stop_all` / `connection(...)` surface; the four lifecycle
  methods (`stop`, `shutdown`, `close`, `destroy`) named and
  bounded — `start` is unchanged from earlier chapters; the
  debugger entry points (`serve_bundle`,
  `serve_session`); the `easycat` CLI (`init`, `doctor`,
  `explain`); `translate.py` — the ch 13 (production-shape)
  → ch 12 (teaching-shape) bundle translator.
- **Modified:** the demo runs through `SessionManager.connection`
  instead of `await session.start()` / `stop()` directly.

## Run

```bash
uv run python docs/teaching/15-operate-in-production/main.py
```

Talk for a few seconds, Ctrl-C. You should see:

1. `Session 'local-…' started via SessionManager.`
2. Your turn(s) happen.
3. `Session stopped; manager released the slot.`
4. A post-stop event-count summary (journal is still readable).
5. A bundle path.
6. The one-liner to open the debugger on that bundle.

## The four lifecycle methods

```
  ┌─────────────────┐   cfg.agent, providers wired
  │ create_session  │
  └────────┬────────┘
           │ await session.start()
           ▼
  ┌─────────────────┐
  │   Session live  │ ──► journal.append() writes records
  └────────┬────────┘
           │ await session.stop()   (graceful)
           │ await session.shutdown() (force-cancel)
           ▼
  ┌─────────────────┐   stop()/shutdown() both call destroy() internally
  │ Session stopped │ ──► journal.read() still works
  └────────┬────────┘ ──► export_debug_bundle() still works
           │ (implicit) destroy() → close() → journal finalized
           ▼
  ┌─────────────────┐
  │   Postmortem    │ ──► SQLite backend closed; JournalView is read-only
  └─────────────────┘
```

| Method | What it does | When to use |
|---|---|---|
| `await session.stop()` | Graceful halt. Cancels in-flight turns, drains queues, disconnects transport, then calls `destroy()`. | The normal shutdown path. |
| `await session.shutdown()` | Force-cancel. Aggressively kills pipeline / STT / TTS / heartbeat tasks, then the same cleanup as `stop()`. | When `stop()` is hung on a misbehaving provider. |
| `session.close()` | Writes the journal's clean-close marker. Does **not** tear down backends. | You almost never call this directly. `destroy()` calls it for you. |
| `session.destroy()` | Backend teardown. Closes the SQLite journal and artifact stores, preserves a read-only postmortem view. | You almost never call this directly. `stop()` / `shutdown()` call it for you. |

The invariant worth memorising: **after `stop()` or `shutdown()`,
`session.journal.read()` and `session.export_debug_bundle()` must
still work.** The journal backend is swapped for a read-only
snapshot during `destroy()`, so the postmortem shape is stable no
matter when you poke at it.

## `SessionManager`

For a multi-connection server — WebSocket, Twilio Media Streams,
whatever — you want something tracking which session belongs to
which connection, with a guaranteed stop on disconnect.
`SessionManager` is that thing.

```python
manager: SessionManager[str] = SessionManager()

async def handle_connection(ws):
    # See examples/twilio_app.py for the full per-connection wiring:
    # an EasyConfig(..., transport=TwilioConnectionTransport(ws))
    # is built per socket and handed to `create_session`, then the
    # manager.
    session = build_session_for(ws)
    async with manager.connection(connection_id, session):
        await ws.wait_closed()
    # connection context exited → session.stop() ran
```

Key properties:

- `add(key, session)` calls `await session.start()` atomically — if
  start fails, the slot is released.
- `stop_all()` gathers all sessions' `stop()` calls concurrently and
  logs exceptions per session without raising.
- `connection(key, session)` is the context-manager sugar for
  `add` + `remove`.

A real Twilio server using exactly this shape lives in
`examples/twilio_app.py`. Crack it open after this chapter.

## The debugger

`src/easycat/debugger/` ships an `aiohttp` single-process web UI
that serves a timeline + per-turn waterfall + record inspector
over a bundle or a live session. Two entry points:

```python
from easycat.debugger import serve_bundle, serve_session

# Offline: bundle on disk.
serve_bundle("runs/ch15-local-123.bundle", port=8765)

# Online: live session, non-blocking.
thread = serve_session(session, port=8765, in_thread=True)
```

On the browser side you get per-stage spans per turn, the journal
record list filterable by kind and stage, the text transcript
reconstructed from `stt.final` / assistant deltas, and a cost
rollup. For chapter 11's bug-hunting, `serve_bundle` on one of the
planted bundles is an instructive follow-up.

## The `easycat` CLI

```bash
$ easycat --help
  init     scaffold a new project
  doctor   check environment + provider reachability
  explain  look up an EasyCat error code
```

- **`easycat init`** — scaffolds a new project from a template
  (`src/easycat/cli/scaffold/`). The fastest path from empty dir
  to a running session.
- **`easycat doctor`** — checks API keys, Python version, optional
  extras, and provider reachability
  (`src/easycat/cli/diagnose/doctor.py`). Run it first when
  something's not working.
- **`easycat explain <code>`** — looks up an error code in the
  registry (`src/easycat/cli/diagnose/explain.py`). When
  `EasyCatError` raises with `code="EC-STT-001"`, this is where
  you find out what that means.

The debugger is intentionally *not* a CLI subcommand — it's imported
and called from Python, because you usually want to serve it from
inside the same process that has the live `Session`.

## The ch 13 → ch 12 bundle translator

Ch 13 emits the real runtime's **production shape**: paired
`stage_start` / `stage_complete` records. Ch 12's eval scripts
key on the **teaching shape**: composite `stage.<name>.execute`
records with an `elapsed_ms` field baked in. The gap is bridged
by pairing on `(turn_id, stage)` and emitting one composite per
pair.

```bash
# 1. Run ch 13 for a few turns.
uv run python docs/teaching/13-swap-providers-and-transports/main.py \
    --provider-mix openai --transport local

# 2. Translate the resulting bundle.
uv run python docs/teaching/15-operate-in-production/translate.py \
    docs/teaching/13-swap-providers-and-transports/runs/ch13-openai-local-*.bundle \
    runs/translated.ndjson
```

`translate.py` is ~50 lines of state machine. Read it top to
bottom — it's the smallest possible thing that explains why
ch 2-12 used the teaching shape (denser to query) and why ch 13
uses the production shape (partial-span visibility on crash).

## Telephony deep-cuts, briefly

`src/easycat/telephony/` has a dozen more modules you haven't
seen: `DTMFAggregator`, `VoicemailDetector`, the `ivr/`,
`screening/`, and `compliance/` subpackages, plus
`TwilioSessionActionExecutor` from ch 14. They're plug-ins to
the same `Session` you've run since chapter 5.

## Try breaking it

1. Add a second session to the manager before the first one stops.
   Two local-transport sessions on the same mic will fight for
   input — what does the journal show for each?
2. Run `easycat doctor` with `OPENAI_API_KEY` unset. Compare with
   it set. Which health checks change?
3. Run `translate.py` against a ch 13 bundle; pipe the output into
   `evals.py` via a small adapter. Do the P50/P95 numbers look
   right?

## The ladder, complete (really)

You have:

- Built each pipeline stage from scratch (chapters 0-9).
- Operated the pipeline with real signal hygiene, observability,
  and evaluation (chapters 10-12).
- Swapped providers and transports, then swapped the agent
  framework itself (chapters 13-14).
- Stood up the operational surface: multi-session management,
  lifecycle discipline, the debugger, the CLI (this chapter).

Every remaining EasyCat surface is either a new provider plugged
into the same factories, a new transport plugged into the same
config, a new bridge plugged into the same shim, or a new telephony
deep-cut plugged into the same executors. The ladder stops here
because the pattern doesn't change.
