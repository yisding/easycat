"""Chapter 15 — operate in production.

Start a real session, walk it through the full lifecycle, prove
the journal survives ``stop()``, export a bundle you could hand
to a teammate, and print the one-liner that opens the debugger UI.

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


def debugger_command(path: Path, *, port: int = 8765) -> str:
    """Open the maintained debugger CLI on this captured bundle."""
    return shlex.join(
        [
            "uv",
            "run",
            "easycat",
            "debugger",
            "serve",
            str(_display_path(path)),
            "--port",
            str(port),
        ]
    )


def build_session():
    """Same shape as ch 13's Local cell. For a real deployment you
    would typically bump ``debug`` to ``"full"`` and swap
    ``journal_backend`` to ``"sqlite+litestream"`` so journals
    survive a process crash; we leave both at teaching defaults
    here so the run stays fast.
    """

    from agents import Agent  # type: ignore[import-untyped]

    config = EasyConfig(
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
    # manager.connection exited -> session.stop() -> private teardown.
    print("Session stopped; manager released the slot.")

    # ── 2. Post-stop: journal still works, bundle still exports ───
    # The lifecycle invariant: Session.journal is always a read-only
    # JournalView. After stop(), that same view reads a preserved
    # postmortem backend, and export_debug_bundle() still works.
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
    print(f"\nWrote bundle → {_display_path(bundle_path)}")
    human_command, json_command = measurement_commands(bundle_path)
    print("Measure this production-shaped bundle directly:")
    print(f"  {human_command}")
    print(f"  {json_command}")

    # ── 3. The debugger CLI ────────────────────────────────────────
    print("\nOpen the debugger UI on this bundle:")
    print(f"  {debugger_command(bundle_path)}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
