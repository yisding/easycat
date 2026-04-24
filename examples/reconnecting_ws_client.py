"""Resilient WebSocket client built on ``ReconnectingWebSocket``.

EasyCat's STT/TTS providers and some transports use
``easycat.reconnecting_ws.ReconnectingWebSocket`` internally for automatic
reconnection with exponential backoff and jitter.  The same class is
exported and can be reused when you write custom network code — for
example a supervisor, a sidecar recorder, or a thin test client against
the local ``ws_server.py``.

This example connects to ``ws://localhost:8765`` (the URL used by
``examples/ws_server.py``), sends a few frames of silence so the server
has something to receive, and keeps listening.  Kill ``ws_server.py``
and restart it — the client logs the reconnect attempts and resumes
transparently.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart
  # Terminal 1:
  uv run python examples/ws_server.py
  # Terminal 2:
  uv run python examples/reconnecting_ws_client.py
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from easycat.reconnecting_ws import ReconnectConfig, ReconnectingWebSocket

URL = "ws://localhost:8765"
SILENCE_FRAME_20MS_16KHZ = b"\x00" * 640  # 16-bit mono PCM, 20 ms at 16 kHz


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    async def on_reconnect() -> None:
        print("[client] reconnected — resuming send loop")

    async def on_give_up() -> None:
        print("[client] gave up reconnecting; server is unreachable")

    ws = ReconnectingWebSocket(
        URL,
        config=ReconnectConfig(
            max_retries=-1,  # unlimited — keep trying until Ctrl-C
            base_delay=1.0,
            max_delay=10.0,
        ),
        provider_name="demo-client",
        on_reconnect=on_reconnect,
        on_give_up=on_give_up,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    print(f"[client] connecting to {URL} (Ctrl+C to stop)")
    # ``max_retries=-1`` means the initial ``connect()`` retries forever
    # when the server is down.  Race it against ``stop`` so Ctrl-C
    # cancels the attempt instead of hanging until a server appears.
    connect_task = asyncio.create_task(ws.connect())
    stop_task = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait(
            {connect_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        if not stop_task.done():
            stop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_task
    if connect_task not in done:
        connect_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await connect_task
        await ws.close()
        return
    # Surface connect errors (rather than silently proceeding).
    connect_task.result()

    async def sender() -> None:
        while not stop.is_set():
            try:
                await ws.send(SILENCE_FRAME_20MS_16KHZ)
            except Exception as exc:
                print(f"[client] send failed (will reconnect on next recv): {exc}")
            await asyncio.sleep(0.02)

    async def receiver() -> None:
        # ``recv_iter()`` only auto-reconnects on ``ConnectionClosed``; a
        # clean server-initiated close (Ctrl-C of ws_server.py) ends the
        # iterator normally. Wrap it so the demo keeps reconnecting.
        while not stop.is_set():
            try:
                async for message in ws.recv_iter():
                    size = (
                        len(message)
                        if isinstance(message, (bytes, bytearray))
                        else len(message or "")
                    )
                    print(f"[client] received {size} bytes/chars from server")
            except Exception as exc:
                print(f"[client] receive loop error: {exc}")
            if stop.is_set():
                return
            print("[client] receive stream ended; reconnecting…")
            try:
                await ws.connect()
            except Exception as exc:
                print(f"[client] reconnect failed: {exc}; giving up")
                stop.set()
                return

    send_task = asyncio.create_task(sender())
    recv_task = asyncio.create_task(receiver())

    await stop.wait()
    send_task.cancel()
    recv_task.cancel()
    for task in (send_task, recv_task):
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await ws.close()


if __name__ == "__main__":
    asyncio.run(main())
