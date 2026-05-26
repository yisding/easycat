"""Chapter 15 — operate in production.

Start a real session, walk it through the full lifecycle, prove
the journal survives ``stop()``, export a bundle you could hand
to a teammate, and print the one-liner that opens the debugger UI.

Dependencies:
    uv sync --extra quickstart --group dev
    export OPENAI_API_KEY=...
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from easycat import (
    EasyConfig,
    JournalRecordKind,
    LocalTransportConfig,
    SessionManager,
    attach_runtime_feedback,
    create_session,
    export_debug_bundle,
    wait_for_shutdown_signal,
)

RUNS_DIR = Path(__file__).parent / "runs"


def build_session():
    """Same shape as ch 13's Local cell. For a real deployment you
    would typically bump ``debug`` to ``"full"`` and swap
    ``journal_backend`` to ``"sqlite+litestream"`` so journals
    survive a process crash; we leave both at teaching defaults
    here so the run stays fast.
    """

    from agents import Agent  # type: ignore[import-untyped]

    config = EasyConfig(
        openai_api_key=os.environ["OPENAI_API_KEY"],
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


async def main() -> None:
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("Set OPENAI_API_KEY.")

    # ── 1. SessionManager for multi-session servers ───────────────
    # In a real server (WebSocket handler, Twilio websocket,
    # whatever) you'd scope a session to a connection key and let
    # the manager tear it down on disconnect. We only run one here,
    # but the shape is the same.
    manager: SessionManager[str] = SessionManager()
    session = build_session()
    attach_runtime_feedback(session)

    session_key = f"local-{int(time.time())}"
    async with manager.connection(session_key, session):
        print(f"Session {session_key!r} started via SessionManager.")
        print("Talk. Ctrl-C to stop.\n")
        try:
            await wait_for_shutdown_signal(session)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
    # manager.connection exited → session.stop() → session.destroy().
    print("Session stopped; manager released the slot.")

    # ── 2. Post-stop: journal still works, bundle still exports ───
    # The invariant from CLAUDE.md: after stop()/shutdown(), the
    # journal is in a read-only postmortem state. .read() works,
    # export_debug_bundle() works, .append() does not.
    assert session.journal is not None
    records = session.journal.read()
    counts: dict[str, int] = {}
    for rec in records:
        if rec.kind is not JournalRecordKind.EVENT:
            continue
        counts[rec.name] = counts.get(rec.name, 0) + 1
    print("\nPost-stop event counts (top 5):")
    for name, n in sorted(counts.items(), key=lambda kv: -kv[1])[:5]:
        print(f"  {n:>4}  {name}")

    RUNS_DIR.mkdir(exist_ok=True)
    bundle_path = RUNS_DIR / f"ch15-{session_key}.bundle"
    export_debug_bundle(session, bundle_path, overwrite=True)
    print(f"\nWrote bundle → {bundle_path.relative_to(Path.cwd())}")

    # ── 3. The debugger one-liner ──────────────────────────────────
    print(
        "\nOpen the debugger UI on this bundle:\n"
        f"  uv run python -c 'from easycat.debugger import serve_bundle; "
        f'serve_bundle("{bundle_path}", port=8765)\'\n'
        "  → browse http://127.0.0.1:8765"
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
