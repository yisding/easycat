"""Tests for the WebTransport transport.

Coverage strategy:
  - Unit: ``_ControlCodec`` framing and ``_WebTransportSession`` stream
    dispatch via a fake :class:`H3Connection`.  No network.
  - Conformance: WebTransportTransport / WebTransportConnectionTransport
    satisfy the Transport protocol surface.
  - Integration: a loopback aioquic CONNECT-webtransport handshake against a
    real :class:`WebTransportServer` covering multi-client behaviour
    (marked ``integration_socket``).
"""

from __future__ import annotations

import asyncio
import json
import struct
from pathlib import Path
from typing import Any

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.providers import Transport
from easycat.transports.webtransport import (
    _TAG_AUDIO,
    _TAG_CONTROL,
    WebTransportConnectionTransport,
    WebTransportServer,
    WebTransportTransport,
    WebTransportTransportConfig,
    _ControlCodec,
    _WebTransportSession,
)

from .conftest import find_free_port

# ── _ControlCodec ─────────────────────────────────────────────────


class TestControlCodec:
    def test_encode_round_trip(self) -> None:
        codec = _ControlCodec()
        msg = {"type": "config", "sample_rate": 48000}
        encoded = _ControlCodec.encode(msg)
        (length,) = struct.unpack_from(">I", encoded, 0)
        assert length == len(encoded) - 4
        assert codec.feed(encoded) == [msg]

    def test_feed_partial_frames(self) -> None:
        codec = _ControlCodec()
        msg = {"type": "ready"}
        encoded = _ControlCodec.encode(msg)
        out: list[dict[str, Any]] = []
        for i in range(len(encoded)):
            out.extend(codec.feed(encoded[i : i + 1]))
        assert out == [msg]

    def test_feed_multiple_frames_in_one_chunk(self) -> None:
        codec = _ControlCodec()
        a = _ControlCodec.encode({"type": "start"})
        b = _ControlCodec.encode({"type": "stop"})
        assert codec.feed(a + b) == [{"type": "start"}, {"type": "stop"}]

    def test_malformed_json_is_skipped(self) -> None:
        codec = _ControlCodec()
        bad = struct.pack(">I", 4) + b"\xff\xff\xff\xff"
        good = _ControlCodec.encode({"type": "ready"})
        assert codec.feed(bad + good) == [{"type": "ready"}]


# ── _WebTransportSession with fake H3Connection ───────────────────


class _FakeH3:
    def __init__(self) -> None:
        self.sent: list[tuple[int, bytes]] = []
        self.next_stream_id = 1000

    def send_data(self, stream_id: int, data: bytes, end_stream: bool) -> None:  # noqa: FBT001
        self.sent.append((stream_id, data))

    def create_webtransport_stream(self, session_id: int, is_unidirectional: bool = False) -> int:
        sid = self.next_stream_id
        self.next_stream_id += 1
        return sid


class _FakeQuicConnection:
    """Records ``reset_stream`` calls so tests can assert barge-in semantics."""

    def __init__(self) -> None:
        self.resets: list[tuple[int, int]] = []

    def reset_stream(self, stream_id: int, error_code: int) -> None:
        self.resets.append((stream_id, error_code))


class _FakeQuicProtocol:
    def __init__(self) -> None:
        self.transmit_calls = 0
        self._quic = _FakeQuicConnection()

    def transmit(self) -> None:
        self.transmit_calls += 1


def _make_session(
    *,
    target_rate: int = 16000,
    in_max: int = 10,
    out_max: int = 10,
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
    )
    return session, fake_h3, in_q, out_q


class TestWebTransportSession:
    @pytest.mark.asyncio
    async def test_audio_tag_dispatches_inbound_pcm(self) -> None:
        session, _h3, in_q, _out_q = _make_session()
        pcm = b"\x00\x02\x00\x03\x00\x04"
        session.handle_stream_data(stream_id=4, data=bytes([_TAG_AUDIO]) + pcm, ended=False)
        chunk = in_q.get_nowait()
        assert isinstance(chunk, AudioChunk)
        assert chunk.data == pcm

    @pytest.mark.asyncio
    async def test_control_config_negotiates_sample_rate(self) -> None:
        session, _h3, in_q, _out_q = _make_session(target_rate=16000)
        msg = _ControlCodec.encode({"type": "config", "sample_rate": 48000})
        session.handle_stream_data(stream_id=8, data=bytes([_TAG_CONTROL]) + msg, ended=False)
        # 48 samples @ 48 kHz → 16 samples @ 16 kHz (32 bytes).
        pcm_48k = b"\x00\x00" * 48
        session.handle_stream_data(stream_id=4, data=bytes([_TAG_AUDIO]) + pcm_48k, ended=False)
        chunk = in_q.get_nowait()
        assert chunk.format.sample_rate == 16000
        assert len(chunk.data) == 32

    @pytest.mark.asyncio
    async def test_invalid_sample_rate_is_ignored(self) -> None:
        session, _h3, in_q, _out_q = _make_session(target_rate=16000)
        msg = _ControlCodec.encode({"type": "config", "sample_rate": -1})
        session.handle_stream_data(stream_id=8, data=bytes([_TAG_CONTROL]) + msg, ended=False)
        pcm = b"\x00\x01" * 8
        session.handle_stream_data(stream_id=4, data=bytes([_TAG_AUDIO]) + pcm, ended=False)
        chunk = in_q.get_nowait()
        assert chunk.format.sample_rate == 16000
        assert chunk.data == pcm

    @pytest.mark.asyncio
    async def test_inbound_queue_full_drops_frame(self) -> None:
        session, _h3, in_q, _out_q = _make_session(in_max=1)
        pcm = b"\x00\x00" * 4
        session.handle_stream_data(stream_id=4, data=bytes([_TAG_AUDIO]) + pcm, ended=False)
        session.handle_stream_data(stream_id=4, data=pcm, ended=False)
        assert in_q.qsize() == 1

    @pytest.mark.asyncio
    async def test_unknown_tag_is_ignored(self) -> None:
        session, _h3, in_q, _out_q = _make_session()
        session.handle_stream_data(stream_id=4, data=bytes([0xFF, 0x00]), ended=False)
        assert in_q.empty()

    @pytest.mark.asyncio
    async def test_outbound_writer_emits_audio_format_then_data(self) -> None:
        session, fake_h3, _in_q, out_q = _make_session()
        await session.start()
        try:
            chunk = AudioChunk(data=b"\x00\x01" * 4, format=PCM16_MONO_16K)
            await out_q.put(chunk)
            await asyncio.sleep(0.05)
        finally:
            await session.stop()

        bodies = [data for _sid, data in fake_h3.sent]
        decoded_control = []
        for data in bodies:
            if len(data) >= 4:
                (length,) = struct.unpack_from(">I", data, 0)
                if length + 4 == len(data):
                    try:
                        decoded_control.append(json.loads(data[4:].decode()))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        pass
        assert {"type": "ready"} in decoded_control
        assert {"type": "audio_format", "sample_rate": 16000} in decoded_control
        assert any(chunk.data in body for body in bodies)

    @pytest.mark.asyncio
    async def test_reset_audio_stream_aborts_in_flight_bytes(self) -> None:
        """After ``reset_audio_stream``, the next chunk opens a fresh stream."""
        session, fake_h3, _in_q, out_q = _make_session()
        await session.start()
        try:
            chunk = AudioChunk(data=b"\x00\x01" * 4, format=PCM16_MONO_16K)
            await out_q.put(chunk)
            await asyncio.sleep(0.05)
            first_audio_sid = session._audio_stream_id  # noqa: SLF001
            assert first_audio_sid is not None

            session.reset_audio_stream()
            assert session._audio_stream_id is None  # noqa: SLF001
            quic = session._quic_protocol._quic  # noqa: SLF001
            assert (first_audio_sid, 0) in quic.resets

            # Next chunk must allocate a new stream id.
            await out_q.put(chunk)
            await asyncio.sleep(0.05)
            second_audio_sid = session._audio_stream_id  # noqa: SLF001
            assert second_audio_sid is not None
            assert second_audio_sid != first_audio_sid
        finally:
            await session.stop()

    @pytest.mark.asyncio
    async def test_outbound_writer_signals_close_on_unexpected_error(self) -> None:
        """A crash in the writer must set ``on_close`` so the owning transport
        tears down instead of silently wedging."""
        session, fake_h3, _in_q, out_q = _make_session()
        await session.start()
        try:
            # Sabotage send_data after the initial ``ready`` control frame
            # so the next outbound audio chunk explodes inside the writer.
            def _explode(*_args, **_kwargs):
                raise RuntimeError("simulated send_data failure")

            fake_h3.send_data = _explode  # type: ignore[assignment]
            await out_q.put(AudioChunk(data=b"\x00\x01" * 4, format=PCM16_MONO_16K))
            await asyncio.wait_for(session._on_close.wait(), timeout=1)  # noqa: SLF001
        finally:
            await session.stop()


# ── Conformance: protocol shape and types ─────────────────────────


def _build_connection_transport() -> WebTransportConnectionTransport:
    return WebTransportConnectionTransport(
        _h3=_FakeH3(),  # type: ignore[arg-type]
        _quic_protocol=_FakeQuicProtocol(),  # type: ignore[arg-type]
        _session_id=0,
    )


class TestWebTransportConnectionTransport:
    def test_satisfies_transport_protocol(self) -> None:
        assert isinstance(_build_connection_transport(), Transport)

    def test_has_protocol_methods(self) -> None:
        t = _build_connection_transport()
        assert callable(t.connect)
        assert callable(t.disconnect)
        assert callable(t.receive_audio)
        assert callable(t.send_audio)
        assert callable(t.clear_audio)

    @pytest.mark.asyncio
    async def test_send_audio_returns_false_when_not_connected(self) -> None:
        t = _build_connection_transport()
        result = await t.send_audio(AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K))
        assert result is False

    @pytest.mark.asyncio
    async def test_clear_audio_drains_outbound_queue(self) -> None:
        t = _build_connection_transport()
        await t.connect()
        try:
            for _ in range(5):
                ok = await t.send_audio(AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K))
                assert ok
            await t.clear_audio()
            assert t._out_queue.qsize() == 0  # noqa: SLF001
        finally:
            await t.disconnect()

    @pytest.mark.asyncio
    async def test_send_audio_returns_false_when_queue_full(self) -> None:
        t = WebTransportConnectionTransport(
            config=WebTransportTransportConfig(outbound_max_pending=2),
            _h3=_FakeH3(),  # type: ignore[arg-type]
            _quic_protocol=_FakeQuicProtocol(),  # type: ignore[arg-type]
            _session_id=0,
        )
        await t.connect()
        try:
            await t._session.stop()  # noqa: SLF001
            assert await t.send_audio(AudioChunk(data=b"\x00", format=PCM16_MONO_16K))
            assert await t.send_audio(AudioChunk(data=b"\x00", format=PCM16_MONO_16K))
            assert not await t.send_audio(AudioChunk(data=b"\x00", format=PCM16_MONO_16K))
        finally:
            await t.disconnect()

    @pytest.mark.asyncio
    async def test_disconnect_enqueues_sentinel(self) -> None:
        t = _build_connection_transport()
        await t.connect()
        await t.disconnect()
        chunks = []
        async for c in t.receive_audio():
            chunks.append(c)
        assert chunks == []

    @pytest.mark.asyncio
    async def test_wait_closed_resolves_on_disconnect(self) -> None:
        t = _build_connection_transport()
        await t.connect()
        wait_task = asyncio.create_task(t.wait_closed(timeout=2))
        await t.disconnect()
        await wait_task

    @pytest.mark.asyncio
    async def test_connect_without_session_raises(self) -> None:
        t = WebTransportConnectionTransport()
        with pytest.raises(RuntimeError, match="no underlying session"):
            await t.connect()

    @pytest.mark.asyncio
    async def test_clear_audio_resets_in_flight_quic_stream(self) -> None:
        """``clear_audio`` must reset the QUIC audio stream, not just the app queue."""
        t = _build_connection_transport()
        await t.connect()
        try:
            # Send enough audio that the writer task allocates an audio stream.
            await t.send_audio(AudioChunk(data=b"\x00\x01" * 4, format=PCM16_MONO_16K))
            await asyncio.sleep(0.05)
            session = t._session  # noqa: SLF001
            assert session is not None
            audio_sid = session._audio_stream_id  # noqa: SLF001
            assert audio_sid is not None

            await t.clear_audio()
            quic = session._quic_protocol._quic  # noqa: SLF001
            assert (audio_sid, 0) in quic.resets
            assert session._audio_stream_id is None  # noqa: SLF001
        finally:
            await t.disconnect()

    @pytest.mark.asyncio
    async def test_send_audio_returns_false_after_disconnect(self) -> None:
        t = _build_connection_transport()
        await t.connect()
        await t.disconnect()
        result = await t.send_audio(AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K))
        assert result is False


# ── WebTransportTransport conformance ─────────────────────────────


class TestWebTransportTransportConformance:
    def test_has_protocol_methods(self) -> None:
        cfg = WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem")
        t = WebTransportTransport(cfg)
        assert callable(t.connect)
        assert callable(t.disconnect)
        assert callable(t.receive_audio)
        assert callable(t.send_audio)
        assert callable(t.clear_audio)

    def test_satisfies_transport_protocol(self) -> None:
        cfg = WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem")
        assert isinstance(WebTransportTransport(cfg), Transport)

    @pytest.mark.asyncio
    async def test_connect_requires_cert_files(self) -> None:
        t = WebTransportTransport()
        with pytest.raises(ValueError, match="certfile and keyfile"):
            await t.connect()

    @pytest.mark.asyncio
    async def test_pump_inbound_propagates_sentinel(self) -> None:
        """When the inner session terminates, the wrapper's ``receive_audio``
        must also stop iterating (regression for review #16).
        """
        outer_cfg = WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem")
        outer = WebTransportTransport(outer_cfg)

        # Build a fully-functional inner connection transport and disconnect
        # it after the pump is wired up.  The pump must drop its own sentinel
        # so a downstream consumer of ``outer.receive_audio()`` stops.
        inner = _build_connection_transport()
        await inner.connect()
        pump_task = asyncio.create_task(outer._pump_inbound(inner))  # noqa: SLF001
        await inner.disconnect()
        await asyncio.wait_for(pump_task, timeout=1)

        # Sentinel should now be queued; iterating receive_audio() exits.
        chunks = []
        async for c in outer.receive_audio():
            chunks.append(c)
        assert chunks == []


# ── WebTransportServer wiring (no network) ────────────────────────


class TestWebTransportServerWiring:
    @pytest.mark.asyncio
    async def test_start_requires_cert(self) -> None:
        async def _noop(transport: WebTransportConnectionTransport) -> None:
            await transport.wait_closed()

        server = WebTransportServer(WebTransportTransportConfig(), _noop)
        with pytest.raises(ValueError, match="certfile and keyfile"):
            await server.start()

    @pytest.mark.asyncio
    async def test_stop_is_idempotent_before_start(self) -> None:
        async def _noop(transport: WebTransportConnectionTransport) -> None:
            await transport.wait_closed()

        server = WebTransportServer(
            WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem"), _noop
        )
        await server.stop()

    @pytest.mark.asyncio
    async def test_stop_safe_when_called_from_within_handler(self) -> None:
        """A handler that triggers ``server.stop()`` mustn't deadlock by
        gathering its own task (regression for review #3/#8).
        """
        server = WebTransportServer(
            WebTransportTransportConfig(certfile="cert.pem", keyfile="key.pem"),
            lambda transport: asyncio.sleep(0),  # type: ignore[arg-type]
        )
        server._started = True  # noqa: SLF001 — fake "started"
        # Inject a handler task that is the current task.
        current = asyncio.current_task()
        assert current is not None
        server._handler_tasks.add(current)  # noqa: SLF001
        # Should return promptly without awaiting itself.
        await asyncio.wait_for(server.stop(), timeout=1)


# ── Top-level lazy exports ────────────────────────────────────────


def test_top_level_lazy_exports() -> None:
    import easycat

    assert hasattr(easycat, "WebTransportTransportConfig")
    assert hasattr(easycat, "WebTransportConnectionTransport")
    assert hasattr(easycat, "WebTransportServer")
    from easycat.transports import WebTransportTransport as _Wt  # noqa: F401


# ── Integration: loopback aioquic CONNECT handshake ───────────────


def _write_self_signed_pair(tmp: Path) -> tuple[Path, Path]:
    pytest.importorskip("cryptography")
    import datetime as _dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "localhost")],
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=1))
        .not_valid_after(_dt.datetime.now(_dt.UTC) + _dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp / "cert.pem"
    key_path = tmp / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def _udp_loopback_available() -> bool:
    """Both AF_INET and AF_INET6 UDP sockets are needed.

    aioquic's :func:`aioquic.asyncio.client.connect` unconditionally opens an
    IPv6 socket (relying on dual-stack to reach IPv4 hosts), so the
    integration test is skipped in environments that lack IPv6 entirely
    (e.g. some container sandboxes).
    """
    import socket

    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            s = socket.socket(family, socket.SOCK_DGRAM)
        except OSError:
            return False
        s.close()
    return True


def _aioquic_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("aioquic") is not None


@pytest.mark.integration_socket
@pytest.mark.skipif(
    not _aioquic_available(),
    reason="aioquic not installed ([webtransport] extra)",
)
@pytest.mark.skipif(not _udp_loopback_available(), reason="UDP loopback unavailable")
class TestWebTransportServerLoopback:
    """Drive real aioquic CONNECT-webtransport handshakes against the server.

    Exercises multi-client semantics: spawn two concurrent clients, verify
    each is handed its own ``WebTransportConnectionTransport`` and can round-
    trip PCM independently.
    """

    @staticmethod
    async def _run_one_client(
        port: int,
        cert_path: Path,
        pcm_in: bytes,
        result_audio: asyncio.Future[bytes],
    ) -> None:
        """Open one WebTransport session and send/recv one PCM frame."""
        from aioquic.asyncio.client import connect as quic_connect
        from aioquic.h3.connection import H3Connection
        from aioquic.h3.events import HeadersReceived as ClientHeadersReceived
        from aioquic.h3.events import WebTransportStreamDataReceived as ClientStreamData
        from aioquic.quic.configuration import QuicConfiguration

        client_quic_config = QuicConfiguration(
            alpn_protocols=["h3"],
            is_client=True,
            max_datagram_frame_size=65536,
        )
        client_quic_config.load_verify_locations(str(cert_path))

        async with quic_connect(
            "127.0.0.1",
            port,
            configuration=client_quic_config,
        ) as client_protocol:
            client_h3 = H3Connection(client_protocol._quic, enable_webtransport=True)
            events_q: asyncio.Queue = asyncio.Queue()
            original = client_protocol.quic_event_received

            def _dispatch(event):
                original(event)
                for h3_event in client_h3.handle_event(event):
                    events_q.put_nowait(h3_event)

            client_protocol.quic_event_received = _dispatch  # type: ignore[assignment]

            connect_stream_id = client_protocol._quic.get_next_available_stream_id()
            client_h3.send_headers(
                connect_stream_id,
                [
                    (b":method", b"CONNECT"),
                    (b":scheme", b"https"),
                    (b":authority", b"localhost"),
                    (b":path", b"/easycat"),
                    (b":protocol", b"webtransport"),
                    (b"sec-webtransport-http3-draft02", b"1"),
                ],
                end_stream=False,
            )
            client_protocol.transmit()

            async def _await_status_ok() -> None:
                while True:
                    ev = await events_q.get()
                    if isinstance(ev, ClientHeadersReceived):
                        status = dict(ev.headers).get(b":status")
                        assert status == b"200", f"unexpected status: {status!r}"
                        return

            await asyncio.wait_for(_await_status_ok(), timeout=5)

            audio_sid = client_h3.create_webtransport_stream(connect_stream_id)
            client_h3.send_data(audio_sid, bytes([_TAG_AUDIO]) + pcm_in, end_stream=False)
            client_protocol.transmit()

            received = bytearray()
            deadline = asyncio.get_event_loop().time() + 5
            while asyncio.get_event_loop().time() < deadline:
                try:
                    ev = await asyncio.wait_for(events_q.get(), timeout=1)
                except TimeoutError:
                    continue
                if isinstance(ev, ClientStreamData):
                    if not received and ev.data and ev.data[0] == _TAG_AUDIO:
                        received.extend(ev.data[1:])
                    elif received:
                        received.extend(ev.data)
                    if len(received) >= len(pcm_in):
                        break
            result_audio.set_result(bytes(received))

    @pytest.mark.asyncio
    async def test_two_concurrent_clients(self, tmp_path: Path) -> None:
        cert_path, key_path = _write_self_signed_pair(tmp_path)
        port = find_free_port()

        # Track handlers and their per-client transports.
        client_pcms: list[bytes] = []
        handler_started: list[asyncio.Event] = [asyncio.Event(), asyncio.Event()]

        async def handle(transport: WebTransportConnectionTransport) -> None:
            idx = len(client_pcms)
            client_pcms.append(b"")
            handler_started[idx].set()
            try:
                # Echo the first inbound frame back as TTS.
                async for chunk in transport.receive_audio():
                    client_pcms[idx] = chunk.data
                    await transport.send_audio(chunk)
                    break
                await transport.wait_closed()
            finally:
                pass

        server = WebTransportServer(
            WebTransportTransportConfig(
                host="127.0.0.1",
                port=port,
                certfile=str(cert_path),
                keyfile=str(key_path),
            ),
            handle,
        )
        await server.start()
        try:
            f1: asyncio.Future[bytes] = asyncio.get_event_loop().create_future()
            f2: asyncio.Future[bytes] = asyncio.get_event_loop().create_future()
            pcm_a = b"\x10\x00" * 8
            pcm_b = b"\x20\x00" * 8
            await asyncio.gather(
                self._run_one_client(port, cert_path, pcm_a, f1),
                self._run_one_client(port, cert_path, pcm_b, f2),
            )
            # Each client should have received its own echo back.
            echoed = sorted([f1.result(), f2.result()])
            sent = sorted([pcm_a, pcm_b])
            assert echoed == sent
            assert sorted(client_pcms) == sent
        finally:
            await server.stop()
