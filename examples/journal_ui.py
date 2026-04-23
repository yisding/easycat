"""Live debugger UI tailing the journal of a local mic session.

``easycat.debugger.serve_session(session, in_thread=True)`` runs the
bundled aiohttp UI on a daemon thread alongside your session. Open the
URL in a browser to see the timeline, per-turn waterfall, records
inspector, transcript, and per-turn audio playback update as the
conversation happens.

Setup: export OPENAI_API_KEY=...; uv sync --extra quickstart --extra debugger
Run:   uv run python examples/journal_ui.py
       # then open http://localhost:8765
"""

from __future__ import annotations

import asyncio

try:
    from agents import Agent  # type: ignore[import-untyped]
except ImportError as exc:
    raise SystemExit(
        "openai-agents is required. Install with: uv sync --extra quickstart"
    ) from exc

from easycat import (
    EasyCatConfig,
    attach_runtime_feedback,
    create_session,
    require_env,
    wait_for_shutdown_signal,
)
from easycat.debugger import serve_session


async def main() -> None:
    require_env("OPENAI_API_KEY")
    # debug="light" keeps the journal in memory so the UI has something to read.
    session = create_session(
        EasyCatConfig.mic(
            agent=Agent(name="assistant", instructions="You are a helpful voice assistant."),
            debug="light",
        )
    )
    attach_runtime_feedback(session)

    serve_session(session, in_thread=True, port=8765, open_browser=False)
    print("[journal_ui] debugger UI: http://localhost:8765")

    await session.start()
    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
