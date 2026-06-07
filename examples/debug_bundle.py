"""Record a session with debug capture, auto-export a bundle, and inspect it.

End-to-end debug-capture workflow:
  1. Run a local mic/speaker session with ``debug="light"`` so every
     pipeline stage records to the journal.
  2. After ``Ctrl+C`` stops the session, ``record_to=`` writes a
     timestamped ``RunBundle`` zip.
  3. Load the bundle back in the same process and print a per-turn
     summary plus a replay of the TTS audio the user heard.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart --group dev
  uv run easycat doctor
  uv run easycat doctor --env-file .env  # if keys live in .env
  uv run easycat doctor --env-file .env --json  # for parseable checks
  uv run python examples/debug_bundle.py
  uv run --env-file .env python examples/debug_bundle.py  # if keys live in .env
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path

from easycat import (
    EasyConfig,
    attach_runtime_feedback,
    create_session,
    require_env,
    wait_for_shutdown_signal,
)
from easycat.debug.bundle import RunBundle

BUNDLE_DIR = Path("runs")


def _summarize(bundle: RunBundle, path: Path) -> None:
    """Walk the journal and print per-turn STT finals + TTS audio totals."""
    stt_finals: dict[str | None, list[str]] = defaultdict(list)
    agent_replies: dict[str | None, list[str]] = defaultdict(list)
    turn_ids: list[str | None] = []

    for record in bundle.records():
        name = record.get("name")
        turn_id = record.get("turn_id")
        data = record.get("data") or {}
        if not isinstance(data, dict):
            continue
        if name == "turn_started" and turn_id not in turn_ids:
            turn_ids.append(turn_id)
        elif name == "stt_final":
            text = data.get("text") or ""
            if text:
                stt_finals[turn_id].append(text)
        elif name == "agent_final":
            text = data.get("text") or ""
            if text:
                agent_replies[turn_id].append(text)

    print(f"\nBundle: {path}")
    print(f"  provider_versions: {bundle.manifest.provider_versions}")
    print(f"  turns recorded:    {len(turn_ids)}")

    for turn_id in turn_ids:
        print(f"\n  turn {turn_id}")
        for text in stt_finals.get(turn_id, []):
            print(f"    user:  {text}")
        for text in agent_replies.get(turn_id, []):
            print(f"    agent: {text}")

        chunks = bundle.replay_audio(turn_id=turn_id)
        total_bytes = sum(len(c.data) for c in chunks)
        total_ms = sum(c.duration_ms for c in chunks)
        print(f"    tts:   {len(chunks)} chunks, {total_bytes} bytes, {total_ms:.0f} ms")


async def main() -> None:
    require_env("OPENAI_API_KEY")

    from agents import Agent  # type: ignore[import-untyped]

    agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")

    session = create_session(
        EasyConfig.mic(
            agent=agent,
            record_to=BUNDLE_DIR,
            debug="light",
        )
    )
    attach_runtime_feedback(session)

    await session.start()
    print(f"Recording session. Press Ctrl+C to stop and export a bundle under {BUNDLE_DIR}/.\n")
    await wait_for_shutdown_signal(session)

    bundle_path = max(BUNDLE_DIR.glob(f"{session.session_id}-*.zip"))
    print(f"\nExported bundle to {bundle_path}")

    _summarize(RunBundle.load(bundle_path), bundle_path)


if __name__ == "__main__":
    asyncio.run(main())
