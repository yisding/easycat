"""Chapter 14 — bring your own agent via GenericWorkflowBridge.

Chapter 13 handed ``agents.Agent(...)`` to ``EasyConfig(agent=...)``.
Under the hood, ``create_session`` wrapped it in an
``OpenAIAgentsBridge`` so the runtime could drive it. This chapter
drops the OpenAI Agents SDK and plugs in a plain async class — the
same Session code, a different brain.

Three things this script demonstrates:

1. A ``GenericWorkflowBridge`` in *deep mode* — our workflow gets a
   ``cancel_token`` alongside the user text, so we can stop the LLM
   stream the instant the user barges in.
2. Session actions: the workflow enqueues an ``EndCallAction`` when
   the user says goodbye. ``CoreSessionActionExecutor`` dispatches
   it and the session stops after the current turn.
3. Output processors: a three-item pronunciation chain (strip
   markdown, fix one name, pause on phone numbers) that runs on
   every committed assistant utterance before it reaches TTS.

Dependencies:
    uv sync --extra quickstart --group dev
    export OPENAI_API_KEY=...
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path

from openai import AsyncOpenAI

from easycat import (
    CoreSessionActionExecutor,
    EasyConfig,
    EndCallAction,
    LocalTransportConfig,
    MarkdownStripProcessor,
    PauseProcessor,
    PhoneticReplacementProcessor,
    attach_runtime_feedback,
    create_session,
    export_debug_bundle,
    wait_for_shutdown_signal,
)
from easycat.cancel import CancelToken
from easycat.integrations.agents import GenericWorkflowBridge
from easycat.session.actions import SessionActions

MODEL = "gpt-4o-mini"
RUNS_DIR = Path(__file__).parent / "runs"


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


async def main() -> None:
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("Set OPENAI_API_KEY.")

    client = AsyncOpenAI()
    actions = SessionActions()  # shared: workflow enqueues, session drains
    workflow = MyWorkflow(client, actions)
    bridge = GenericWorkflowBridge(workflow)
    assert bridge.deep_mode, "deep mode required for mid-turn interruption"

    # A tiny pronunciation pipeline. Processors run serially on every
    # committed assistant utterance before the text reaches TTS; a
    # raise in one is logged and the next runs (fail-open).
    processors = [
        MarkdownStripProcessor(),
        PhoneticReplacementProcessor({"easycat": "ee zee cat"}),
        # 120 ms pause between digit groups in a phone number.
        PauseProcessor(pattern=r"\b\d{3}[-. ]?\d{3}[-. ]?\d{4}\b", pause_ms=120),
    ]

    config = EasyConfig(
        openai_api_key=os.environ["OPENAI_API_KEY"],
        agent=bridge,  # ← the whole point of this chapter
        transport=LocalTransportConfig(),
        stt="openai",
        tts="openai",
        output_processors=processors,
        session_actions=actions,
        action_executors=(CoreSessionActionExecutor(),),
        debug="light",
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    print("Talk to your custom agent. Say 'goodbye' to have it hang up.\n")
    try:
        await wait_for_shutdown_signal(session)
    finally:
        RUNS_DIR.mkdir(exist_ok=True)
        path = RUNS_DIR / f"ch14-bridge-{int(time.time())}.bundle"
        try:
            export_debug_bundle(session, path, overwrite=True)
            print(f"Wrote bundle → {path.relative_to(Path.cwd())}")
        except Exception as exc:  # noqa: BLE001 — teaching script
            print(f"(no bundle written: {exc})")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
