"""Chapter 6 — Control a Session directly.

Dependencies:
    uv sync --extra quickstart --group dev
    OPENAI_API_KEY (voice mode only, for STT and TTS)

Preflight:
    uv run easycat doctor
    uv run easycat doctor --json
    uv run easycat doctor --env-file .env
    uv run easycat doctor --env-file .env --json

Run:
    uv run python docs/using-easycat/06-session-control/main.py text
    uv run python docs/using-easycat/06-session-control/main.py voice
    If the key lives in .env, add `--env-file .env` after `uv run`.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Literal, cast

from easycat import (
    EasyConfig,
    STTFinal,
    create_session,
    create_text_session,
    require_env,
)
from easycat.helpers import run_session

Mode = Literal["text", "voice"]


class CounterWorkflow:
    """Deterministic state makes reset behavior visible without a model API."""

    def __init__(self) -> None:
        self.turns = 0

    async def on_user_turn(self, text: str) -> str:
        self.turns += 1
        return f"Workflow turn {self.turns}: {text}"

    def reset(self) -> None:
        self.turns = 0


def parse_mode() -> Mode:
    parser = argparse.ArgumentParser(
        description="Run an offline text lifecycle or a live voice Session."
    )
    parser.add_argument("mode", choices=("text", "voice"))
    return cast(Mode, parser.parse_args().mode)


async def run_text_demo() -> None:
    session = create_text_session(agent=CounterWorkflow(), debug="off")
    session.on(
        turn_started=lambda: print("[event] turn started"),
        agent_response=lambda text: print(f"[event] agent: {text}"),
        turn_ended=lambda: print("[event] turn ended"),
    )

    async with session:
        first = await session.send_text("first message")
        second = await session.send_text("second message")
        await session.reset_state()
        after_reset = await session.send_text("after reset")

    await session.wait_closed()
    print("Reply 1:", first)
    print("Reply 2:", second)
    print("Reply after reset:", after_reset)

    try:
        await session.send_text("too late")
    except RuntimeError as exc:
        print("Post-stop guard:", exc)


def run_voice_demo() -> None:
    require_env("OPENAI_API_KEY")
    session = create_session(EasyConfig.mic(agent=CounterWorkflow()))
    transcript_subscription = session.subscribe_event(
        STTFinal,
        lambda event: print(f"[typed event] user: {event.text}"),
    )
    registrations = session.on(
        agent_response=lambda text: print(f"[simple event] agent: {text}"),
        interruption=lambda: print("[simple event] interrupted"),
    )
    try:
        run_session(session)
    finally:
        transcript_subscription.unsubscribe()
        session.unsubscribe_handlers(registrations)


def main() -> None:
    mode = parse_mode()
    if mode == "text":
        asyncio.run(run_text_demo())
        return
    run_voice_demo()


if __name__ == "__main__":
    main()
