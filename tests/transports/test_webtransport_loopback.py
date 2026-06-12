"""WebTransport lazy exports and loopback integration tests."""

from __future__ import annotations

import asyncio
import contextlib
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from easycat.transports.webtransport import (
    _TAG_AUDIO,
    WebTransportConnectionTransport,
    WebTransportServer,
    WebTransportTransportConfig,
)

from ._webtransport_helpers import _aioquic_available


def test_top_level_lazy_exports() -> None:
    import easycat

    assert hasattr(easycat, "WebTransportTransportConfig")
    assert hasattr(easycat, "WebTransportConnectionTransport")
    assert hasattr(easycat, "WebTransportServer")
    from easycat.transports import (
        WebTransportTransport as _Wt,
    )
    from easycat.transports import (
        run_webtransport_config_server as _RunWt,
    )
    from easycat.transports import (
        serve_webtransport_config_sessions as _ServeWt,
    )

    assert _Wt
    assert _RunWt
    assert _ServeWt


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


@contextlib.asynccontextmanager
async def _wt_client(port: int, cert_path: Path):
    """Connect a WebTransport client whose protocol owns its own H3 layer.

    The stock ``QuicConnectionProtocol`` + a post-hoc ``quic_event_received``
    monkeypatch lets aioquic's default stream handling build (and then GC)
    asyncio ``StreamWriter`` objects for the server's QPACK/control
    unidirectional streams, which raise "Cannot send data on peer-initiated
    unidirectional stream" from ``StreamWriter.__del__``.  A protocol that
    owns H3 from construction and never chains to the base handler avoids that
    entirely (this mirrors the server's ``_EasyCatH3Protocol``).

    Yields the connected protocol; use ``client.h3`` and ``client.events``.
    """
    from aioquic.asyncio.client import connect as quic_connect
    from aioquic.asyncio.protocol import QuicConnectionProtocol
    from aioquic.h3.connection import H3Connection
    from aioquic.quic.configuration import QuicConfiguration

    cfg = QuicConfiguration(
        alpn_protocols=["h3"],
        is_client=True,
        max_datagram_frame_size=65536,
    )
    cfg.load_verify_locations(str(cert_path))
    # The self-signed cert's SAN is ``localhost``; we dial the 127.0.0.1
    # bind, so pin the TLS server name to what the cert actually attests.
    cfg.server_name = "localhost"

    class _ClientProtocol(QuicConnectionProtocol):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.h3 = H3Connection(self._quic, enable_webtransport=True)
            self.events: asyncio.Queue = asyncio.Queue()

        def quic_event_received(self, event: Any) -> None:
            for h3_event in self.h3.handle_event(event):
                self.events.put_nowait(h3_event)

    async with quic_connect(
        "127.0.0.1",
        port,
        configuration=cfg,
        create_protocol=_ClientProtocol,
    ) as client:
        yield client


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
        from aioquic.h3.events import HeadersReceived as ClientHeadersReceived
        from aioquic.h3.events import WebTransportStreamDataReceived as ClientStreamData

        async with _wt_client(port, cert_path) as client:
            client_h3 = client.h3
            events_q = client.events

            connect_stream_id = client._quic.get_next_available_stream_id()
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
            client.transmit()

            async def _await_status_ok() -> None:
                while True:
                    ev = await events_q.get()
                    if isinstance(ev, ClientHeadersReceived):
                        status = dict(ev.headers).get(b":status")
                        assert status == b"200", f"unexpected status: {status!r}"
                        return

            await asyncio.wait_for(_await_status_ok(), timeout=5)

            audio_sid = client_h3.create_webtransport_stream(connect_stream_id)
            # WebTransport stream payload is raw QUIC stream data, not an H3
            # DATA frame — mirror what the server/browser do.  Client→server
            # audio is self-describing: [tag][4-byte BE rate][PCM].
            client._quic.send_stream_data(
                audio_sid,
                bytes([_TAG_AUDIO]) + struct.pack(">I", 16000) + pcm_in,
                end_stream=False,
            )
            client.transmit()

            # Server→client audio is [0x01][4-byte BE rate][PCM]; the header
            # may be split across stream-data events, so accumulate per
            # stream id and strip the 5-byte header once enough has arrived.
            audio_sid: int | None = None
            audio_buf = bytearray()
            deadline = asyncio.get_event_loop().time() + 5
            while asyncio.get_event_loop().time() < deadline:
                try:
                    ev = await asyncio.wait_for(events_q.get(), timeout=1)
                except TimeoutError:
                    continue
                if isinstance(ev, ClientStreamData):
                    if audio_sid is None and ev.data and ev.data[0] == _TAG_AUDIO:
                        audio_sid = ev.stream_id
                        audio_buf.extend(ev.data)
                    elif ev.stream_id == audio_sid:
                        audio_buf.extend(ev.data)
                    if len(audio_buf) >= 5 + len(pcm_in):
                        break
            result_audio.set_result(bytes(audio_buf[5 : 5 + len(pcm_in)]))

    @pytest.mark.asyncio
    async def test_two_concurrent_clients(
        self,
        tmp_path: Path,
        unused_tcp_port_factory: Callable[[], int],
    ) -> None:
        cert_path, key_path = _write_self_signed_pair(tmp_path)
        port = unused_tcp_port_factory()

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
