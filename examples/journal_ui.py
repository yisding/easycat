"""Live debugger UI tailing the journal of a local mic session.

``easycat.debugger.serve_session(session, in_thread=True)`` runs the
bundled aiohttp UI on a daemon thread alongside your session. Open the
URL in a browser to see the timeline, per-turn waterfall, records
inspector, transcript, and per-turn audio playback update as the
conversation happens.

Setup: export OPENAI_API_KEY=...; uv sync --extra quickstart --extra debugger --group dev
       uv run easycat doctor
       uv run easycat doctor --env-file .env  # if keys live in .env
       uv run easycat doctor --env-file .env --json  # for parseable checks
Run:   uv run python examples/journal_ui.py
       uv run --env-file .env python examples/journal_ui.py  # if keys live in .env
       # then open http://localhost:8765
"""

from __future__ import annotations

try:
    from agents import Agent  # type: ignore[import-untyped]
except ImportError as exc:
    raise SystemExit(
        "openai-agents is required. For an app, run: "
        "uv add 'easycat[quickstart,debugger]'. In this repo, run: "
        "uv sync --extra quickstart --extra debugger --group dev"
    ) from exc

from easycat import (
    EasyConfig,
    create_session,
    require_env,
)
from easycat.debugger import serve_session
from easycat.helpers import run_session


def main() -> None:
    require_env("OPENAI_API_KEY")
    # debug="light" keeps the journal in memory so the UI has something to read.
    session = create_session(
        EasyConfig.mic(
            agent=Agent(name="assistant", instructions="You are a helpful voice assistant."),
            debug="light",
        )
    )

    serve_session(session, in_thread=True, port=8765, open_browser=False)
    print("[journal_ui] debugger UI: http://localhost:8765")

    run_session(session)


if __name__ == "__main__":
    main()
