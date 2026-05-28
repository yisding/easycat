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

<!-- BEGIN auto:diff prev=14-bring-your-own-agent src=main.py -->
<details>
<summary>Full unified diff vs <code>14-bring-your-own-agent/main.py</code> (auto-generated)</summary>

```diff
--- docs/teaching/14-bring-your-own-agent/main.py
+++ docs/teaching/15-operate-in-production/main.py
@@ -1,22 +1,8 @@
-"""Chapter 14 — bring your own agent via GenericWorkflowBridge.
+"""Chapter 15 — operate in production.
 
-Chapter 13 handed ``agents.Agent(...)`` to ``EasyConfig(agent=...)``.
-Under the hood, ``create_session`` wrapped it in an
-``OpenAIAgentsBridge`` so the runtime could drive it. This chapter
-drops the OpenAI Agents SDK and plugs in a plain async class — the
-same Session code, a different brain.
-
-Three things this script demonstrates:
-
-1. A ``GenericWorkflowBridge`` in *deep mode* — our workflow gets a
-   ``cancel_token`` alongside the user text, so we can stop the LLM
-   stream the instant the user barges in.
-2. Session actions: the workflow enqueues an ``EndCallAction`` when
-   the user says goodbye. ``CoreSessionActionExecutor`` dispatches
-   it and the session stops after the current turn.
-3. Output processors: a three-item pronunciation chain (strip
-   markdown, fix one name, pause on phone numbers) that runs on
-   every committed assistant utterance before it reaches TTS.
+Start a real session, walk it through the full lifecycle, prove
+the journal survives ``stop()``, export a bundle you could hand
+to a teammate, and print the one-liner that opens the debugger UI.
 
 Dependencies:
     uv sync --extra quickstart --group dev
@@ -28,134 +14,97 @@
 import asyncio
 import os
 import time
-from collections.abc import AsyncIterator
 from pathlib import Path
-
-from openai import AsyncOpenAI
 
 from easycat import (
     EasyConfig,
+    JournalRecordKind,
     LocalTransportConfig,
-    MarkdownStripProcessor,
-    PauseProcessor,
-    PhoneticReplacementProcessor,
+    SessionManager,
     attach_runtime_feedback,
     create_session,
     export_debug_bundle,
     wait_for_shutdown_signal,
 )
-from easycat.cancel import CancelToken
-from easycat.integrations.agents import GenericWorkflowBridge
-from easycat.session.actions import CoreSessionActionExecutor, EndCallAction, SessionActions
 
-MODEL = "gpt-4o-mini"
 RUNS_DIR = Path(__file__).parent / "runs"
 
 
-class MyWorkflow:
-    """Our brain. No framework — just async + OpenAI chat completions.
-
-    Deep mode is opted into by the signature: as long as
-    ``on_user_turn`` names a ``recorder`` parameter, the bridge runs
-    us in deep mode and wires ``cancel_token`` through. We don't
-    actually need the recorder here (we aren't journalling tool
-    calls), but naming it is the switch.
+def build_session():
+    """Same shape as ch 13's Local cell. For a real deployment you
+    would typically bump ``debug`` to ``"full"`` and swap
+    ``journal_backend`` to ``"sqlite+litestream"`` so journals
+    survive a process crash; we leave both at teaching defaults
+    here so the run stays fast.
     """
 
-    def __init__(self, client: AsyncOpenAI, actions: SessionActions) -> None:
-        self._client = client
-        self._actions = actions
-        self._history: list[dict] = [
-            {
-                "role": "system",
-                "content": (
-                    "You are a helpful voice assistant. Keep replies under two sentences. "
-                    "If the user says goodbye or asks to hang up, reply with a brief "
-                    "farewell — the transport layer will hang up for you."
-                ),
-            }
-        ]
+    from agents import Agent  # type: ignore[import-untyped]
 
-    async def on_user_turn(
-        self,
-        text: str,
-        *,
-        recorder,  # AgentRecorder — unused here, but names the deep mode switch
-        cancel_token: CancelToken | None,
-    ) -> AsyncIterator[str]:
-        self._history.append({"role": "user", "content": text})
-
-        # Toy intent check; a real app would route via tool calls.
-        if any(w in text.lower() for w in ("bye", "hang up", "goodbye")):
-            # Ask the session to stop after this turn finishes speaking.
-            self._actions.enqueue(EndCallAction(reason="user requested hang-up"))
-            reply = "Sure, ending the call. Goodbye."
-            self._history.append({"role": "assistant", "content": reply})
-            yield reply
-            return
-
-        stream = await self._client.chat.completions.create(
-            model=MODEL, messages=self._history, stream=True
-        )
-        full = ""
-        async for chunk in stream:
-            if cancel_token is not None and cancel_token.is_cancelled:
-                break
-            delta = chunk.choices[0].delta.content or ""
-            if not delta:
-                continue
-            full += delta
-            yield delta  # the bridge wraps each chunk as a text_delta event
-        if full:
-            self._history.append({"role": "assistant", "content": full})
+    config = EasyConfig(
+        openai_api_key=os.environ["OPENAI_API_KEY"],
+        agent=Agent(
+            name="assistant",
+            instructions="You are a helpful voice assistant. Keep replies brief.",
+        ),
+        transport=LocalTransportConfig(),
+        stt="openai",
+        tts="openai",
+        debug="light",
+    )
+    return create_session(config)
 
 
 async def main() -> None:
     if not os.getenv("OPENAI_API_KEY"):
         raise SystemExit("Set OPENAI_API_KEY.")
 
-    client = AsyncOpenAI()
-    actions = SessionActions()  # shared: workflow enqueues, session drains
-    workflow = MyWorkflow(client, actions)
-    bridge = GenericWorkflowBridge(workflow)
-    assert bridge.deep_mode, "deep mode required for mid-turn interruption"
-
-    # A tiny pronunciation pipeline. Processors run serially on every
-    # committed assistant utterance before the text reaches TTS; a
-    # raise in one is logged and the next runs (fail-open).
-    processors = [
-        MarkdownStripProcessor(),
-        PhoneticReplacementProcessor({"easycat": "ee zee cat"}),
-        # 120 ms pause between digit groups in a phone number.
-        PauseProcessor(pattern=r"\b\d{3}[-. ]?\d{3}[-. ]?\d{4}\b", pause_ms=120),
-    ]
-
-    config = EasyConfig(
-        openai_api_key=os.environ["OPENAI_API_KEY"],
-        agent=bridge,  # ← the whole point of this chapter
-        transport=LocalTransportConfig(),
-        stt="openai",
-        tts="openai",
-        output_processors=processors,
-        session_actions=actions,
-        action_executors=(CoreSessionActionExecutor(),),
-        debug="light",
-    )
-    session = create_session(config)
+    # ── 1. SessionManager for multi-session servers ───────────────
+    # In a real server (WebSocket handler, Twilio websocket,
+    # whatever) you'd scope a session to a connection key and let
+    # the manager tear it down on disconnect. We only run one here,
+    # but the shape is the same.
+    manager: SessionManager[str] = SessionManager()
+    session = build_session()
     attach_runtime_feedback(session)
 
-    await session.start()
-    print("Talk to your custom agent. Say 'goodbye' to have it hang up.\n")
-    try:
-        await wait_for_shutdown_signal(session)
-    finally:
-        RUNS_DIR.mkdir(exist_ok=True)
-        path = RUNS_DIR / f"ch14-bridge-{int(time.time())}.bundle"
+    session_key = f"local-{int(time.time())}"
+    async with manager.connection(session_key, session):
+        print(f"Session {session_key!r} started via SessionManager.")
+        print("Talk. Ctrl-C to stop.\n")
         try:
-            export_debug_bundle(session, path, overwrite=True)
-            print(f"Wrote bundle → {path.relative_to(Path.cwd())}")
-        except Exception as exc:  # noqa: BLE001 — teaching script
-            print(f"(no bundle written: {exc})")
+            await wait_for_shutdown_signal(session)
+        except (KeyboardInterrupt, asyncio.CancelledError):
+            pass
+    # manager.connection exited → session.stop() → session.destroy().
+    print("Session stopped; manager released the slot.")
+
+    # ── 2. Post-stop: journal still works, bundle still exports ───
+    # The invariant from CLAUDE.md: after stop()/shutdown(), the
+    # journal is in a read-only postmortem state. .read() works,
+    # export_debug_bundle() works, .append() does not.
+    assert session.journal is not None
+    records = session.journal.read()
+    counts: dict[str, int] = {}
+    for rec in records:
+        if rec.kind is not JournalRecordKind.EVENT:
+            continue
+        counts[rec.name] = counts.get(rec.name, 0) + 1
+    print("\nPost-stop event counts (top 5):")
+    for name, n in sorted(counts.items(), key=lambda kv: -kv[1])[:5]:
+        print(f"  {n:>4}  {name}")
+
+    RUNS_DIR.mkdir(exist_ok=True)
+    bundle_path = RUNS_DIR / f"ch15-{session_key}.bundle"
+    export_debug_bundle(session, bundle_path, overwrite=True)
+    print(f"\nWrote bundle → {bundle_path.relative_to(Path.cwd())}")
+
+    # ── 3. The debugger one-liner ──────────────────────────────────
+    print(
+        "\nOpen the debugger UI on this bundle:\n"
+        f"  uv run python -c 'from easycat.debugger import serve_bundle; "
+        f'serve_bundle("{bundle_path}", port=8765)\'\n'
+        "  → browse http://127.0.0.1:8765"
+    )
 
 
 if __name__ == "__main__":
```

</details>
<!-- END auto:diff -->

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

The session each connection gets is built by a small factory.
Note the teaching defaults — `debug="full"` and
`journal_backend="sqlite+litestream"` would be production
choices; the file keeps them at the chapter defaults so the run
stays fast:

<!-- BEGIN auto:snippet src=main.py symbol=build_session -->
```python
def build_session():
    """Same shape as ch 13's Local cell. For a real deployment you
    would typically bump ``debug`` to ``"full"`` and swap
    ``journal_backend`` to ``"sqlite+litestream"`` so journals
    survive a process crash; we leave both at teaching defaults
    here so the run stays fast.
    """

    from agents import Agent  # type: ignore[import-untyped]

    config = EasyConfig(
        openai_api_key=os.environ["OPENAI_API_KEY"],
        agent=Agent(
            name="assistant",
            instructions="You are a helpful voice assistant. Keep replies brief.",
        ),
        transport=LocalTransportConfig(),
        stt="openai",
        tts="openai",
        debug="light",
    )
    return create_session(config)
```
<!-- END auto:snippet -->

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
