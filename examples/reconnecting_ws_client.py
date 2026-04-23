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
    await ws.connect()

    async def sender() -> None:
        while not stop.is_set():
            try:
                await ws.send(SILENCE_FRAME_20MS_16KHZ)
            except Exception as exc:
                print(f"[client] send failed (will reconnect on next recv): {exc}")
            await asyncio.sleep(0.02)

    async def receiver() -> None:
        async for message in ws.recv_iter():
            size = len(message) if isinstance(message, (bytes, bytearray)) else len(message or "")
            print(f"[client] received {size} bytes/chars from server")

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
