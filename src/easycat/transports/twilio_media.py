"""Twilio Media Streams transport.

Handles Twilio's bidirectional WebSocket protocol for real phone calls.
Converts between Twilio's mulaw 8 kHz format and EasyCat's PCM16 format,
and emits DTMF / control events into the Session event bus.
"""

from __future__ import annotations

import base64
import json
import logging
import struct
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection

from easycat.audio_format import PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.audio_utils import resample
from easycat.events import DTMF, EventBus
from easycat.transports._base import _ServerTransportBase

logger = logging.getLogger(__name__)

# Twilio sends/receives mulaw 8 kHz mono.
MULAW_8K = AudioFormat(sample_rate=8000, channels=1, sample_width=1, encoding="mulaw")


@dataclass
class TwilioTransportConfig:
    """Configuration for :class:`TwilioTransport`."""

    host: str = "0.0.0.0"
    port: int = 8766
    audio_format: AudioFormat = field(default_factory=lambda: PCM16_MONO_16K)
    max_pending_chunks: int = 200


class TwilioTransport(_ServerTransportBase):
    """Transport for Twilio Media Streams bidirectional WebSocket.

    Implements the ``Transport`` protocol from :mod:`easycat.providers`.

    Twilio message types handled:
      - ``connected`` — initial connection acknowledgement
      - ``start``     — stream metadata (streamSid, callSid, tracks, etc.)
      - ``media``     — base64-encoded mulaw 8 kHz audio
      - ``stop``      — stream ended
      - ``mark``      — playback mark acknowledgement
      - ``dtmf``      — DTMF digit pressed by caller

    DTMF digits are emitted into the provided ``EventBus`` so WS6 can
    consume them.  Audio is converted from mulaw 8 kHz to the internal PCM16
    format (default 16 kHz) on ingest, and back on egress.
    """

    _transport_name = "Twilio"

    def __init__(
        self,
        config: TwilioTransportConfig | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._config = config or TwilioTransportConfig()
        super().__init__(
            host=self._config.host,
            port=self._config.port,
            max_pending_chunks=self._config.max_pending_chunks,
        )
        self._audio_format = self._config.audio_format
        self._event_bus = event_bus

        self._stream_sid: str | None = None
        self._call_sid: str | None = None

        self._mark_counter = 0

    # ── Transport protocol ────────────────────────────────────────

    async def disconnect(self) -> None:
        """Disconnect Twilio and stop the server."""
        await super().disconnect()
        self._stream_sid = None
        self._call_sid = None

    async def send_audio(self, chunk: AudioChunk) -> None:
        """Convert a PCM16 chunk to mulaw 8 kHz and send to Twilio."""
        ws = self._ws
        if ws is None or self._stream_sid is None:
            return

        mulaw_data = pcm16_to_mulaw(chunk.data, chunk.format.sample_rate)
        payload = base64.b64encode(mulaw_data).decode("ascii")

        message = json.dumps(
            {
                "event": "media",
                "streamSid": self._stream_sid,
                "media": {"payload": payload},
            }
        )
        try:
            await ws.send(message)
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Cannot send audio: Twilio disconnected")

    # ── Mark support ──────────────────────────────────────────────

    async def send_mark(self, name: str | None = None) -> str:
        """Send a ``mark`` message so Twilio acknowledges playback position.

        Returns the mark name used (auto-generated if not provided).
        """
        ws = self._ws
        if ws is None or self._stream_sid is None:
            raise RuntimeError("No active Twilio stream")

        if name is None:
            self._mark_counter += 1
            name = f"mark_{self._mark_counter}"

        message = json.dumps(
            {
                "event": "mark",
                "streamSid": self._stream_sid,
                "mark": {"name": name},
            }
        )
        await ws.send(message)
        return name

    async def clear_audio(self) -> None:
        """Send a ``clear`` message to discard queued outbound audio on Twilio's side."""
        ws = self._ws
        if ws is None or self._stream_sid is None:
            return

        message = json.dumps(
            {
                "event": "clear",
                "streamSid": self._stream_sid,
            }
        )
        try:
            await ws.send(message)
        except websockets.exceptions.ConnectionClosed:
            logger.debug("Cannot clear audio: Twilio disconnected")

    # ── Twilio WebSocket handler ──────────────────────────────────

    async def _handle_connection(self, ws: ServerConnection) -> None:
        """Handle the Twilio Media Streams WebSocket connection."""
        if self._ws is not None:
            logger.warning("Rejecting additional Twilio connection")
            await ws.close(4000, "Only one stream at a time")
            return

        self._ws = ws
        self._client_connected.set()
        logger.info("Twilio Media Streams connected")

        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                await self._handle_message(raw)
        except websockets.exceptions.ConnectionClosed:
            logger.info("Twilio Media Streams disconnected")
        finally:
            self._ws = None
            self._client_connected.clear()
            self._stream_sid = None
            self._enqueue_sentinel()

    async def _handle_message(self, raw: str) -> None:
        """Route a Twilio JSON message to the appropriate handler."""
        try:
            msg: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Ignoring invalid JSON from Twilio")
            return

        event = msg.get("event", "")
        if event == "connected":
            logger.debug("Twilio connected event: protocol=%s", msg.get("protocol"))
        elif event == "start":
            self._handle_start(msg)
        elif event == "media":
            await self._handle_media(msg)
        elif event == "stop":
            logger.info("Twilio stream stopped (streamSid=%s)", self._stream_sid)
            # Explicitly end the current audio stream so receive_audio() can terminate.
            self._stream_sid = None
            self._call_sid = None
            self._enqueue_sentinel()
        elif event == "mark":
            mark_name = msg.get("mark", {}).get("name", "")
            logger.debug("Twilio mark acknowledged: %s", mark_name)
        elif event == "dtmf":
            await self._handle_dtmf(msg)
        else:
            logger.debug("Unknown Twilio event: %s", event)

    def _handle_start(self, msg: dict[str, Any]) -> None:
        """Extract stream metadata from the ``start`` message."""
        start = msg.get("start", {})
        self._stream_sid = msg.get("streamSid") or start.get("streamSid")
        self._call_sid = start.get("callSid")
        logger.info(
            "Twilio stream started: streamSid=%s callSid=%s",
            self._stream_sid,
            self._call_sid,
        )

    async def _handle_media(self, msg: dict[str, Any]) -> None:
        """Decode mulaw audio from a ``media`` message and enqueue as PCM16."""
        media = msg.get("media", {})
        payload = media.get("payload", "")
        if not payload:
            return

        try:
            mulaw_data = base64.b64decode(payload)
        except Exception:
            logger.warning("Ignoring Twilio media frame with invalid base64 payload")
            return
        pcm_data = mulaw_to_pcm16(mulaw_data, self._audio_format.sample_rate)

        chunk = AudioChunk(data=pcm_data, format=self._audio_format)
        self._enqueue_chunk(chunk, context="Twilio")

    async def _handle_dtmf(self, msg: dict[str, Any]) -> None:
        """Emit a DTMF event for the pressed digit."""
        dtmf_data = msg.get("dtmf", {})
        digit = dtmf_data.get("digit", "")
        if digit and self._event_bus is not None:
            logger.debug("DTMF digit received: %s", digit)
            await self._event_bus.emit(DTMF(digit=digit))

    # ── Properties ────────────────────────────────────────────────

    @property
    def stream_sid(self) -> str | None:
        return self._stream_sid

    @property
    def call_sid(self) -> str | None:
        return self._call_sid


# ── Audio conversion helpers ──────────────────────────────────────


def mulaw_to_pcm16(mulaw_data: bytes, target_rate: int = 16000) -> bytes:
    """Convert mulaw 8 kHz audio to PCM16 at ``target_rate``."""
    pcm_8k = _mulaw_decode(mulaw_data)
    if target_rate == 8000:
        return pcm_8k
    return resample(pcm_8k, 8000, target_rate)


def pcm16_to_mulaw(pcm_data: bytes, source_rate: int = 16000) -> bytes:
    """Convert PCM16 at ``source_rate`` to mulaw 8 kHz."""
    if source_rate != 8000:
        pcm_data = resample(pcm_data, source_rate, 8000)
    return _mulaw_encode(pcm_data)


_MULAW_BIAS = 0x84
_MULAW_CLIP = 32635


def _mulaw_decode(mulaw_data: bytes) -> bytes:
    """Decode G.711 mu-law bytes into PCM16 little-endian bytes."""
    out = bytearray(len(mulaw_data) * 2)
    for i, value in enumerate(mulaw_data):
        sample = _mulaw_decode_sample(value)
        out[i * 2 : i * 2 + 2] = sample.to_bytes(2, "little", signed=True)
    return bytes(out)


def _mulaw_encode(pcm_data: bytes) -> bytes:
    """Encode PCM16 little-endian bytes into G.711 mu-law bytes."""
    if len(pcm_data) % 2 != 0:
        pcm_data = pcm_data[:-1]
    out = bytearray(len(pcm_data) // 2)
    for i, (sample,) in enumerate(struct.iter_unpack("<h", pcm_data)):
        out[i] = _mulaw_encode_sample(sample)
    return bytes(out)


def _mulaw_decode_sample(value: int) -> int:
    """Decode a single mu-law byte into a signed PCM16 sample."""
    value = (~value) & 0xFF
    sign = value & 0x80
    exponent = (value >> 4) & 0x07
    mantissa = value & 0x0F
    sample = ((mantissa << 3) + _MULAW_BIAS) << exponent
    sample -= _MULAW_BIAS
    if sign:
        sample = -sample
    return sample


def _mulaw_encode_sample(sample: int) -> int:
    """Encode a signed PCM16 sample into a mu-law byte."""
    if sample < 0:
        sign = 0x80
        sample = -sample
    else:
        sign = 0x00

    if sample > _MULAW_CLIP:
        sample = _MULAW_CLIP

    sample += _MULAW_BIAS
    exponent = 7
    exp_mask = 0x4000
    while exponent > 0 and (sample & exp_mask) == 0:
        exponent -= 1
        exp_mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


# ── TwiML helpers ─────────────────────────────────────────────────


def twiml_connect_stream(
    websocket_url: str,
    *,
    track: str = "both",
    status_callback_url: str | None = None,
) -> str:
    """Generate TwiML ``<Connect><Stream>`` XML for bidirectional streaming.

    Parameters
    ----------
    websocket_url:
        The ``wss://`` URL of the EasyCat Twilio transport server.
    track:
        Which audio tracks to stream (``inbound``, ``outbound``, or ``both``).
    status_callback_url:
        Optional URL for Twilio to POST call status updates.
    """
    from xml.sax.saxutils import quoteattr

    status_attr = ""
    if status_callback_url:
        status_attr = f" statusCallback={quoteattr(status_callback_url)}"

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        "  <Connect>\n"
        f"    <Stream url={quoteattr(websocket_url)} track={quoteattr(track)}{status_attr} />\n"
        "  </Connect>\n"
        "</Response>"
    )


def twiml_stream(
    websocket_url: str,
    *,
    track: str = "inbound_track",
) -> str:
    """Generate TwiML ``<Start><Stream>`` XML for one-way streaming.

    Parameters
    ----------
    websocket_url:
        The ``wss://`` URL of the EasyCat Twilio transport server.
    track:
        Which track to stream (``inbound_track`` or ``outbound_track``).
    """
    from xml.sax.saxutils import quoteattr

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        "  <Start>\n"
        f"    <Stream url={quoteattr(websocket_url)} track={quoteattr(track)} />\n"
        "  </Start>\n"
        '  <Pause length="60" />\n'
        "</Response>"
    )
