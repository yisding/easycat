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
    uv run easycat doctor
    uv run easycat doctor --env-file .env         # if keys live in .env
    uv run easycat doctor --env-file .env --json  # for parseable checks
    Add `--env-file .env` after `uv run` on script commands if keys live in `.env`.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

from easycat import (
    EasyConfig,
    LocalTransportConfig,
    MarkdownStripProcessor,
    attach_runtime_feedback,
    create_session,
    default_pronunciation_processors,
    export_debug_bundle,
    wait_for_shutdown_signal,
)
from easycat.cancel import CancelToken
from easycat.integrations.agents import GenericWorkflowBridge
from easycat.integrations.agents.base import AgentRecorder, CancellationMode
from easycat.llm_output_processing import LLMOutputProcessor
from easycat.session.actions import CoreSessionActionExecutor, EndCallAction, SessionActions

if TYPE_CHECKING:
    from openai import AsyncOpenAI

MODEL = "gpt-4o-mini"
RUNS_DIR = Path(__file__).parent / "runs"


def _display_path(path: Path) -> Path:
    try:
        return path.relative_to(Path.cwd())
    except ValueError:
        return path


def measurement_commands(path: Path) -> tuple[str, str]:
    """Commands that read this production-shaped bundle directly."""
    base = ["uv", "run", "easycat", "latency", str(_display_path(path))]
    return (
        shlex.join(base),
        shlex.join([*base, "--json"]),
    )


def pronunciation_command(path: Path) -> str:
    """Inspect the scheduler's provider-ready pronunciation payloads."""
    return shlex.join(
        [
            "uv",
            "run",
            "easycat",
            "journal",
            "grep",
            str(_display_path(path)),
            "--query",
            "tts_payload_prepared",
            "--json",
        ]
    )


def build_output_processors() -> list[LLMOutputProcessor]:
    """Build the chapter's pronunciation stack from the public factory."""
    return [
        MarkdownStripProcessor(),
        *default_pronunciation_processors(
            name_pronunciations={"easycat": "ee zee cat"},
            phone_pause_ms=120,
        ),
    ]


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
            model=MODEL, messages=self._history, stream=True
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


async def main() -> None:
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("Set OPENAI_API_KEY.")

    from openai import AsyncOpenAI

    client = AsyncOpenAI()
    actions = SessionActions()  # shared: workflow enqueues, session drains
    workflow = MyWorkflow(client, actions)
    bridge = GenericWorkflowBridge(workflow)
    assert bridge.deep_mode, "deep mode required for mid-turn interruption"

    # A tiny pronunciation pipeline. Processors run serially on every
    # committed assistant utterance before the text reaches TTS; a
    # raise in one is logged and the next runs (fail-open).
    processors = build_output_processors()

    config = EasyConfig(
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
        await session.stop(force=True)
        RUNS_DIR.mkdir(exist_ok=True)
        path = RUNS_DIR / f"ch14-bridge-{int(time.time())}.bundle"
        try:
            export_debug_bundle(session, path, overwrite=True)
            print(f"Wrote bundle → {_display_path(path)}")
            human_command, json_command = measurement_commands(path)
            print("Measure this production-shaped bundle directly:")
            print(f"  {human_command}")
            print(f"  {json_command}")
            print("Inspect its provider-ready pronunciation payloads:")
            print(f"  {pronunciation_command(path)}")
        except Exception as exc:  # noqa: BLE001 — teaching script
            print(f"(no bundle written: {exc})")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
