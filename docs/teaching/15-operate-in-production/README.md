# Chapter 15 — Operate in production

<!-- BEGIN auto:navigation -->
**Progress: 16 of 16** · [← Chapter 14 — Bring your own agent](../14-bring-your-own-agent/) · [Ladder index](../) · [Progress worksheet](../PROGRESS.md) · [Exercises](./EXERCISES.md)
<!-- END auto:navigation -->

> Chapters 0-14 built and generalised a single session. Production
> means running N of them at once, tearing them down cleanly, and
> being able to debug the one that misbehaved yesterday. This
> chapter is about the operational surface: `SessionManager`, the
> public lifecycle, the debugger UI, and the CLI.

<!-- BEGIN auto:spaced-retrieval -->
## Recall before reading

> **Following the ladder? Spaced retrieval — Chapter 13 — Swap Providers AND Transports**
>
> Close earlier chapters and answer from memory before reading further. If this
> chapter is your starting point, skip this block.
>
> **Answer from memory:**
>
> How many cells result from two provider mixes × three transports, and which values stay fixed
> along each axis?
>
> After recording your answer, explain one way `provider × transport matrix` changes how you
> reason about `multi-session manager rollback`. Keep the first answer visible.
>
> **Check only after answering:**
>
> ```bash
> uv run python docs/teaching/13-swap-providers-and-transports/matrix_probe.py
> ```
>
> Cite one observed field, measurement, or behavior; repair only the part your
> evidence disproved.
<!-- END auto:spaced-retrieval -->

<!-- BEGIN auto:offline-checkpoint -->
> **Hardware-free checkpoint:** prove `multi-session manager rollback` without a microphone,
> speakers, or provider credentials:
>
> **Predict first:** Which failures release manager slots, and does one stop failure prevent the
> peer stop?
>
> ```bash
> uv run python docs/teaching/15-operate-in-production/manager_probe.py
> ```
>
> **Evidence to find:** failed starts release slots; stop-all records one error and still
> attempts both sessions.
>
> **Explain the result:** Explain which rollback invariant prevents one failed session from
> blocking another.
>
> [See all 16 checkpoints](../#hardware-free-checkpoint-spine).
<!-- END auto:offline-checkpoint -->

## Prerequisites

- [Chapter 14.](../14-bring-your-own-agent/)
- `uv sync --extra quickstart --group dev`.
- `OPENAI_API_KEY`.
- Running this chapter makes live provider calls that may incur charges.
  Review your provider billing and usage limits first.
- Provider-backed scripts may send audio, transcripts, or prompts to configured
  services. Use non-sensitive test content and review provider data-handling
  policies first.
- After setting provider keys, run `uv run easycat doctor` from the repo root; if keys live in `.env`, run `uv run easycat doctor --env-file .env`. Use `uv run easycat doctor --env-file .env --json` for parseable checks.
- If keys live in `.env`, also add `--env-file .env` after `uv run`
  in the chapter command you run.

> **Minimum to skip the ladder:** chapter 13 (you need a
> `create_session` user) and chapter 11 (the debugger UI consumes
> journals). Chapter 14's bridge layer is helpful background but
> not required to use `SessionManager`.

## Diff from chapter 14

- **Added:** `SessionManager` and its `add` / `remove` /
  `stop_all` / `connection(...)` surface; the public lifecycle
  surface (`async with session:`, `stop`, and `stop(force=True)`)
  named and bounded — `start` is
  unchanged from earlier chapters; the
  debugger entry points (`serve_bundle`,
  `serve_session`); the `easycat` CLI (`init`, `doctor`,
  `explain`, `bundles`, `inspect`, `validate`); `latency_gate.py` —
  a captured production-bundle percentile and sample-count gate.
- **Modified:** the demo runs through `SessionManager.connection`
  instead of `await session.start()` / `stop()` directly.

<!-- BEGIN auto:diff prev=14-bring-your-own-agent src=main.py trim_blank_context=true -->
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
@@ -33,30 +19,19 @@
 import os
 import shlex
 import time
-from collections.abc import AsyncIterator
 from pathlib import Path
-from typing import TYPE_CHECKING

 from easycat import (
     EasyConfig,
+    JournalRecordKind,
     LocalTransportConfig,
-    MarkdownStripProcessor,
+    SessionManager,
     attach_runtime_feedback,
     create_session,
-    default_pronunciation_processors,
     export_debug_bundle,
     wait_for_shutdown_signal,
 )
-from easycat.cancel import CancelToken
-from easycat.integrations.agents import GenericWorkflowBridge
-from easycat.integrations.agents.base import AgentRecorder, CancellationMode
-from easycat.llm_output_processing import LLMOutputProcessor
-from easycat.session.actions import CoreSessionActionExecutor, EndCallAction, SessionActions

-if TYPE_CHECKING:
-    from openai import AsyncOpenAI
-
-MODEL = "gpt-5.6-luna"
 RUNS_DIR = Path(__file__).parent / "runs"


@@ -76,188 +51,96 @@
     )


-def pronunciation_command(path: Path) -> str:
-    """Inspect the scheduler's provider-ready pronunciation payloads."""
+def debugger_command(path: Path, *, port: int = 8765) -> str:
+    """Open the maintained debugger CLI on this captured bundle."""
     return shlex.join(
         [
             "uv",
             "run",
             "easycat",
-            "journal",
-            "grep",
+            "debugger",
+            "serve",
             str(_display_path(path)),
-            "--query",
-            "tts_payload_prepared",
-            "--json",
+            "--port",
+            str(port),
         ]
     )


-def build_output_processors() -> list[LLMOutputProcessor]:
-    """Build the chapter's pronunciation stack from the public factory."""
-    return [
-        MarkdownStripProcessor(),
-        *default_pronunciation_processors(
-            name_pronunciations={"easycat": "ee zee cat"},
-            phone_ellipsis_count=1,
-        ),
-    ]
-
-
-class MyWorkflow:
-    """Our brain. No framework — just async + OpenAI chat completions.
-
-    Deep mode is opted into by the signature: as long as
-    ``on_user_turn`` names a ``recorder`` parameter, the bridge runs
-    us in deep mode and wires ``cancel_token`` through. We don't
-    actually need the recorder here (we aren't journalling tool
-    calls), but naming it is the switch. The history hooks below
-    keep our private message list aligned with what the caller heard.
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
-        recorder: AgentRecorder,  # unused here, but names the deep mode switch
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
-            model=MODEL,
-            reasoning_effort="none",
-            messages=self._history,
-            stream=True,
-        )
-        full = ""
-        try:
-            async with stream as response_stream:
-                async for chunk in response_stream:
-                    if cancel_token is not None and cancel_token.is_cancelled:
-                        break
-                    delta = chunk.choices[0].delta.content or ""
-                    if not delta:
-                        continue
-                    full += delta
-                    yield delta  # the bridge wraps each chunk as a text_delta event
-        finally:
-            # BridgeTemplate closes this generator on barge-in. Commit the
-            # delivered prefix before apply_interruption rewrites it to what
-            # the caller actually heard.
-            if full:
-                self._history.append({"role": "assistant", "content": full})
-
-    def apply_interruption(self, delivered_text: str, mode: CancellationMode) -> None:
-        """Rewrite private history to the portion the caller actually heard."""
-        suffix = "..." if delivered_text and mode is CancellationMode.IMMEDIATE_STOP else ""
-        self.replace_last_assistant_text(f"{delivered_text}{suffix}")
-
-    def replace_last_assistant_text(self, text: str) -> None:
-        """Let interruption and Markdown cleanup update private history."""
-        for message in reversed(self._history):
-            if message["role"] == "assistant":
-                message["content"] = text
-                return
-            if message["role"] == "user":
-                # No assistant output was generated for this turn. Do not
-                # rewrite the previous turn's already-committed response.
-                return
-
-    def snapshot_state(self) -> dict[str, object]:
-        """Allowlist privacy-safe workflow metadata for debug artifacts."""
-        last_assistant = next(
-            (
-                str(message["content"])
-                for message in reversed(self._history)
-                if message["role"] == "assistant"
-            ),
-            "",
-        )
-        return {
-            "message_count": len(self._history),
-            "history_roles": [str(message["role"]) for message in self._history],
-            "last_assistant_chars": len(last_assistant),
-            "session_action_pending": self._actions.has_pending,
-        }
+    config = EasyConfig(
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
-    from openai import AsyncOpenAI
-
     if not os.getenv("OPENAI_API_KEY"):
         raise SystemExit("Set OPENAI_API_KEY.")

-    # The custom workflow owns this client; EasyCat only owns providers and
-    # transports it creates from EasyConfig. Keep the caller-owned scope outer.
-    async with AsyncOpenAI() as client:
-        actions = SessionActions()  # shared: workflow enqueues, session drains
-        workflow = MyWorkflow(client, actions)
-        bridge = GenericWorkflowBridge(workflow)
-        assert bridge.deep_mode, "deep mode required for mid-turn interruption"
+    # ── 1. SessionManager for multi-session servers ───────────────
+    # In a real server (WebSocket handler, Twilio websocket,
+    # whatever) you'd scope a session to a connection key and let
+    # the manager tear it down on disconnect. We only run one here,
+    # but the shape is the same.
+    manager: SessionManager[str] = SessionManager()
+    session = build_session()
+    attach_runtime_feedback(session)

-        # A tiny pronunciation pipeline. Processors run serially on every
-        # committed assistant utterance before the text reaches TTS; a
-        # raise in one is logged and the next runs (fail-open).
-        processors = build_output_processors()
+    session_key = f"local-{int(time.time())}"
+    async with manager.connection(session_key, session):
+        print(f"Session {session_key!r} started via SessionManager.")
+        print("Talk. Ctrl-C to stop.\n")
+        try:
+            await wait_for_shutdown_signal(session)
+        except (KeyboardInterrupt, asyncio.CancelledError):
+            pass
+    # manager.connection exited -> session.stop() -> private teardown.
+    print("Session stopped; manager released the slot.")

-        config = EasyConfig(
-            agent=bridge,  # ← the whole point of this chapter
-            transport=LocalTransportConfig(),
-            stt="openai",
-            tts="openai",
-            output_processors=processors,
-            session_actions=actions,
-            action_executors=(CoreSessionActionExecutor(),),
-            debug="light",
-        )
-        session = create_session(config)
-        attach_runtime_feedback(session)
+    # ── 2. Post-stop: journal still works, bundle still exports ───
+    # The lifecycle invariant: Session.journal is always a read-only
+    # JournalView. After stop(), that same view reads a preserved
+    # postmortem backend, and export_debug_bundle() still works.
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

-        try:
-            async with session:
-                print("Talk to your custom agent. Say 'goodbye' to have it hang up.\n")
-                await wait_for_shutdown_signal(session)
-        finally:
-            # Session exit preserves the read-only journal view. Export while
-            # the custom workflow's client is still in its separately owned
-            # scope, including when shutdown arrives through cancellation.
-            RUNS_DIR.mkdir(exist_ok=True)
-            path = RUNS_DIR / f"ch14-bridge-{int(time.time())}.bundle"
-            try:
-                export_debug_bundle(session, path, overwrite=True)
-                print(f"Wrote bundle → {_display_path(path)}")
-                human_command, json_command = measurement_commands(path)
-                print("Measure this production-shaped bundle directly:")
-                print(f"  {human_command}")
-                print(f"  {json_command}")
-                print("Inspect its provider-ready pronunciation payloads:")
-                print(f"  {pronunciation_command(path)}")
-            except Exception as exc:  # noqa: BLE001 — teaching script
-                print(f"(no bundle written: {exc})")
+    RUNS_DIR.mkdir(exist_ok=True)
+    bundle_path = RUNS_DIR / f"ch15-{session_key}.bundle"
+    export_debug_bundle(session, bundle_path, overwrite=True)
+    print(f"\nWrote bundle → {_display_path(bundle_path)}")
+    human_command, json_command = measurement_commands(bundle_path)
+    print("Measure this production-shaped bundle directly:")
+    print(f"  {human_command}")
+    print(f"  {json_command}")
+
+    # ── 3. The debugger CLI ────────────────────────────────────────
+    print("\nOpen the debugger UI on this bundle:")
+    print(f"  {debugger_command(bundle_path)}")


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
6. Human and JSON `easycat latency` commands for that bundle.
7. The one-liner to open the debugger on that bundle.

## The public lifecycle

```
  ┌─────────────────┐   cfg.agent, providers wired
  │ create_session  │
  └────────┬────────┘
           │ await session.start()
           ▼
  ┌─────────────────┐
  │   Session live  │ ──► runtime backend appends records
  └────────┬────────┘ ──► session.journal reads through JournalView
           │ await session.stop()           (graceful drain)
           │ await session.stop(force=True) (force-cancel)
           ▼
  ┌─────────────────┐   public stop paths run private backend teardown
  │ Session stopped │ ──► journal.read() still works
  └────────┬────────┘ ──► export_debug_bundle() still works
           │ (implicit) journal finalized; read-only view preserved
           ▼
  ┌─────────────────┐
  │   Postmortem    │ ──► SQLite backend closed; JournalView is read-only
  └─────────────────┘
```

| Method | What it does | When to use |
|---|---|---|
| `async with session:` | Starts the session on entry and calls `stop(force=True)` on exit. | Preferred scoped idiom for examples and servers that tie one session to one block. |
| `await session.stop()` | Public graceful halt. Drains in-flight work, disconnects transport, finalizes private backends, and preserves postmortem journal/bundle access. | The normal shutdown path. |
| `await session.stop(force=True)` | Public force-cancel path. Cancels pipeline/provider work before the same teardown. | When graceful stop is stuck or the caller is exiting a scope. |

The invariant worth memorising: **after `stop()`,
`session.journal.read()` and `session.export_debug_bundle()` must
still work.** `session.journal` is a stable, read-only `JournalView`
for the entire session lifetime; application code never appends through
it. During private teardown, EasyCat replaces the live backend behind
that same view with a read-only SQLite wrapper or frozen in-memory
snapshot. A caller that cached the view before stop can keep using it.

Run the provider-free full-SQLite proof:

```bash
uv run python docs/teaching/15-operate-in-production/postmortem_probe.py
```

It executes a real text turn with the built-in echo agent, then shows
the same public `JournalView` observing a `SqliteJournal` before stop and
a `ReadonlySqliteJournal` afterward. It also exports and reloads a bundle
after stop and verifies that its record sequence matches the postmortem
view. The temporary data directory is removed when the probe exits.

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

- `add(key, session)` reserves a unique key, then awaits
  `session.start()`. If start fails or the add task is cancelled, the
  manager's reservation is released before the exception or cancellation
  is re-raised. If another session has already claimed the key, it is
  preserved. The session's own start path owns rollback of resources it
  opened before that interrupted start.
- `stop_all()` gathers all sessions' `stop()` calls concurrently and returns a
  `SessionStopReport`; one failure never prevents the remaining attempts.
  Inspect `report.failures` (or `report.ok`) instead of relying on logs alone.
  Successfully stopped sessions are removed; failed or cancelled teardowns
  stay registered for a later retry. Pass `force=True` for that final forced
  sweep.
- `connection(key, session)` is the context-manager sugar for
  `add` + `remove`.
- Do not call `remove()` / `stop_all()` on a key while application code
  is still active inside its `connection(...)` block. Coordinate
  cancellation of those handler tasks first, then perform the final
  sweep.

Run the provider-free [manager probe](manager_probe.py) to see two
active slots, duplicate-key rejection, ordinary and cancelled-start
rollback, key reuse, and context-managed removal without opening a
microphone. Its final `stop_all()` sweep also proves that every captured
session is asked to stop, one stop failure does not abort the rest of
the sweep, and the failed entry remains available for retry. The structured
result appears under `stop_all.report`; the intentional matching log is
captured under `stop_all.expected_error` instead of leaking to stderr and
making the probe itself look failed.

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
over a bundle or a live session. For a captured bundle or SQLite
journal, use the maintained operator CLI; it binds to loopback and
opens the browser by default:

```bash
uv run easycat debugger serve runs/ch15-local-123.bundle --port 8765
```

The Python entry points remain available when you need to embed the
debugger in application code; `serve_session` is the live-session
attachment:

```python
from easycat.debugger import serve_bundle, serve_session

# Offline: bundle on disk.
serve_bundle("runs/ch15-local-123.bundle", port=8765)

# Online: live session, non-blocking.
thread = serve_session(session, port=8765, in_thread=True)
```

On the browser side you get per-stage spans per turn, the journal
record list filterable by kind and stage, and the text transcript
reconstructed from `stt_final` / assistant deltas. For chapter 11's
bug-hunting, `serve_bundle` on one of the
planted bundles is an instructive follow-up.

## The `easycat` CLI

From this repo, run CLI commands through `uv run` so they use the
checked-out EasyCat package and virtualenv. In an installed app
environment, drop the `uv run` prefix.

```bash
$ uv run easycat
EasyCat — voice bot framework

  Scaffold
    console     Try the keyless offline console (--live explicitly enables a provider)
    init        Scaffold a new project from a template
    doctor      Check local readiness, configured credentials, and provider network liveness
    serve       Serve the browser playground or a manifest-backed VoiceServer
    plan        Show the provider/capability plan for a manifest profile

  Debug with the journal
    bundles     List captured debug bundles and crash dumps
    debugger    Open the browser debugger for a captured call
    inspect     Summarise a debug bundle or SQLite journal
    replay      Replay a debug bundle or SQLite journal
    latency     Summarise critical-path latency percentiles for a bundle
    diff        Diff two bundles turn-by-turn for milestone regressions
    journal     Search and tail captured journals and crash dumps
    tail        Live-tail a SQLite journal as it grows
    explain     Route a call problem by symptom, or look up an error code

  Validation
    validate    Run validation checks and inspect validation reports

  Docs and guidance
    docs        Show docs for learning, maintenance, validation, and operations

Run easycat <command> --help for command-specific options.
Run easycat docs for learning, maintenance, validation, and operations routes.
Run easycat docs --json for machine-readable docs routes, audiences, and command hints.
Run easycat explain <code> for errors.
Run easycat explain json-schema for CLI JSON.
```

- **`uv run easycat console`** — tries EasyCat in your terminal with no API
  keys (`src/easycat/cli/console.py`): a keyless interactive text loop with
  an echo agent that ignores ambient provider credentials, always ending with
  an exported debug bundle path and a replay hint. Add `--voice-demo`
  (`uv run easycat console --voice-demo`) to run one scripted no-key turn
  through the full audio pipeline. Live OpenAI traffic is an explicit opt-in:
  `--live` uses voice when a microphone works and otherwise falls back to a
  live text session.
- **`uv run easycat init my-agent`** — scaffolds a new project from a template
  (`src/easycat/cli/scaffold/`). The fastest path from empty dir
  to a running session. Run `uv run easycat init --list-templates` first when
  you need to compare transports and agent frameworks; the list includes
  base `easycat[...]` package requirements and extras, required environment
  variables, optional environment knobs, generated files, and copyable
  create/preflight/check/fix/docs/json-schema/run commands for each template
  (`uv run easycat init --list-templates --json` emits the
  same template catalog and post-scaffold command previews).
- **`uv run easycat doctor`** — reports Python/EasyCat versions, provider
  credentials and reachability, optional `onnxruntime`, the dev-profile
  microphone, journal writability, and disk space
  (`src/easycat/cli/diagnose/doctor.py`). Use
  `--environment production` to omit the local-microphone probe and
  `--provider <name>` to require and probe one provider. If a scaffolded app
  stores keys in `.env`, run `uv run easycat doctor --env-file .env`; add `--json`
  (`uv run easycat doctor --json`,
  `uv run easycat doctor --env-file .env --json`) for parseable first-run
  environment checks.
- **`uv run easycat serve`** — serves the browser voice playground on
  localhost (`src/easycat/cli/serve.py`): one command that wires
  `EasyConfig.browser()` to the bundled WebRTC client and prints an
  `Open http://localhost:8080` URL, with live transcript, interruption
  indicator, and per-turn latency readout in the page. Needs an OpenAI
  key; a non-loopback `--host` requires `--token` (or
  `EASYCAT_SERVE_TOKEN`). Put a remote browser serve behind TLS and pass its
  HTTPS origin with `--public-url` (or `EASYCAT_SERVE_PUBLIC_URL`); EasyCat
  will not print a token-bearing direct-HTTP link.
- **`uv run easycat plan`** — resolves an `easycat.toml` profile into its
  provider/capability plan across all seven pipeline roles
  (`src/easycat/cli/plan.py`), reporting the selected provider per role plus
  any missing API keys or optional extras — without instantiating providers.
  It is the side-effect-free, ahead-of-deploy counterpart to a server's
  `/health/ready` check; add `--json` (`uv run easycat plan --json`) for the
  standard machine-readable envelope, and `--profile` to plan a non-default
  `[voice.<profile>]`.
- **`uv run easycat docs`** — prints the maintained docs map and route
  descriptions so installed users can jump to quickstart, examples, teaching
  chapters, architecture and maintenance guides, deployment, observability,
  and validation reference material.
  Use `uv run easycat docs --audience operators` for the production and
  observability route set, or
  `uv run easycat docs --audience operators --json` when automation needs
  parseable operator-facing routes; `uv run easycat docs --json` emits the
  same route map with command hints and audience labels. Replace uppercase or
  angle-bracket placeholders such as `PATH` or `<session_id>` before running
  those hints. Coding agent? Use the root
  [AGENTS.md](../../../AGENTS.md) for repository coding rules; use
  [llms.txt](../../../llms.txt) for machine-readable docs route discovery or
  run `uv run easycat explain json-schema`.
- **`uv run easycat explain <code>`** — looks up an error code in the
  registry (`src/easycat/cli/diagnose/explain.py`). When
  `EasyCatError` raises with `code="EASYCAT_E203"`, this is where
  you find out what that means. `uv run easycat explain json-schema`
  documents the standard `--json` envelope and command-specific success and
  error fields.
- **`uv run easycat bundles list`** / **`uv run easycat bundles show <path>`** —
  list captured bundles and crash dumps, then summarize a debug bundle or
  SQLite journal from the shell.
- **`uv run easycat debugger serve <path>`** — open the browser debugging UI
  for a captured bundle or SQLite journal.
- **`uv run easycat bundles export <path>`** — write a redacted context pack
  that a coding agent can read without copying raw journal payloads.
- **`uv run easycat inspect <path>`** — friendly alias for
  `uv run easycat bundles show <path>` for bundles and SQLite journals.
- **`uv run easycat replay <path>`** — replay a debug bundle or SQLite
  journal from the shell. It defaults to artifact fidelity and denies
  recorded tool frames unless you choose `--tool-policy stub` or
  `--tool-policy allow`; the CLI itself never invokes external tools.
- **`uv run easycat latency <path>`** — summarise critical-path latency
  percentiles (p50/p95/p99) for a bundle or SQLite journal, splitting the
  pipeline dispatch wait from the model's first-token time so you can tell a
  slow pipeline from a slow model.
- **`uv run easycat diff <path-a> <path-b>`** — diff two bundles turn by turn,
  surfacing milestone and transcript deltas so you can see which segment
  regressed between a baseline ("before") and a comparison ("after") run.
- **`uv run easycat journal grep <path> --query TEXT`** / **`uv run easycat journal
  follow <path>`** / **`uv run easycat journal promote <path> TURN_ID --out FILE`** —
  full-text search a journal or bundle, live-tail a SQLite journal as it grows,
  or promote a single turn from either source into a replayable, self-contained
  regression bundle. Every emitted line is redacted.
- **`uv run easycat tail <path>`** — live-tail a SQLite journal as it grows; a
  short alias for `uv run easycat journal follow <path>`.
- **`uv run easycat validate quick`** — deterministic local validation
  for normal PR work. `uv run easycat validate quick --json` emits the
  current quick validation run inside the
  standard CLI envelope.
- **`uv run easycat validate socket`** — localhost socket integration
  validation.
- **`uv run easycat validate stress`** — local stress validation and
  saturation-signal capture.
- **`uv run easycat validate contracts`** — offline provider,
  protocol, and bridge contract validation. Use
  `uv run easycat validate contracts --json` for a parseable contract run.
- **`uv run easycat validate latency --smoke`** — low-cost live latency
  probe; use `--sweep` for the broader condition matrix.
- **`uv run easycat validate live`** — live provider canaries and
  capability reports.
- **`uv run easycat validate release`** — build the package, install the
  wheel into a clean temporary venv, verify it outside the source tree, and
  run the release validation gates through the installed package.
  `uv run easycat validate release --json` emits the
  installed-wheel validation result inside the standard CLI envelope.
- **`uv run easycat validate report .easycat/validation/latest.json`** —
  render a concise summary of the latest saved validation report;
  `uv run easycat validate report .easycat/validation/latest.json --json`
  re-emits the saved report inside that same envelope. Use
  `.easycat/validation/runs/<run_id>/report.json` for a specific older run.

`easycat debugger serve` is the offline operator surface for captured
bundles and SQLite journals. Use `serve_session(...)` from Python when
the debugger must stay attached to a live in-process `Session`.

## Gate captured production latency directly

Chapters 2–12 use compact synthetic records to teach one concept at a
time. Chapters 13–15 emit the real runtime's **production shape**:
paired `stage_start` / `stage_complete` spans plus explicit critical-path
milestones. Do not translate production records back into the synthetic
fixture shape for evaluation. The maintained `easycat latency` command
already reconstructs the waterfall and percentile distribution directly.

```bash
uv run easycat latency PATH --json \
  | uv run python docs/teaching/15-operate-in-production/latency_gate.py \
      --metric 'vad->tts' --percentile p95 --max-ms 2000 --min-samples 5
```

The gate fails separately for an exceeded budget and an insufficient
sample count; both produce JSON that CI can archive. The 2000 ms value is
only an example—set it from your deployment's SLO and baseline. When CI
should collect fresh live samples across provider conditions instead,
use `uv run easycat validate latency --sweep --baseline PATH`.

## Telephony deep-cuts, briefly

`src/easycat/telephony/` has a dozen more modules you haven't
seen: `DTMFAggregator`, `VoicemailDetector`, the `ivr/`,
`screening/`, and `compliance/` subpackages, plus
`TwilioSessionActionExecutor` from ch 14. They're plug-ins to
the same `Session` you've run since chapter 5.

## Try breaking it

1. Run `manager_probe.py`. Why are both interrupted-start slots reusable
   even though the manager never calls `stop()` on either interrupted
   object? Why does the failing `stop_all()` session not prevent its peer
   from stopping?
2. Compare scoped production JSON reports from
   `uv run easycat doctor --provider openai --environment production --json`
   with `OPENAI_API_KEY` unset and set. Which rows appear or change?
3. Pipe a production bundle's `easycat latency --json` report into
   `latency_gate.py`. Trigger both `over_budget` and
   `insufficient_samples`; why should CI distinguish them?
4. Run `postmortem_probe.py`. Why is `append` absent both before and
   after stop even though the backend type changes? Which object keeps
   its identity, and which object is replaced?

<!-- BEGIN auto:practice-handoff -->
## Practice and self-check

Work through [the chapter exercises](./EXERCISES.md), then try their closing
self-check from memory. If an answer is weak, rerun the hardware-free
checkpoint or revisit the section that owns the gap.
<!-- END auto:practice-handoff -->

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
