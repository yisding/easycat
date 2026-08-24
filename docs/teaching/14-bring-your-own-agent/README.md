# Chapter 14 — Bring your own agent

<!-- BEGIN auto:navigation -->
**Progress: 15 of 16** · [← Chapter 13 — Swap Providers AND Transports](../13-swap-providers-and-transports/) · [Ladder index](../) · [Progress worksheet](../PROGRESS.md) · [Exercises](./EXERCISES.md) · [Chapter 15 — Operate in production →](../15-operate-in-production/)
<!-- END auto:navigation -->

> Chapter 13's `build_agent()` returned an `agents.Agent(...)` from
> the OpenAI Agents SDK. `create_session` silently wrapped it in an
> `OpenAIAgentsBridge`. In this chapter we drop the framework
> entirely and plug in a plain async class — same Session code,
> different brain.

<!-- BEGIN auto:spaced-retrieval -->
## Recall before reading

> **Following the ladder? Spaced retrieval — Chapter 12 — Evals + the Latency Budget**
>
> Close earlier chapters and answer from memory before reading further. If this
> chapter is your starting point, skip this block.
>
> **Answer from memory:**
>
> Which bundle controls P95, and how far should P95 move when that bundle is removed?
>
> After recording your answer, explain one way `small-sample P95 sensitivity` changes how you
> reason about `plain workflow bridge contract`. Keep the first answer visible.
>
> **Check only after answering:**
>
> ```bash
> uv run python docs/teaching/12-evals-and-latency/p95_sensitivity_probe.py
> ```
>
> Cite one observed field, measurement, or behavior; repair only the part your
> evidence disproved.
<!-- END auto:spaced-retrieval -->

<!-- BEGIN auto:offline-checkpoint -->
> **Hardware-free checkpoint:** prove `plain workflow bridge contract` without a microphone,
> speakers, or provider credentials:
>
> **Predict first:** Can a plain workflow yield both reply text and a session action, and which
> bridge mode should it report?
>
> ```bash
> uv run python docs/teaching/14-bring-your-own-agent/workflow_state_probe.py
> ```
>
> **Evidence to find:** `MyWorkflow` yields a reply plus `EndCallAction`; the bridge reports deep
> mode.
>
> **Explain the result:** Trace the workflow yield into both reply and action without
> framework-specific state.
>
> [See all 16 checkpoints](../#hardware-free-checkpoint-spine).
<!-- END auto:offline-checkpoint -->

## Prerequisites

- [Chapter 13.](../13-swap-providers-and-transports/)
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

> **Minimum to skip the ladder:** chapter 6 (the streaming-agent
> surface — that's the concept the bridge layer abstracts).
> Chapter 13 is the natural lead-in but not strictly required;
> read its "one code change per axis" section first if you skip
> the rest.

## Diff from chapter 13

- **Added:** a hand-rolled `MyWorkflow` class with
  `on_user_turn(text, *, recorder, cancel_token)` (deep mode);
  the `auto_adapt_agent()` → bridge flow; session-action wiring with
  `SessionActions`, `EndCallAction`, and `CoreSessionActionExecutor`; the
  three-item pronunciation chain (`MarkdownStripProcessor`,
  `PhoneticReplacementProcessor`, and `PauseProcessor`);
  `mcp_servers=[...]` config entry.
- **Modified:** `EasyConfig(agent=...)` now points at a
  hand-rolled workflow, not an `agents.Agent(...)` from a
  framework.
- **Removed:** dependence on the OpenAI Agents SDK as an agent
  surface. (It still works — chapter 13 used it — but this
  chapter shows you don't need any framework at all.)

<!-- BEGIN auto:diff prev=13-swap-providers-and-transports src=main.py trim_blank_context=true -->
<details>
<summary>Full unified diff vs <code>13-swap-providers-and-transports/main.py</code> (auto-generated)</summary>

```diff
--- docs/teaching/13-swap-providers-and-transports/main.py
+++ docs/teaching/14-bring-your-own-agent/main.py
@@ -1,37 +1,26 @@
-"""Chapter 13 — swap providers AND transports.
-
-One driver. Two orthogonal axes. Six combinations:
-
-                Local     WebRTC     Twilio
-  openai         ✓          ✓         ✓
-  deepgram-eleven ✓         ✓         ✓
-
-Only the **two Local cells** run out of the box — WebRTC and
-Twilio need a connected client (browser or phone call) and are
-covered by the respective examples. The *code shape* is the
-same: `EasyConfig(transport=...)` is the only line that
-changes.
-
-    # Axis 1 — swap providers (same transport)
-    uv run python docs/teaching/13-swap-providers-and-transports/main.py \\
-        --provider-mix openai --transport local
-    uv run python docs/teaching/13-swap-providers-and-transports/main.py \\
-        --provider-mix deepgram-eleven --transport local
-
-    # Axis 2 — swap transport (same providers)
-    uv run python docs/teaching/13-swap-providers-and-transports/main.py \\
-        --provider-mix openai --transport webrtc
-    uv run python docs/teaching/13-swap-providers-and-transports/main.py \\
-        --provider-mix openai --transport twilio
+"""Chapter 14 — bring your own agent via GenericWorkflowBridge.
+
+Chapter 13 handed ``agents.Agent(...)`` to ``EasyConfig(agent=...)``.
+Under the hood, ``create_session`` wrapped it in an
+``OpenAIAgentsBridge`` so the runtime could drive it. This chapter
+drops the OpenAI Agents SDK and plugs in a plain async class — the
+same Session code, a different brain.
+
+Three things this script demonstrates:
+
+1. A ``GenericWorkflowBridge`` in *deep mode* — our workflow gets a
+   ``cancel_token`` alongside the user text, so we can stop the LLM
+   stream the instant the user barges in.
+2. Session actions: the workflow enqueues an ``EndCallAction`` when
+   the user says goodbye. ``CoreSessionActionExecutor`` dispatches
+   it and the session stops after the current turn.
+3. Output processors: a three-item pronunciation chain (strip
+   markdown, fix one name, pause on phone numbers) that runs on
+   every committed assistant utterance before it reaches TTS.

 Dependencies:
     uv sync --extra quickstart --group dev
-    For deepgram-eleven: --extra deepgram --extra elevenlabs
-    For WebRTC: --extra webrtc
-    For Twilio: --extra telephony
-    OPENAI_API_KEY (always)
-    DEEPGRAM_API_KEY, ELEVENLABS_API_KEY (for deepgram-eleven mix)
-    TWIML/Twilio credentials (for twilio transport)
+    export OPENAI_API_KEY=...
     uv run easycat doctor
     uv run easycat doctor --env-file .env         # if keys live in .env
     uv run easycat doctor --env-file .env --json  # for parseable checks
@@ -40,22 +29,34 @@

 from __future__ import annotations

-import argparse
 import asyncio
 import os
 import shlex
 import time
+from collections.abc import AsyncIterator
 from pathlib import Path
+from typing import TYPE_CHECKING

 from easycat import (
     EasyConfig,
     LocalTransportConfig,
+    MarkdownStripProcessor,
     attach_runtime_feedback,
     create_session,
+    default_pronunciation_processors,
     export_debug_bundle,
     wait_for_shutdown_signal,
 )
-
+from easycat.cancel import CancelToken
+from easycat.integrations.agents import GenericWorkflowBridge
+from easycat.integrations.agents.base import AgentRecorder, CancellationMode
+from easycat.llm_output_processing import LLMOutputProcessor
+from easycat.session.actions import CoreSessionActionExecutor, EndCallAction, SessionActions
+
+if TYPE_CHECKING:
+    from openai import AsyncOpenAI
+
+MODEL = "gpt-5.6-luna"
 RUNS_DIR = Path(__file__).parent / "runs"


@@ -75,115 +76,192 @@
     )


-def build_agent() -> object:
-    """Simple OpenAI-Agents-SDK agent. Provider-agnostic — the agent
-    doesn't know or care which STT/TTS/transport is wired."""
-    from agents import Agent  # type: ignore[import-untyped]
-
-    return Agent(
-        name="assistant",
-        instructions="You are a helpful voice assistant. Keep replies brief.",
+def pronunciation_command(path: Path) -> str:
+    """Inspect the scheduler's provider-ready pronunciation payloads."""
+    return shlex.join(
+        [
+            "uv",
+            "run",
+            "easycat",
+            "journal",
+            "grep",
+            str(_display_path(path)),
+            "--query",
+            "tts_payload_prepared",
+            "--json",
+        ]
     )


-def transport_config(name: str):
-    if name == "local":
-        return LocalTransportConfig()
-    if name == "webrtc":
-        # Requires `uv sync --extra webrtc --group dev`. The browser client connects via
-        # SDP offer/answer; see `examples/webrtc_server.py` for the HTTP
-        # signalling endpoint that pairs with WebRTCTransport.
-        from easycat import WebRTCTransportConfig
-
-        return WebRTCTransportConfig()
-    if name == "twilio":
-        # Requires `uv sync --extra telephony --group dev`. A live phone call connects
-        # via Twilio Media Streams over WebSocket; see
-        # `examples/twilio_app.py` for the FastAPI app that wires this up.
-        from easycat.transports.twilio_media import TwilioTransportConfig
-
-        return TwilioTransportConfig()
-    raise SystemExit(f"Unknown transport: {name}")
-
-
-def telephony_config(name: str):
-    """Wire Twilio-backed session actions for the phone transport."""
-    if name != "twilio":
-        return None
-    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
-    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
-    if not account_sid or not auth_token:
-        raise SystemExit("Twilio actions need TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN.")
-
-    from easycat import TelephonyConfig, TwilioSessionActionConfig
-
-    return TelephonyConfig(
-        twilio_actions=TwilioSessionActionConfig(
-            account_sid=account_sid,
-            auth_token=auth_token,
+def build_output_processors() -> list[LLMOutputProcessor]:
+    """Build the chapter's pronunciation stack from the public factory."""
+    return [
+        MarkdownStripProcessor(),
+        *default_pronunciation_processors(
+            name_pronunciations={"easycat": "ee zee cat"},
+            phone_ellipsis_count=1,
+        ),
+    ]
+
+
+class MyWorkflow:
+    """Our brain. No framework — just async + OpenAI chat completions.
+
+    Deep mode is opted into by the signature: as long as
+    ``on_user_turn`` names a ``recorder`` parameter, the bridge runs
+    us in deep mode and wires ``cancel_token`` through. We don't
+    actually need the recorder here (we aren't journalling tool
+    calls), but naming it is the switch. The history hooks below
+    keep our private message list aligned with what the caller heard.
+    """
+
+    def __init__(self, client: AsyncOpenAI, actions: SessionActions) -> None:
+        self._client = client
+        self._actions = actions
+        self._history: list[dict] = [
+            {
+                "role": "system",
+                "content": (
+                    "You are a helpful voice assistant. Keep replies under two sentences. "
+                    "If the user says goodbye or asks to hang up, reply with a brief "
+                    "farewell — the transport layer will hang up for you."
+                ),
+            }
+        ]
+
+    async def on_user_turn(
+        self,
+        text: str,
+        *,
+        recorder: AgentRecorder,  # unused here, but names the deep mode switch
+        cancel_token: CancelToken | None,
+    ) -> AsyncIterator[str]:
+        self._history.append({"role": "user", "content": text})
+
+        # Toy intent check; a real app would route via tool calls.
+        if any(w in text.lower() for w in ("bye", "hang up", "goodbye")):
+            # Ask the session to stop after this turn finishes speaking.
+            self._actions.enqueue(EndCallAction(reason="user requested hang-up"))
+            reply = "Sure, ending the call. Goodbye."
+            self._history.append({"role": "assistant", "content": reply})
+            yield reply
+            return
+
+        stream = await self._client.chat.completions.create(
+            model=MODEL,
+            reasoning_effort="none",
+            messages=self._history,
+            stream=True,
         )
-    )
-
-
-def provider_mix(name: str) -> dict:
-    """Return the STT/TTS strings for the named mix.
-
-    All values are string shortcuts — ``EasyConfig.__post_init__``
-    parses them into concrete config objects via the factory.
-    """
-    if name == "openai":
-        return {"stt": "openai", "tts": "openai"}
-    if name == "deepgram-eleven":
-        if not os.getenv("DEEPGRAM_API_KEY") or not os.getenv("ELEVENLABS_API_KEY"):
-            raise SystemExit("deepgram-eleven mix needs DEEPGRAM_API_KEY + ELEVENLABS_API_KEY.")
-        return {"stt": "deepgram/nova-2", "tts": "elevenlabs"}
-    raise SystemExit(f"Unknown provider mix: {name}")
+        full = ""
+        try:
+            async with stream as response_stream:
+                async for chunk in response_stream:
+                    if cancel_token is not None and cancel_token.is_cancelled:
+                        break
+                    delta = chunk.choices[0].delta.content or ""
+                    if not delta:
+                        continue
+                    full += delta
+                    yield delta  # the bridge wraps each chunk as a text_delta event
+        finally:
+            # BridgeTemplate closes this generator on barge-in. Commit the
+            # delivered prefix before apply_interruption rewrites it to what
+            # the caller actually heard.
+            if full:
+                self._history.append({"role": "assistant", "content": full})
+
+    def apply_interruption(self, delivered_text: str, mode: CancellationMode) -> None:
+        """Rewrite private history to the portion the caller actually heard."""
+        suffix = "..." if delivered_text and mode is CancellationMode.IMMEDIATE_STOP else ""
+        self.replace_last_assistant_text(f"{delivered_text}{suffix}")
+
+    def replace_last_assistant_text(self, text: str) -> None:
+        """Let interruption and Markdown cleanup update private history."""
+        for message in reversed(self._history):
+            if message["role"] == "assistant":
+                message["content"] = text
+                return
+            if message["role"] == "user":
+                # No assistant output was generated for this turn. Do not
+                # rewrite the previous turn's already-committed response.
+                return
+
+    def snapshot_state(self) -> dict[str, object]:
+        """Allowlist privacy-safe workflow metadata for debug artifacts."""
+        last_assistant = next(
+            (
+                str(message["content"])
+                for message in reversed(self._history)
+                if message["role"] == "assistant"
+            ),
+            "",
+        )
+        return {
+            "message_count": len(self._history),
+            "history_roles": [str(message["role"]) for message in self._history],
+            "last_assistant_chars": len(last_assistant),
+            "session_action_pending": self._actions.has_pending,
+        }


 async def main() -> None:
-    ap = argparse.ArgumentParser()
-    ap.add_argument("--provider-mix", choices=("openai", "deepgram-eleven"), default="openai")
-    ap.add_argument("--transport", choices=("local", "webrtc", "twilio"), default="local")
-    args = ap.parse_args()
+    from openai import AsyncOpenAI

     if not os.getenv("OPENAI_API_KEY"):
         raise SystemExit("Set OPENAI_API_KEY.")

-    tag = f"{args.provider_mix}-{args.transport}"
-    print(f"=== {tag} ===")
-
-    mix = provider_mix(args.provider_mix)
-    config = EasyConfig(
-        agent=build_agent(),
-        transport=transport_config(args.transport),
-        telephony=telephony_config(args.transport),
-        debug="light",  # journal must be on so export_debug_bundle works
-        **mix,
-    )
-    session = create_session(config)
-    attach_runtime_feedback(session)
-
+    # The custom workflow owns this client; EasyCat only owns providers and
+    # transports it creates from EasyConfig. Keep the caller-owned scope outer.
+    async with AsyncOpenAI() as client:
+        actions = SessionActions()  # shared: workflow enqueues, session drains
+        workflow = MyWorkflow(client, actions)
+        bridge = GenericWorkflowBridge(workflow)
+        assert bridge.deep_mode, "deep mode required for mid-turn interruption"
+
+        # A tiny pronunciation pipeline. Processors run serially on every
+        # committed assistant utterance before the text reaches TTS; a
+        # raise in one is logged and the next runs (fail-open).
+        processors = build_output_processors()
+
+        config = EasyConfig(
+            agent=bridge,  # ← the whole point of this chapter
+            transport=LocalTransportConfig(),
+            stt="openai",
+            tts="openai",
+            output_processors=processors,
+            session_actions=actions,
+            action_executors=(CoreSessionActionExecutor(),),
+            debug="light",
+        )
+        session = create_session(config)
+        attach_runtime_feedback(session)
+
+        try:
+            async with session:
+                print("Talk to your custom agent. Say 'goodbye' to have it hang up.\n")
+                await wait_for_shutdown_signal(session)
+        finally:
+            # Session exit preserves the read-only journal view. Export while
+            # the custom workflow's client is still in its separately owned
+            # scope, including when shutdown arrives through cancellation.
+            RUNS_DIR.mkdir(exist_ok=True)
+            path = RUNS_DIR / f"ch14-bridge-{int(time.time())}.bundle"
+            try:
+                export_debug_bundle(session, path, overwrite=True)
+                print(f"Wrote bundle → {_display_path(path)}")
+                human_command, json_command = measurement_commands(path)
+                print("Measure this production-shaped bundle directly:")
+                print(f"  {human_command}")
+                print(f"  {json_command}")
+                print("Inspect its provider-ready pronunciation payloads:")
+                print(f"  {pronunciation_command(path)}")
+            except Exception as exc:  # noqa: BLE001 — teaching script
+                print(f"(no bundle written: {exc})")
+
+
+if __name__ == "__main__":
     try:
-        async with session:
-            print("Session started. Talk (or connect a client).  Ctrl-C to stop.\n")
-            await wait_for_shutdown_signal(session)
-    finally:
-        # Context exit force-stops cancellation paths. The normal signal helper
-        # already stopped gracefully, so that second stop is an idempotent no-op.
-        # Export from the preserved read-only postmortem view even when shutdown
-        # reached this scope through cancellation.
-        RUNS_DIR.mkdir(exist_ok=True)
-        path = RUNS_DIR / f"ch13-{tag}-{int(time.time())}.bundle"
-        try:
-            export_debug_bundle(session, path, overwrite=True)
-            print(f"Wrote bundle → {_display_path(path)}")
-            human_command, json_command = measurement_commands(path)
-            print("Measure this production-shaped bundle directly:")
-            print(f"  {human_command}")
-            print(f"  {json_command}")
-        except Exception as exc:  # noqa: BLE001 — teaching script
-            print(f"(no bundle written: {exc})")
-
-
-if __name__ == "__main__":
-    asyncio.run(main())
+        asyncio.run(main())
+    except KeyboardInterrupt:
+        pass
```

</details>
<!-- END auto:diff -->

## Run

```bash
uv run python docs/teaching/14-bring-your-own-agent/main.py
```

Talk to it. Say **"goodbye"** to watch the session-action flow fire
— the workflow enqueues `EndCallAction`, `CoreSessionActionExecutor`
dispatches it, the session stops after the current turn. On exit the
script exports its production-shaped bundle and prints both human and
JSON `easycat latency` commands for that exact path.

## Caller-owned workflow dependencies

`Session` owns the STT, TTS, VAD, transport, and storage it constructs
from `EasyConfig`. It does not automatically own arbitrary objects
captured inside your workflow. `GenericWorkflowBridge` does not infer
that `MyWorkflow._client` should be closed, so the script gives the
caller-owned `AsyncOpenAI` client a separate outer scope:

```python
async with AsyncOpenAI() as client:
    workflow = MyWorkflow(client, actions)
    session = create_session(config)
    async with session:
        await wait_for_shutdown_signal(session)

    export_debug_bundle(session, path, overwrite=True)
```

The order is intentional: stop the session while its workflow client
is still available, export through the preserved postmortem journal,
then close the caller-owned client. A bridge adapts behavior; it does
not silently transfer ownership of everything reachable from the
workflow object.

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
                        ExternalAgentBridge
                                    │
                                    ▼
                              Session.run()
```

Every `agent=` value the config accepts is routed through
`auto_adapt_agent()`, which picks the right concrete bridge. EasyCat then wraps
the result in `AgentRunner` unless wrapping is disabled, so Session can drive it
through one bridge surface. So the "Session orchestration" in chapters 2-13 has
always been framework-agnostic; bridges are the seam.

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

Deep mode passes the session's `cancel_token` into your workflow, so
you can stop upstream work as soon as the caller barges in. Shallow
mode (`on_user_turn(text)`) does not expose that token or the recorder
to your code. The bridge still stops forwarding streamed chunks after
it observes cancellation, and the session cancels queued TTS audio,
but an opaque workflow cannot safely reconcile its own history unless
it implements an explicit `apply_interruption(...)` hook.

When that state notification is unsupported, the session writes
`assistant_interruption_notified` with `notified: false`. The barge-in
itself is recorded separately as `control_signal_cause` with
`cause: barge_in`. There is no shallow-mode-specific control signal.

The teaching block above is the essence. The real `MyWorkflow`
in `main.py` adds history, the system prompt, and the action
enqueue — but the deep-mode signature is unchanged:

<!-- BEGIN auto:snippet src=main.py symbol=MyWorkflow -->
```python
class MyWorkflow:
    """Our brain. No framework — just async + OpenAI chat completions.

    Deep mode is opted into by the signature: as long as
    ``on_user_turn`` names a ``recorder`` parameter, the bridge runs
    us in deep mode and wires ``cancel_token`` through. We don't
    actually need the recorder here (we aren't journalling tool
    calls), but naming it is the switch. The history hooks below
    keep our private message list aligned with what the caller heard.
    """

    def __init__(self, client: AsyncOpenAI, actions: SessionActions) -> None:
        self._client = client
        self._actions = actions
        self._history: list[dict] = [
            {
                "role": "system",
                "content": (
                    "You are a helpful voice assistant. Keep replies under two sentences. "
                    "If the user says goodbye or asks to hang up, reply with a brief "
                    "farewell — the transport layer will hang up for you."
                ),
            }
        ]

    async def on_user_turn(
        self,
        text: str,
        *,
        recorder: AgentRecorder,  # unused here, but names the deep mode switch
        cancel_token: CancelToken | None,
    ) -> AsyncIterator[str]:
        self._history.append({"role": "user", "content": text})

        # Toy intent check; a real app would route via tool calls.
        if any(w in text.lower() for w in ("bye", "hang up", "goodbye")):
            # Ask the session to stop after this turn finishes speaking.
            self._actions.enqueue(EndCallAction(reason="user requested hang-up"))
            reply = "Sure, ending the call. Goodbye."
            self._history.append({"role": "assistant", "content": reply})
            yield reply
            return

        stream = await self._client.chat.completions.create(
            model=MODEL,
            reasoning_effort="none",
            messages=self._history,
            stream=True,
        )
        full = ""
        try:
            async with stream as response_stream:
                async for chunk in response_stream:
                    if cancel_token is not None and cancel_token.is_cancelled:
                        break
                    delta = chunk.choices[0].delta.content or ""
                    if not delta:
                        continue
                    full += delta
                    yield delta  # the bridge wraps each chunk as a text_delta event
        finally:
            # BridgeTemplate closes this generator on barge-in. Commit the
            # delivered prefix before apply_interruption rewrites it to what
            # the caller actually heard.
            if full:
                self._history.append({"role": "assistant", "content": full})

    def apply_interruption(self, delivered_text: str, mode: CancellationMode) -> None:
        """Rewrite private history to the portion the caller actually heard."""
        suffix = "..." if delivered_text and mode is CancellationMode.IMMEDIATE_STOP else ""
        self.replace_last_assistant_text(f"{delivered_text}{suffix}")

    def replace_last_assistant_text(self, text: str) -> None:
        """Let interruption and Markdown cleanup update private history."""
        for message in reversed(self._history):
            if message["role"] == "assistant":
                message["content"] = text
                return
            if message["role"] == "user":
                # No assistant output was generated for this turn. Do not
                # rewrite the previous turn's already-committed response.
                return

    def snapshot_state(self) -> dict[str, object]:
        """Allowlist privacy-safe workflow metadata for debug artifacts."""
        last_assistant = next(
            (
                str(message["content"])
                for message in reversed(self._history)
                if message["role"] == "assistant"
            ),
            "",
        )
        return {
            "message_count": len(self._history),
            "history_roles": [str(message["role"]) for message in self._history],
            "last_assistant_chars": len(last_assistant),
            "session_action_pending": self._actions.has_pending,
        }
```
<!-- END auto:snippet -->

Deep mode exposes state hooks; it does not decide which of your state is
safe to persist. `GenericWorkflowBridge` prefers a workflow's
`snapshot_state()` dictionary when it writes interruption artifacts. Without
that hook it falls back to serializing the workflow's `__dict__`, which is a
much broader boundary and can pull caller-owned client or queue objects into
the payload as string representations.

`MyWorkflow.snapshot_state()` therefore returns an intentional metadata-only
allowlist: message count, role sequence, last-assistant character count, and
whether a session action is pending. It omits prompt text, user text, the
`AsyncOpenAI` client, and the `SessionActions` object. The explicit dictionary
is author-owned artifact data; do not put credentials or sensitive message
content in it and assume another redaction pass will rescue it.

Run the provider-free state-boundary probe:

```bash
uv run python docs/teaching/14-bring-your-own-agent/workflow_state_probe.py
```

It takes the real workflow's goodbye branch, shows the queued
`EndCallAction`, and prints both the public bridge snapshot and the exact
metadata payload used for interruption artifacts. The payload contains the
allowlisted values, not reachable Python objects.

### 2. Session actions

Tools inside your agent can't reach the live `Session` — they
live inside the framework's own event loop. Instead, they enqueue
typed actions on a shared `SessionActions` queue. The session
drains the queue after the turn, dispatching each action to the
first executor that claims it via `supports()`.

```python
actions.enqueue(EndCallAction(reason="user requested hang-up"))
```

The seven action types:

| Action | Typical executor |
|---|---|
| `EndCallAction` | `CoreSessionActionExecutor` (stops the session) |
| `TransferCallAction` | `TwilioSessionActionExecutor` (REST dial) |
| `SendDTMFAction` | `TwilioSessionActionExecutor` (IVR) |
| `SendSMSAction` | `TwilioSessionActionExecutor` |
| `AddToDNCAction` | `CoreSessionActionExecutor` (updates the DNC store) |
| `RemoveFromDNCAction` | `CoreSessionActionExecutor` (updates the DNC store) |
| `CustomAction` | whatever you write |

The Twilio executor lives in `src/easycat/telephony/session_actions.py`
and needs `call_sid` off the transport — it's only useful on the
Twilio transport. The core executor is provider-neutral and handles
`EndCallAction`, `AddToDNCAction`, and `RemoveFromDNCAction`.

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
| `PauseProcessor` | Regex-match → insert provider-compatible ellipsis cues (or opt-in SSML breaks) |
| `LLMOutputProcessor` | Protocol — roll your own |

Processors run serially, fail-open: an exception in one is logged
and the next one still runs. The Session applies the full chain to
the **TTS payload**. Only `strip_markdown` is also written back to
the bridge's chat history (via `replace_last_assistant_text`), so
phonetic replacements and pauses shape what the user *hears* but
the LLM still sees the original text next turn.

`default_pronunciation_processors(...)` is a factory that wires the
common stack if you don't want to hand-build the list. It always adds
phone-number pauses and adds phonetic swaps when you pass a non-empty
`name_pronunciations` mapping.

The scheduler emits one `tts_payload_prepared` journal record for each
provider-ready payload. Its `processors` list names the configured
stack, while `changed`, the original/prepared formats, and
`ssml_downgraded` describe the combined result. There are no
per-processor `output_processor.*` records or intermediate strings.
The default ellipsis style stays plain text, so all four bundled providers
receive the pacing cue and `ssml_downgraded` remains false. The provider still
decides its exact timing. For an exact `pause_ms`, opt into `style="ssml"` and
use a provider that advertises native SSML support; bundled providers currently
strip those tags.

## MCP (a short sidebar)

MCP — Model Context Protocol — servers are first-class:

```python
EasyConfig(
    agent=my_agent,
    mcp_servers=["stdio://path/to/mcp-server", "sse://localhost:4000"],
)
```

The validator accepts `stdio://`, `sse://`, `http://`, `https://`.
EasyCat forwards the list into `RecorderContext`, and
each bridge injects it into its framework's agent object
(`agent.mcp_servers = [...]` before `run_streamed()`). Shallow-mode
`GenericWorkflowBridge` logs a warning because it has no way to
wire MCP into your hand-rolled workflow — deep mode makes it your
responsibility.

## The bring-your-own-agent ladder

Four rungs, in order of power and effort:

| Rung | You write | You get |
|---|---|---|
| 1. Plain agent | `async run(text) -> str` | `AgentRunner` wraps it: timeout, history, cancellation |
| 2. Shallow workflow | `on_user_turn(text)` | streaming + session actions; session cancels output, state stays opaque unless you add a hook |
| 3. Deep workflow | `on_user_turn(text, *, recorder, cancel_token)` | cooperative upstream cancellation, journaled internals, state hooks |
| 4. Full bridge | a `BridgeTemplate` subclass | custom event grammar, framework state snapshots, atomic interruption mutations |

This chapter's script lives on rung 3. The top rung is for when you
are integrating a *framework* — something with its own event stream
and persistent state — rather than a single workflow object.

### The top rung: a full bridge via `BridgeTemplate`

Subclass `BridgeTemplate`
(`src/easycat/integrations/agents/template.py::BridgeTemplate`). It
owns the boilerplate every bridge repeats — the `invoke()` cursor
lifecycle (including the cancellation-safe cleanup arm), the
four-step atomic interruption journal protocol, scrubbed state
serialization for artifacts, the default `COMMITTABLE_BOUNDARIES`,
and safe no-op history-mutation methods. You implement three
things: event streaming, interruption planning, and a state
snapshot.

```python
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    FrameworkStateSnapshot,
    InterruptionPlan,
)
from easycat.integrations.agents.template import BridgeTemplate


class MyFrameworkBridge(BridgeTemplate):
    def __init__(self, agent) -> None:
        super().__init__(display_name=type(agent).__name__)
        self._agent = agent

    async def stream_events(self, turn_input, recorder, cancel_token):
        async for chunk in self._agent.stream(turn_input.text):
            yield AgentBridgeEvent(kind="text_delta", text=chunk)

    def snapshot_state(self):
        return FrameworkStateSnapshot(
            fields={"history_len": len(self._agent.history)},
            kind="my_framework",
        )

    def _plan_interruption(self, delivered_text, mode):
        return InterruptionPlan(
            mutation_kind="interrupt_truncate",
            pre_state_ref=f"my-pre-{id(self._agent):x}",
            post_state_ref=f"my-post-{id(self._agent):x}",
            framework_instructions={"delivered_text": delivered_text},
        )

    def _apply_planned_mutation(self, plan):
        self._agent.truncate(plan.framework_instructions["delivered_text"])
```

`GenericWorkflowBridge` itself is built on the template — read
`src/easycat/integrations/agents/generic_workflow.py` as the in-tree
reference implementation.

To make `EasyConfig(agent=my_framework_obj)` find your bridge
automatically, register a detector:

```python
from easycat.integrations.agents import register_agent_detector

register_agent_detector(
    lambda obj: isinstance(obj, MyFrameworkAgent),
    lambda obj: MyFrameworkBridge(obj),
)
```

Detectors are consulted by `auto_adapt_agent()` after the
`AgentRunner` unwrap and the bridge passthrough but *before* the
built-in framework branches, so your detector wins over the
built-ins. Registration is programmatic only — call it from your
application or plugin setup code; there is no entry-point or
config-file mechanism.

### The configure_runtime contract

`configure_runtime` is an *optional* extension surface for bridges
that consume session-level `mcp_servers` / `model` / `api_key`
settings. It is deliberately **not** part of the
`ExternalAgentBridge` protocol: the protocol is
`@runtime_checkable`, and declaring an optional method there would
make `isinstance(obj, ExternalAgentBridge)` return `False` for
every bridge that legitimately omits it. The session factory probes
for it with `getattr(bridge, "configure_runtime", None)` (see
`easycat.config._inject_agent_runtime`) and falls back to the
historical private-attribute path when absent.

Bridges that opt in implement this signature:

```python
def configure_runtime(
    self,
    *,
    mcp_servers: list[str] | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> None: ...
```

`mcp_servers` is a list of EasyCat MCP URI strings; `None` means
"leave unchanged". Passing an empty list explicitly clears any
previously configured servers, so a bridge reused across sessions
does not leak the prior list.

## Try breaking it

1. Change `on_user_turn` to `async def on_user_turn(self, text)`
   — drop `recorder` / `cancel_token`. You just demoted to shallow
   mode. Temporarily comment out `apply_interruption` as well so you
   exercise the default shallow contract, then interrupt the bot
   mid-sentence. What stops, and what can no longer be reconciled?
   (Hint: inspect `assistant_interruption_notified` and its `notified`
   field.)
2. Add a `CustomAction` and a 10-line executor that prints it.
   Trigger it from the workflow. How does the journal record the
   action's lifecycle?
3. Say "Call me at 555-867-5309," then run the printed
   `journal grep` command for `tts_payload_prepared`. Which parts of
   the transformation reached the provider, and which SSML timing
   guarantee was downgraded? Try `style="ellipsis"` and compare the
   prepared format.
4. Run `workflow_state_probe.py`, then temporarily add a harmless
   `api_key: "demo"` field to `snapshot_state()`. The explicit snapshot is
   author-owned artifact data, so the probe exposes that field. Remove it;
   the lesson is to allowlist safe metadata, never to test with a real secret.

<!-- BEGIN auto:practice-handoff -->
## Practice and self-check

Work through [the chapter exercises](./EXERCISES.md), then try their closing
self-check from memory. If an answer is weak, rerun the hardware-free
checkpoint or revisit the section that owns the gap.
<!-- END auto:practice-handoff -->

## What's next

[Chapter 15 — Operate in production](../15-operate-in-production/)
takes the single-session demo you've been running since chapter 0
and shows what it takes to run N of them at once: `SessionManager`,
the lifecycle methods, the debugger UI, and the CLI.
