"""Wire-level voice clients used by E2E tests.

Each client opens a real socket (or peer connection) to the pipeline's
transport, streams real PCM audio in, and collects what comes back.
These clients do NOT mock any part of the transport protocol.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection


@dataclass
class WSVoiceClient:
    """WebSocket voice client that talks to ``WebSocketConnectionTransport``.

    Sends binary PCM16 frames and JSON control messages. Collects both
    outbound binary frames and JSON control messages from the server.
    """

    url: str
    _ws: ClientConnection | None = field(default=None, init=False, repr=False)
    _recv_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _inbound_binary: list[bytes] = field(default_factory=list, init=False)
    _inbound_json: list[dict[str, Any]] = field(default_factory=list, init=False)
    _closed: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    server_audio_format: dict[str, Any] | None = field(default=None, init=False)

    async def __aenter__(self) -> WSVoiceClient:
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def connect(self) -> None:
        self._ws = await websockets.connect(self.url, max_size=None)
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if isinstance(msg, (bytes, bytearray)):
                    self._inbound_binary.append(bytes(msg))
                else:
                    try:
                        parsed = json.loads(msg)
                    except json.JSONDecodeError:
                        continue
                    self._inbound_json.append(parsed)
                    if parsed.get("type") == "audio_format":
                        self.server_audio_format = parsed
        except (
            websockets.exceptions.ConnectionClosed,
            asyncio.CancelledError,
        ):
            pass
        finally:
            self._closed.set()

    async def negotiate_config(self, *, sample_rate: int) -> None:
        """Send the client-config control message."""
        assert self._ws is not None
        await self._ws.send(json.dumps({"type": "config", "sample_rate": sample_rate}))

    async def wait_for_ready(self, *, timeout: float = 5.0) -> dict[str, Any]:
        """Wait until the server sends ``{"type": "ready"}``."""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            for msg in self._inbound_json:
                if msg.get("type") == "ready":
                    return msg
            now = asyncio.get_event_loop().time()
            if now >= deadline:
                raise TimeoutError("no ready message received")
            await asyncio.sleep(0.02)

    async def send_binary(self, data: bytes) -> None:
        assert self._ws is not None
        await self._ws.send(data)

    async def send_pcm_realtime(
        self,
        pcm: bytes,
        *,
        sample_rate: int,
        chunk_ms: int = 20,
    ) -> None:
        """Stream PCM16 with real-time pacing (the pipeline expects it)."""
        assert self._ws is not None
        bytes_per_ms = int((sample_rate * 2) / 1000)
        chunk_size = bytes_per_ms * chunk_ms
        if chunk_size <= 0:
            chunk_size = 320
        for i in range(0, len(pcm), chunk_size):
            await self._ws.send(pcm[i : i + chunk_size])
            await asyncio.sleep(chunk_ms / 1000.0)

    async def send_silence(self, *, seconds: float, sample_rate: int) -> None:
        await self.send_pcm_realtime(
            bytes(int(seconds * sample_rate) * 2), sample_rate=sample_rate
        )

    async def send_json(self, obj: dict[str, Any]) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps(obj))

    async def collect_outbound(
        self,
        *,
        min_bytes: int = 1,
        timeout: float = 15.0,
    ) -> bytes:
        """Wait until at least ``min_bytes`` of binary data has arrived."""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            total = sum(len(b) for b in self._inbound_binary)
            if total >= min_bytes:
                break
            if asyncio.get_event_loop().time() >= deadline:
                raise TimeoutError(f"only {total} bytes received (wanted {min_bytes})")
            await asyncio.sleep(0.05)
        return b"".join(self._inbound_binary)

    async def drain_until_silent(
        self,
        *,
        idle_for_s: float = 1.5,
        max_wait_s: float = 60.0,
    ) -> bytes:
        """Collect outbound bytes until no new bytes arrive for
        ``idle_for_s`` seconds (or ``max_wait_s`` total elapses).

        Use this for voice-round-trip tests where we want to capture a
        whole TTS response rather than the first N bytes.
        """
        last_size = 0
        last_change = asyncio.get_event_loop().time()
        start = last_change
        while True:
            now = asyncio.get_event_loop().time()
            size = sum(len(b) for b in self._inbound_binary)
            if size != last_size:
                last_size = size
                last_change = now
            if size > 0 and (now - last_change) >= idle_for_s:
                break
            if (now - start) >= max_wait_s:
                break
            await asyncio.sleep(0.1)
        return b"".join(self._inbound_binary)

    def outbound_bytes(self) -> bytes:
        return b"".join(self._inbound_binary)

    def control_messages(self) -> list[dict[str, Any]]:
        return list(self._inbound_json)

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._recv_task is not None:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass


# ---------------------------------------------------------------------------
# Twilio client
# ---------------------------------------------------------------------------


@dataclass
class TwilioVoiceClient:
    """Twilio Media Streams client that talks to ``TwilioConnectionTransport``.

    Follows the Twilio wire protocol: ``connected`` -> ``start`` ->
    ``media`` (base64 μ-law 8 kHz) -> ``mark`` / ``stop``. Collects
    outbound ``media``, ``mark``, and ``clear`` messages.
    """

    url: str
    stream_sid: str = "MZ_test_stream"
    call_sid: str = "CA_test_call"
    _ws: ClientConnection | None = field(default=None, init=False, repr=False)
    _recv_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _inbound: list[dict[str, Any]] = field(default_factory=list, init=False)
    _closed: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    async def __aenter__(self) -> TwilioVoiceClient:
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def connect(self) -> None:
        self._ws = await websockets.connect(self.url, max_size=None)
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if isinstance(msg, (bytes, bytearray)):
                    continue  # Twilio is JSON only
                try:
                    self._inbound.append(json.loads(msg))
                except json.JSONDecodeError:
                    continue
        except (
            websockets.exceptions.ConnectionClosed,
            asyncio.CancelledError,
        ):
            pass
        finally:
            self._closed.set()

    async def send_raw(self, obj: dict[str, Any]) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps(obj))

    async def send_connected(self) -> None:
        await self.send_raw({"event": "connected", "protocol": "Call"})

    async def send_start(self) -> None:
        await self.send_raw(
            {
                "event": "start",
                "streamSid": self.stream_sid,
                "start": {
                    "streamSid": self.stream_sid,
                    "callSid": self.call_sid,
                    "accountSid": "AC_test",
                    "tracks": ["inbound"],
                    "mediaFormat": {
                        "encoding": "audio/x-mulaw",
                        "sampleRate": 8000,
                        "channels": 1,
                    },
                },
            }
        )

    async def send_media_mulaw(self, mulaw_bytes: bytes, *, track: str = "inbound") -> None:
        payload = base64.b64encode(mulaw_bytes).decode()
        await self.send_raw(
            {
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": payload, "track": track},
            }
        )

    async def stream_mulaw_realtime(
        self,
        mulaw_bytes: bytes,
        *,
        frame_bytes: int = 160,
        track: str = "inbound",
    ) -> None:
        """Stream μ-law at 8 kHz with real-time pacing (20 ms frames)."""
        for i in range(0, len(mulaw_bytes), frame_bytes):
            frame = mulaw_bytes[i : i + frame_bytes]
            if not frame:
                break
            await self.send_media_mulaw(frame, track=track)
            await asyncio.sleep(0.02)

    async def send_dtmf(self, digit: str) -> None:
        await self.send_raw(
            {
                "event": "dtmf",
                "streamSid": self.stream_sid,
                "dtmf": {"digit": digit},
            }
        )

    async def send_stop(self) -> None:
        await self.send_raw({"event": "stop", "streamSid": self.stream_sid})

    def inbound_messages(self) -> list[dict[str, Any]]:
        return list(self._inbound)

    def outbound_media_frames(self) -> list[dict[str, Any]]:
        return [m for m in self._inbound if m.get("event") == "media"]

    def clear_events(self) -> list[dict[str, Any]]:
        return [m for m in self._inbound if m.get("event") == "clear"]

    def decoded_outbound_mulaw(self) -> bytes:
        out = bytearray()
        for m in self.outbound_media_frames():
            try:
                out += base64.b64decode(m["media"]["payload"])
            except Exception:
                continue
        return bytes(out)

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._recv_task is not None:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass


__all__ = ["TwilioVoiceClient", "WSVoiceClient"]
