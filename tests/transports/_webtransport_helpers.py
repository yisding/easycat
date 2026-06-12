"""Shared WebTransport test fakes and builders."""

from __future__ import annotations

import asyncio
import struct
from typing import Any

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.transports.webtransport import (
    _TAG_AUDIO,
    WebTransportConnectionTransport,
    _WebTransportSession,
)


def _audio_frame(pcm: bytes, rate: int = 16000) -> bytes:
    """Client→server audio framing: ``[tag][4-byte BE sample-rate][PCM]``.

    Symmetric with the server→client framing — the mic rate is inline so it
    can't race a ``config`` control frame on an independent QUIC stream.
    """
    return bytes([_TAG_AUDIO]) + struct.pack(">I", rate) + pcm


def _aioquic_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("aioquic") is not None


class _FakeH3:
    def __init__(self) -> None:
        self.next_stream_id = 1000

    def create_webtransport_stream(self, session_id: int, is_unidirectional: bool = False) -> int:
        sid = self.next_stream_id
        self.next_stream_id += 1
        return sid


class _FakeQuicConnection:
    """Records ``reset_stream`` and raw WebTransport stream sends.

    WebTransport stream payload goes out as raw QUIC stream data (not H3
    ``DATA`` frames), so outbound framing assertions read ``sent`` here.
    """

    def __init__(self) -> None:
        self.resets: list[tuple[int, int]] = []
        self.sent: list[tuple[int, bytes]] = []

    def reset_stream(self, stream_id: int, error_code: int) -> None:
        self.resets.append((stream_id, error_code))

    def send_stream_data(self, stream_id: int, data: bytes, end_stream: bool = False) -> None:  # noqa: FBT001, FBT002
        self.sent.append((stream_id, data))


class _FakeStreamSender:
    """Stand-in for ``aioquic.quic.stream.QuicStreamSender``.

    Only ``_buffer`` (unsent + unacked bytes) is read by the outbound
    backpressure gate.
    """

    def __init__(self, buffer: bytearray) -> None:
        self._buffer = buffer


class _FakeStream:
    def __init__(self, buffer: bytearray) -> None:
        self.sender = _FakeStreamSender(buffer)


class _FakeQuicProtocol:
    def __init__(self) -> None:
        self.transmit_calls = 0
        self._quic = _FakeQuicConnection()
        self.close_calls: list[tuple[int, str]] = []

    def transmit(self) -> None:
        self.transmit_calls += 1

    def close(self, error_code: int = 0, reason_phrase: str = "") -> None:
        self.close_calls.append((error_code, reason_phrase))


class _DegradedRecorder:
    """Captures ``_WebTransportSession`` degraded-event emissions.

    Matches the :data:`_DegradedEmitter` signature so it can be injected in
    place of the bound ``WebTransportConnectionTransport._emit_degraded``.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []

    def __call__(self, reason: str, detail: str = "", *, fatal: bool = False) -> None:
        self.calls.append((reason, detail, fatal))

    @property
    def reasons(self) -> list[str]:
        return [c[0] for c in self.calls]


def _make_session(
    *,
    target_rate: int = 16000,
    in_max: int = 10,
    out_max: int = 10,
    emit_degraded: Any = None,
) -> tuple[_WebTransportSession, _FakeH3, asyncio.Queue, asyncio.Queue]:
    fake_h3 = _FakeH3()
    in_q: asyncio.Queue[AudioChunk | None] = asyncio.Queue(maxsize=in_max)
    out_q: asyncio.Queue[AudioChunk | None] = asyncio.Queue(maxsize=out_max)
    session = _WebTransportSession(
        h3=fake_h3,  # type: ignore[arg-type]
        quic_protocol=_FakeQuicProtocol(),  # type: ignore[arg-type]
        session_id=0,
        target_sample_rate=target_rate,
        audio_format=PCM16_MONO_16K,
        in_queue=in_q,
        out_queue=out_q,
        on_close=asyncio.Event(),
        emit_degraded=emit_degraded,
    )
    return session, fake_h3, in_q, out_q


def _build_connection_transport() -> WebTransportConnectionTransport:
    return WebTransportConnectionTransport(
        _h3=_FakeH3(),  # type: ignore[arg-type]
        _quic_protocol=_FakeQuicProtocol(),  # type: ignore[arg-type]
        _session_id=0,
    )
