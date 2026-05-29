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

> **Minimum to skip the ladder:** chapter 6 (the streaming-agent
> surface — that's the concept the bridge layer abstracts).
> Chapter 13 is the natural lead-in but not strictly required;
> read its "one code change per axis" section first if you skip
> the rest.

## Diff from chapter 13

- **Added:** a hand-rolled `MyWorkflow` class with
  `on_user_turn(text, *, recorder, cancel_token)` (deep mode);
  the `auto_adapt_agent()` → `BridgeAdapterShim` flow; the five
  `SessionAction` types and their executors; the four output
  processors (`MarkdownStripProcessor`, `PhoneticReplacementProcessor`,
  `PauseProcessor`, custom); `mcp_servers=[...]` config entry.
- **Modified:** `EasyConfig(agent=...)` now points at a
  hand-rolled workflow, not an `agents.Agent(...)` from a
  framework.
- **Removed:** dependence on the OpenAI Agents SDK as an agent
  surface. (It still works — chapter 13 used it — but this
  chapter shows you don't need any framework at all.)

<!-- BEGIN auto:diff prev=13-swap-providers-and-transports src=main.py -->
<details>
<summary>Full unified diff vs <code>13-swap-providers-and-transports/main.py</code> (auto-generated)</summary>

```diff
--- docs/teaching/13-swap-providers-and-transports/main.py
+++ docs/teaching/14-bring-your-own-agent/main.py
@@ -1,134 +1,156 @@
-"""Chapter 13 — swap providers AND transports.
+"""Chapter 14 — bring your own agent via GenericWorkflowBridge.
 
-One driver. Two orthogonal axes. Six combinations:
+Chapter 13 handed ``agents.Agent(...)`` to ``EasyConfig(agent=...)``.
+Under the hood, ``create_session`` wrapped it in an
+``OpenAIAgentsBridge`` so the runtime could drive it. This chapter
+drops the OpenAI Agents SDK and plugs in a plain async class — the
+same Session code, a different brain.
 
-                Local     WebRTC     Twilio
-  openai         ✓          ✓         ✓
-  deepgram-eleven ✓         ✓         ✓
+Three things this script demonstrates:
 
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
-    For WebRTC: --extra webrtc
-    For Twilio: --extra telephony
-    OPENAI_API_KEY (always)
-    DEEPGRAM_API_KEY, ELEVENLABS_API_KEY (for deepgram-eleven mix)
-    TWIML/Twilio credentials (for twilio transport)
+    export OPENAI_API_KEY=...
 """
 
 from __future__ import annotations
 
-import argparse
 import asyncio
 import os
 import time
+from collections.abc import AsyncIterator
 from pathlib import Path
+
+from openai import AsyncOpenAI
 
 from easycat import (
     EasyConfig,
     LocalTransportConfig,
+    MarkdownStripProcessor,
+    PauseProcessor,
+    PhoneticReplacementProcessor,
     attach_runtime_feedback,
     create_session,
     export_debug_bundle,
     wait_for_shutdown_signal,
 )
+from easycat.cancel import CancelToken
+from easycat.integrations.agents import GenericWorkflowBridge
+from easycat.session.actions import CoreSessionActionExecutor, EndCallAction, SessionActions
 
+MODEL = "gpt-4o-mini"
 RUNS_DIR = Path(__file__).parent / "runs"
 
 
-def build_agent() -> object:
-    """Simple OpenAI-Agents-SDK agent. Provider-agnostic — the agent
-    doesn't know or care which STT/TTS/transport is wired."""
-    from agents import Agent  # type: ignore[import-untyped]
+class MyWorkflow:
+    """Our brain. No framework — just async + OpenAI chat completions.
 
-    return Agent(
-        name="assistant",
-        instructions="You are a helpful voice assistant. Keep replies brief.",
-    )
+    Deep mode is opted into by the signature: as long as
+    ``on_user_turn`` names a ``recorder`` parameter, the bridge runs
+    us in deep mode and wires ``cancel_token`` through. We don't
+    actually need the recorder here (we aren't journalling tool
+    calls), but naming it is the switch.
+    """
 
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
 
-def transport_config(name: str):
-    if name == "local":
-        return LocalTransportConfig()
-    if name == "webrtc":
-        # Requires `uv sync --extra webrtc`. The browser client connects via
-        # SDP offer/answer; see `examples/webrtc_server.py` for the HTTP
-        # signalling endpoint that pairs with WebRTCTransport.
-        from easycat import WebRTCTransportConfig
+    async def on_user_turn(
+        self,
+        text: str,
+        *,
+        recorder,  # AgentRecorder — unused here, but names the deep mode switch
+        cancel_token: CancelToken | None,
+    ) -> AsyncIterator[str]:
+        self._history.append({"role": "user", "content": text})
 
-        return WebRTCTransportConfig()
-    if name == "twilio":
-        # Requires `uv sync --extra telephony`. A live phone call connects
-        # via Twilio Media Streams over WebSocket; see
-        # `examples/twilio_app.py` for the Flask app that wires this up.
-        from easycat.transports.twilio_media import TwilioTransportConfig
+        # Toy intent check; a real app would route via tool calls.
+        if any(w in text.lower() for w in ("bye", "hang up", "goodbye")):
+            # Ask the session to stop after this turn finishes speaking.
+            self._actions.enqueue(EndCallAction(reason="user requested hang-up"))
+            reply = "Sure, ending the call. Goodbye."
+            self._history.append({"role": "assistant", "content": reply})
+            yield reply
+            return
 
-        return TwilioTransportConfig()
-    raise SystemExit(f"Unknown transport: {name}")
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
+        stream = await self._client.chat.completions.create(
+            model=MODEL, messages=self._history, stream=True
+        )
+        full = ""
+        async for chunk in stream:
+            if cancel_token is not None and cancel_token.is_cancelled:
+                break
+            delta = chunk.choices[0].delta.content or ""
+            if not delta:
+                continue
+            full += delta
+            yield delta  # the bridge wraps each chunk as a text_delta event
+        if full:
+            self._history.append({"role": "assistant", "content": full})
 
 
 async def main() -> None:
-    ap = argparse.ArgumentParser()
-    ap.add_argument("--provider-mix", choices=("openai", "deepgram-eleven"), default="openai")
-    ap.add_argument("--transport", choices=("local", "webrtc", "twilio"), default="local")
-    args = ap.parse_args()
-
     if not os.getenv("OPENAI_API_KEY"):
         raise SystemExit("Set OPENAI_API_KEY.")
 
-    tag = f"{args.provider_mix}-{args.transport}"
-    print(f"=== {tag} ===")
+    client = AsyncOpenAI()
+    actions = SessionActions()  # shared: workflow enqueues, session drains
+    workflow = MyWorkflow(client, actions)
+    bridge = GenericWorkflowBridge(workflow)
+    assert bridge.deep_mode, "deep mode required for mid-turn interruption"
 
-    mix = provider_mix(args.provider_mix)
+    # A tiny pronunciation pipeline. Processors run serially on every
+    # committed assistant utterance before the text reaches TTS; a
+    # raise in one is logged and the next runs (fail-open).
+    processors = [
+        MarkdownStripProcessor(),
+        PhoneticReplacementProcessor({"easycat": "ee zee cat"}),
+        # 120 ms pause between digit groups in a phone number.
+        PauseProcessor(pattern=r"\b\d{3}[-. ]?\d{3}[-. ]?\d{4}\b", pause_ms=120),
+    ]
+
     config = EasyConfig(
         openai_api_key=os.environ["OPENAI_API_KEY"],
-        agent=build_agent(),
-        transport=transport_config(args.transport),
-        debug="light",  # journal must be on so export_debug_bundle works
-        **mix,
+        agent=bridge,  # ← the whole point of this chapter
+        transport=LocalTransportConfig(),
+        stt="openai",
+        tts="openai",
+        output_processors=processors,
+        session_actions=actions,
+        action_executors=(CoreSessionActionExecutor(),),
+        debug="light",
     )
     session = create_session(config)
     attach_runtime_feedback(session)
 
     await session.start()
-    print("Session started. Talk (or connect a client).  Ctrl-C to stop.\n")
+    print("Talk to your custom agent. Say 'goodbye' to have it hang up.\n")
     try:
         await wait_for_shutdown_signal(session)
     finally:
         RUNS_DIR.mkdir(exist_ok=True)
-        path = RUNS_DIR / f"ch13-{tag}-{int(time.time())}.bundle"
+        path = RUNS_DIR / f"ch14-bridge-{int(time.time())}.bundle"
         try:
             export_debug_bundle(session, path, overwrite=True)
             print(f"Wrote bundle → {path.relative_to(Path.cwd())}")
@@ -137,4 +159,7 @@
 
 
 if __name__ == "__main__":
-    asyncio.run(main())
+    try:
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
    calls), but naming it is the switch.
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
        recorder,  # AgentRecorder — unused here, but names the deep mode switch
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
            model=MODEL, messages=self._history, stream=True
        )
        full = ""
        async for chunk in stream:
            if cancel_token is not None and cancel_token.is_cancelled:
                break
            delta = chunk.choices[0].delta.content or ""
            if not delta:
                continue
            full += delta
            yield delta  # the bridge wraps each chunk as a text_delta event
        if full:
            self._history.append({"role": "assistant", "content": full})
```
<!-- END auto:snippet -->

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
