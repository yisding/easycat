"""Push-to-talk demo.

In ``TurnMode.PUSH_TO_TALK`` the ``TurnManager`` does not consult VAD
to start or end turns: the application calls ``session.start_turn()`` and
``session.end_turn()`` itself.  Useful for hardware push-to-talk buttons,
walkie-talkie UX, or mocked turns in tests.

This example reads Enter presses from stdin: press ``Enter`` to mark
the start of a turn, then ``Enter`` again to mark the end.  Real
deployments would wire this to a GPIO pin, a UI button, or a hotkey.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart
  uv run python examples/push_to_talk.py
"""

from __future__ import annotations

import asyncio
import sys
import threading

from easycat import (
    EasyConfig,
    LocalTransportConfig,
    TurnManagerConfig,
    TurnMode,
    attach_runtime_feedback,
    create_session,
    require_env,
)


async def _stdin_loop(session) -> None:  # type: ignore[no-untyped-def]
    """Toggle a turn each time the user presses Enter.

    Adapts to the environment: on Unix TTYs we use ``loop.add_reader`` so
    Ctrl+C doesn't leave a blocked ``input()`` thread behind.  On Windows'
    default (Proactor) loop ``add_reader`` raises ``NotImplementedError``,
    and when stdin isn't selectable the reader would busy-loop on EOF — in
    both cases we fall back to a daemon thread calling ``readline()``.
    Daemon threads are killed at interpreter exit, so shutdown still works
    if the thread is blocked inside ``readline()``.  Closed stdin (EOF)
    ends the loop cleanly instead of spinning.
    """
    print("\nPress Enter to START speaking, Enter again to END the turn.")
    print("Press Ctrl+C to quit.\n")

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bool] = asyncio.Queue()  # True = got line, False = EOF

    try:
        fd = sys.stdin.fileno()
    except (OSError, ValueError):
        fd = -1

    use_reader = False
    if fd >= 0 and sys.platform != "win32":

        def _on_stdin() -> None:
            line = sys.stdin.readline()
            if not line:
                # EOF — detach so we don't busy-loop on an always-ready fd.
                loop.remove_reader(fd)
            queue.put_nowait(bool(line))

        try:
            loop.add_reader(fd, _on_stdin)
            use_reader = True
        except NotImplementedError:
            pass

    if not use_reader:

        def _read_forever() -> None:
            while True:
                line = sys.stdin.readline()
                loop.call_soon_threadsafe(queue.put_nowait, bool(line))
                if not line:
                    return

        threading.Thread(target=_read_forever, daemon=True).start()

    speaking = False
    try:
        while True:
            got = await queue.get()
            if not got:
                print("  [stdin closed — exiting]")
                return
            if not speaking:
                await session.start_turn()
                print("  [turn started — speak now]")
            else:
                await session.end_turn()
                print("  [turn ended — agent is replying]")
            speaking = not speaking
    finally:
        if use_reader:
            loop.remove_reader(fd)


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")
    from agents import Agent  # type: ignore[import-untyped]

    agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")

    config = EasyConfig(
        openai_api_key=api_key,
        transport=LocalTransportConfig(),
        turn_taking=TurnManagerConfig(mode=TurnMode.PUSH_TO_TALK),
        agent=agent,
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    try:
        await _stdin_loop(session)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await session.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
