"""Record a session with debug capture, export a bundle, and inspect it.

End-to-end debug-capture workflow:
  1. Run a local mic/speaker session with ``debug="light"`` so every
     pipeline stage records to the journal.
  2. After ``Ctrl+C`` stops the session, export a ``RunBundle`` zip.
  3. Load the bundle back in the same process and print a per-turn
     summary plus a replay of the TTS audio the user heard.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart
  uv run python examples/debug_bundle.py
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path

from easycat import (
    EasyConfig,
    LocalTransportConfig,
    attach_runtime_feedback,
    create_session,
    require_env,
    wait_for_shutdown_signal,
)
from easycat.debug.bundle import RunBundle

BUNDLE_PATH = Path("run.zip")


def _summarize(bundle: RunBundle) -> None:
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

    print(f"\nBundle: {BUNDLE_PATH}")
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
    api_key = require_env("OPENAI_API_KEY")

    from agents import Agent  # type: ignore[import-untyped]

    agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")

    config = EasyConfig(
        openai_api_key=api_key,
        transport=LocalTransportConfig(),
        agent=agent,
        debug="light",  # enables journal + in-memory artifact store
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    print("Recording session. Press Ctrl+C to stop and export the bundle.\n")
    await wait_for_shutdown_signal(session)

    session.export_debug_bundle(str(BUNDLE_PATH), overwrite=True)
    print(f"\nExported bundle to {BUNDLE_PATH}")

    _summarize(RunBundle.load(BUNDLE_PATH))


if __name__ == "__main__":
    asyncio.run(main())
