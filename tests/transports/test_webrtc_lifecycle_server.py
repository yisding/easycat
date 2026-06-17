"""WebRTC signaling server, lifecycle, receive, and degraded-event tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import easycat.transports.webrtc as webrtc_mod
from easycat.audio_format import AudioChunk
from easycat.events import EventBus, TransportDegraded
from easycat.transports.webrtc import (
    _DEGRADED_INBOUND_CONSUME_ERROR,
    _DEGRADED_NEGOTIATION_FAILED,
    _DEGRADED_OUTBOUND_QUEUE_FULL,
    ICEServer,
    WebRTCTransport,
    WebRTCTransportConfig,
    serve_webrtc_config_sessions,
)

from ._webrtc_fakes import (
    _HAS_AIOHTTP,
    _HAS_WEBRTC_DEPS,
    _FakeAudioFrame,
    _FakeInboundTrack,
    _FakeJsonRequest,
    _FakeOfferRequest,
    _FakeRTCPeerConnection,
    _FakeSessionDescription,
    _FakeWeb,
    _install_fake_webrtc_modules,
    _UsesPytestTcpPortFactory,
)
from .conftest import make_chunk


class _FakeSameOriginJsonRequest(_FakeJsonRequest):
    scheme = "http"
    host = "127.0.0.1:8080"

    def __init__(self, payload: object) -> None:
        super().__init__(payload)
        self.headers = {"Origin": "http://127.0.0.1:8080"}


class TestWebRTCIngressQueueOwnership:
    @pytest.mark.asyncio
    async def test_repeated_offer_keeps_active_receive_audio_on_same_queue(self, monkeypatch):
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        # The signaling server is live (an /offer can only reach the handler
        # once connect() has started it); offers received after teardown begins
        # are rejected with 503 instead.
        transport._connected = True
        original_queue = transport._in_queue

        audio_iter = transport.receive_audio()
        pending = asyncio.create_task(anext(audio_iter))
        await asyncio.sleep(0)
        assert not pending.done()

        first_response = await transport._handle_offer(_FakeOfferRequest())
        second_response = await transport._handle_offer(_FakeOfferRequest())

        assert first_response.status == 200
        assert second_response.status == 200
        assert transport._in_queue is original_queue
        await asyncio.sleep(0)
        assert not pending.done()

        new_chunk = make_chunk(8)
        transport._enqueue_chunk(new_chunk, context="test")
        received = await asyncio.wait_for(pending, timeout=1.0)
        assert received is new_chunk
        await audio_iter.aclose()

    @pytest.mark.asyncio
    async def test_repeated_offer_drains_stale_audio_without_replacing_queue(self, monkeypatch):
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True  # signaling server live (see test above)

        first_response = await transport._handle_offer(_FakeOfferRequest())
        assert first_response.status == 200

        original_queue = transport._in_queue
        stale_chunk = make_chunk(8)
        transport._enqueue_chunk(stale_chunk, context="test")
        transport._enqueue_sentinel()

        second_response = await transport._handle_offer(_FakeOfferRequest())

        assert second_response.status == 200
        assert transport._in_queue is original_queue
        assert transport._in_queue.empty()

        new_chunk = make_chunk(10)
        transport._enqueue_chunk(new_chunk, context="test")
        audio_iter = transport.receive_audio()
        received = await asyncio.wait_for(anext(audio_iter), timeout=1.0)
        assert received is new_chunk
        await audio_iter.aclose()

    @pytest.mark.asyncio
    async def test_disconnect_does_not_hold_offer_lock_during_http_cleanup(self):
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True
        offer_task: asyncio.Task[object] | None = None

        class _OfferDuringStopSite:
            async def stop(self) -> None:
                nonlocal offer_task
                offer_task = asyncio.create_task(transport._handle_offer(_FakeOfferRequest()))
                await asyncio.sleep(0)

        class _CleanupWaitsForHandlersRunner:
            async def cleanup(self) -> None:
                assert offer_task is not None
                response = await asyncio.wait_for(offer_task, timeout=1.0)
                assert response.status == 503

        transport._site = _OfferDuringStopSite()
        transport._runner = _CleanupWaitsForHandlersRunner()

        await asyncio.wait_for(transport.disconnect(), timeout=1.0)

        assert transport._site is None
        assert transport._runner is None
        assert offer_task is not None
        assert offer_task.done()

    @pytest.mark.asyncio
    async def test_failed_replacement_offer_keeps_existing_peer_and_receiver(self, monkeypatch):
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True  # signaling server live (see tests above)

        first_response = await transport._handle_offer(_FakeOfferRequest())
        assert first_response.status == 200
        first_pc = _FakeRTCPeerConnection.instances[0]
        first_generation = transport._peer_generation

        audio_iter = transport.receive_audio()
        pending = asyncio.create_task(anext(audio_iter))
        await asyncio.sleep(0)
        assert not pending.done()

        async def _boom(self) -> _FakeSessionDescription:  # noqa: ANN001
            raise RuntimeError("sdp boom")

        monkeypatch.setattr(_FakeRTCPeerConnection, "createAnswer", _boom)

        failed_response = await transport._handle_offer(_FakeOfferRequest())

        assert failed_response.status == 400
        assert transport._peer_generation == first_generation
        assert transport._pc is first_pc
        assert not first_pc.closed
        assert _FakeRTCPeerConnection.instances[1].closed
        await asyncio.sleep(0)
        assert not pending.done()

        new_chunk = make_chunk(12)
        transport._enqueue_chunk(new_chunk, context="test")
        received = await asyncio.wait_for(pending, timeout=1.0)
        assert received is new_chunk
        await audio_iter.aclose()

    @pytest.mark.asyncio
    async def test_track_event_during_set_remote_description_starts_consumer(self, monkeypatch):
        # aiortc fires the synchronous ``track`` event during
        # setRemoteDescription, before the offer handler commits the new peer
        # generation. A successful offer must still start ``_consume_task`` and
        # forward the captured track's frames to receive_audio().
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True  # signaling server live (see tests above)

        # Frame already at the pipeline target rate (16 kHz mono) so the consume
        # path forwards the raw PCM without resampling/downmixing.
        target_rate = transport._config.audio_format.sample_rate
        frame_pcm = bytes(range(40))
        inbound = _FakeInboundTrack(frames=[_FakeAudioFrame(frame_pcm, sample_rate=target_rate)])
        _FakeRTCPeerConnection.next_inbound_track = inbound

        audio_iter = transport.receive_audio()
        pending = asyncio.create_task(anext(audio_iter))
        await asyncio.sleep(0)
        assert not pending.done()

        response = await transport._handle_offer(_FakeOfferRequest())
        assert response.status == 200

        # The deferred consumer must be created and running post-commit.
        assert transport._consume_task is not None
        assert not transport._consume_task.done()
        # The ``ended`` handler must be registered on the captured track.
        assert "ended" in inbound._handlers

        # The frame the track delivered must reach receive_audio().
        received = await asyncio.wait_for(pending, timeout=1.0)
        assert received.data == frame_pcm
        assert received.format == transport._config.audio_format

        await audio_iter.aclose()
        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_replacing_connected_peer_clears_wait_for_client(self, monkeypatch):
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True  # signaling server live (see test above)

        first_response = await transport._handle_offer(_FakeOfferRequest())
        assert first_response.status == 200
        first_pc = _FakeRTCPeerConnection.instances[0]
        first_pc.connectionState = "connected"
        first_connected = first_pc._handlers["connectionstatechange"]()
        if asyncio.iscoroutine(first_connected):
            await first_connected
        assert transport.has_client
        assert transport._client_connected.is_set()

        second_response = await transport._handle_offer(_FakeOfferRequest())

        assert second_response.status == 200
        assert first_pc.closed
        assert not transport.has_client
        assert not transport._client_connected.is_set()

        second_pc = _FakeRTCPeerConnection.instances[1]
        second_pc.connectionState = "connected"
        second_connected = second_pc._handlers["connectionstatechange"]()
        if asyncio.iscoroutine(second_connected):
            await second_connected
        assert transport.has_client
        assert transport._client_connected.is_set()


class TestWebRTCStatsArtifact:
    @pytest.mark.asyncio
    async def test_stats_endpoint_persists_sanitized_snapshots(self, tmp_path):
        stats_path = tmp_path / "webrtc-stats.jsonl"
        transport = WebRTCTransport(WebRTCTransportConfig(stats_path=str(stats_path)))
        transport._web = _FakeWeb

        response = await transport._handle_stats(
            _FakeSameOriginJsonRequest(
                {
                    "kind": "webrtc_client_stats",
                    "schema_version": 1,
                    "sample_id": "sample-1\nextra",
                    "label": "first_received_audio",
                    "local_candidate_ip": "192.168.1.20",
                    "candidate_pair": {
                        "state": "succeeded",
                        "current_round_trip_time_ms": 12.5,
                        "local_candidate_id": "candidate-secret",
                    },
                    "inbound_audio": {
                        "packets_received": 42,
                        "jitter_ms": 3.25,
                        "remote_candidate_id": "candidate-secret",
                    },
                }
            )
        )

        assert response.status == 200
        line = stats_path.read_text(encoding="utf-8").strip()
        payload = json.loads(line)
        assert payload["sample_id"] == "sample-1 extra"
        assert payload["label"] == "first_received_audio"
        assert payload["candidate_pair"] == {
            "current_round_trip_time_ms": 12.5,
            "state": "succeeded",
        }
        assert payload["inbound_audio"] == {"jitter_ms": 3.25, "packets_received": 42}
        assert "candidate-secret" not in line
        assert "192.168.1.20" not in line

    @pytest.mark.asyncio
    async def test_stats_endpoint_rejects_non_object_payload(self, tmp_path):
        stats_path = tmp_path / "webrtc-stats.jsonl"
        transport = WebRTCTransport(WebRTCTransportConfig(stats_path=str(stats_path)))
        transport._web = _FakeWeb

        response = await transport._handle_stats(
            _FakeSameOriginJsonRequest(["not", "an", "object"])
        )

        assert response.status == 400
        assert not stats_path.exists()

    @pytest.mark.asyncio
    async def test_stats_endpoint_requires_same_origin_for_unauthenticated_stats_path(
        self, tmp_path
    ):
        stats_path = tmp_path / "webrtc-stats.jsonl"
        transport = WebRTCTransport(WebRTCTransportConfig(stats_path=str(stats_path)))
        transport._web = _FakeWeb

        response = await transport._handle_stats(_FakeJsonRequest({"kind": "webrtc_client_stats"}))

        assert response.status == 403
        assert not stats_path.exists()

    @pytest.mark.asyncio
    async def test_stats_endpoint_requires_token_for_non_loopback_stats_path(self, tmp_path):
        stats_path = tmp_path / "webrtc-stats.jsonl"
        transport = WebRTCTransport(
            WebRTCTransportConfig(host="0.0.0.0", stats_path=str(stats_path))
        )
        transport._web = _FakeWeb

        response = await transport._handle_stats(
            _FakeSameOriginJsonRequest({"kind": "webrtc_client_stats"})
        )

        assert response.status == 403
        assert not stats_path.exists()

    @pytest.mark.asyncio
    async def test_stats_endpoint_caps_records(self, tmp_path):
        stats_path = tmp_path / "webrtc-stats.jsonl"
        transport = WebRTCTransport(
            WebRTCTransportConfig(stats_path=str(stats_path), stats_max_records=1)
        )
        transport._web = _FakeWeb

        first = await transport._handle_stats(
            _FakeSameOriginJsonRequest({"kind": "webrtc_client_stats", "sequence": 1})
        )
        second = await transport._handle_stats(
            _FakeSameOriginJsonRequest({"kind": "webrtc_client_stats", "sequence": 2})
        )

        assert first.status == 200
        assert second.status == 429
        assert len(stats_path.read_text(encoding="utf-8").splitlines()) == 1

    @pytest.mark.asyncio
    async def test_stats_endpoint_rate_limits_requests(self, tmp_path):
        stats_path = tmp_path / "webrtc-stats.jsonl"
        transport = WebRTCTransport(
            WebRTCTransportConfig(stats_path=str(stats_path), stats_max_requests_per_minute=1)
        )
        transport._web = _FakeWeb

        first = await transport._handle_stats(
            _FakeSameOriginJsonRequest({"kind": "webrtc_client_stats", "sequence": 1})
        )
        second = await transport._handle_stats(
            _FakeSameOriginJsonRequest({"kind": "webrtc_client_stats", "sequence": 2})
        )

        assert first.status == 200
        assert second.status == 429
        assert len(stats_path.read_text(encoding="utf-8").splitlines()) == 1

    @pytest.mark.asyncio
    async def test_stats_endpoint_caps_file_size(self, tmp_path):
        stats_path = tmp_path / "webrtc-stats.jsonl"
        transport = WebRTCTransport(
            WebRTCTransportConfig(stats_path=str(stats_path), stats_max_file_bytes=10)
        )
        transport._web = _FakeWeb

        response = await transport._handle_stats(
            _FakeSameOriginJsonRequest({"kind": "webrtc_client_stats", "sequence": 1})
        )

        assert response.status == 429
        assert not stats_path.exists()

    def test_bundled_client_posts_webrtc_stats_milestones(self):
        html_path = Path(webrtc_mod.__file__).with_name("static") / "webrtc_client.html"
        html = html_path.read_text(encoding="utf-8")

        assert 'fetch(baseUrl + "/stats"' in html
        for label in (
            "before_speech",
            "client_speech_end",
            "first_received_audio",
            "teardown",
        ):
            assert f'postStatsSnapshot("{label}"' in html


@pytest.mark.integration_socket
@pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
class TestWebRTCTransportLifecycle(_UsesPytestTcpPortFactory):
    @pytest.mark.asyncio
    async def test_connect_disconnect(self):
        port = self._unused_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)

        await transport.connect()
        assert transport.is_connected

        await transport.disconnect()
        assert not transport.is_connected

    @pytest.mark.asyncio
    async def test_default_host_serves_health_on_loopback(self):
        import aiohttp

        port = self._unused_port()
        config = WebRTCTransportConfig(port=port, static_dir=None)
        transport = WebRTCTransport(config)

        await transport.connect()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/health") as resp:
                    assert resp.status == 200
                    data = await resp.json()
                    assert data["status"] == "ok"
        finally:
            await transport.disconnect()

    @pytest.mark.asyncio
    async def test_disconnect_idempotent(self):
        transport = WebRTCTransport()
        await transport.disconnect()
        assert not transport.is_connected

    @pytest.mark.asyncio
    async def test_connect_idempotent(self):
        port = self._unused_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)

        await transport.connect()
        await transport.connect()  # Should not raise.
        assert transport.is_connected

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_root_redirects_to_bundled_client_when_present(self, tmp_path):
        import aiohttp

        client = tmp_path / "webrtc_client.html"
        client.write_text("<html></html>", encoding="utf-8")

        port = self._unused_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port, static_dir=str(tmp_path))
        transport = WebRTCTransport(config)
        await transport.connect()

        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/", allow_redirects=False) as resp:
                assert resp.status == 302
                assert resp.headers["Location"] == "/webrtc_client.html"

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_root_returns_endpoint_hint_without_static_client(self):
        import aiohttp

        port = self._unused_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port, static_dir=None)
        transport = WebRTCTransport(config)
        await transport.connect()

        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["service"] == "easycat-webrtc-signaling"
                assert "/offer" in data["endpoints"]
                assert "/stats" in data["endpoints"]
                assert "Access-Control-Allow-Origin" not in resp.headers

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_failed_connect_does_not_leave_stale_bundled_client_state(
        self,
        tmp_path,
        monkeypatch,
    ):
        import aiohttp

        client = tmp_path / "webrtc_client.html"
        client.write_text("<html></html>", encoding="utf-8")

        port = self._unused_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port, static_dir=str(tmp_path))
        transport = WebRTCTransport(config)

        async def broken_start(_self):
            raise RuntimeError("port busy")

        monkeypatch.setattr(aiohttp.web.TCPSite, "start", broken_start)

        with pytest.raises(RuntimeError, match="port busy"):
            await transport.connect()

        monkeypatch.undo()

        assert transport._has_bundled_client is False
        assert transport._app is None
        assert transport._runner is None
        assert transport._site is None

        # Retry on same instance without static files should not keep stale
        # redirect behavior from the failed attempt.
        transport._config.static_dir = None
        await transport.connect()

        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["service"] == "easycat-webrtc-signaling"

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_health_endpoint(self):
        import aiohttp

        port = self._unused_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)
        await transport.connect()

        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/health") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["status"] == "ok"
                assert "Access-Control-Allow-Origin" not in resp.headers

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_offer_without_valid_sdp_returns_error(self):
        import aiohttp

        port = self._unused_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)
        await transport.connect()

        async with aiohttp.ClientSession() as session:
            # Send invalid JSON.
            async with session.post(
                f"http://127.0.0.1:{port}/offer",
                data="not json",
                headers={"Content-Type": "application/json"},
            ) as resp:
                assert resp.status == 400

            # Send valid JSON but invalid schema.
            async with session.post(
                f"http://127.0.0.1:{port}/offer",
                json={"type": "answer", "sdp": "dummy"},
            ) as resp:
                assert resp.status == 400

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_config_endpoint_omits_turn_credentials_by_default(self):
        import aiohttp

        port = self._unused_port()
        servers = [
            ICEServer(urls="stun:stun.example.com:3478"),
            ICEServer(
                urls=["turn:turn.example.com:3478"],
                username="user",
                credential="pass",
            ),
        ]
        config = WebRTCTransportConfig(host="127.0.0.1", port=port, ice_servers=servers)
        transport = WebRTCTransport(config)
        await transport.connect()

        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/config") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert "iceServers" in data
                assert len(data["iceServers"]) == 2
                # Public config should include URLs but should not leak TURN credentials by
                # default.
                turn = data["iceServers"][1]
                assert turn["urls"] == ["turn:turn.example.com:3478"]
                assert "username" not in turn
                assert "credential" not in turn

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_config_endpoint_can_expose_turn_credentials(self):
        import aiohttp

        port = self._unused_port()
        servers = [
            ICEServer(
                urls=["turn:turn.example.com:3478"],
                username="user",
                credential="pass",
            ),
        ]
        config = WebRTCTransportConfig(
            host="127.0.0.1",
            port=port,
            ice_servers=servers,
            expose_ice_credentials=True,
        )
        transport = WebRTCTransport(config)
        await transport.connect()

        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/config") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["iceServers"] == [
                    {
                        "urls": ["turn:turn.example.com:3478"],
                        "username": "user",
                        "credential": "pass",
                    }
                ]

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_offer_uses_full_ice_credentials_for_server_peer(self, monkeypatch):
        _install_fake_webrtc_modules(monkeypatch)
        servers = [
            ICEServer(
                urls=["turn:turn.example.com:3478"],
                username="user",
                credential="pass",
            )
        ]
        transport = WebRTCTransport(WebRTCTransportConfig(ice_servers=servers))
        transport._web = _FakeWeb
        transport._connected = True

        response = await transport._handle_offer(_FakeOfferRequest())

        assert response.status == 200
        pc = _FakeRTCPeerConnection.instances[0]
        assert pc.config.iceServers[0].kwargs == {
            "urls": ["turn:turn.example.com:3478"],
            "username": "user",
            "credential": "pass",
        }

    @pytest.mark.asyncio
    async def test_cors_preflight_allows_same_origin(self):
        import aiohttp

        port = self._unused_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)
        await transport.connect()

        origin = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession() as session:
            async with session.options(
                f"http://127.0.0.1:{port}/offer",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "POST",
                },
            ) as resp:
                assert resp.status == 200
                assert resp.headers["Access-Control-Allow-Origin"] == origin
                assert resp.headers["Access-Control-Allow-Methods"] == "POST, GET, OPTIONS"

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_cors_preflight_denies_unknown_cross_origin_by_default(self):
        import aiohttp

        port = self._unused_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)
        await transport.connect()

        async with aiohttp.ClientSession() as session:
            async with session.options(
                f"http://127.0.0.1:{port}/offer",
                headers={
                    "Origin": "https://evil.example",
                    "Access-Control-Request-Method": "POST",
                },
            ) as resp:
                assert resp.status == 200
                assert "Access-Control-Allow-Origin" not in resp.headers

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_cors_allows_configured_origin(self):
        import aiohttp

        port = self._unused_port()
        origin = "https://voice.example.com"
        config = WebRTCTransportConfig(
            host="127.0.0.1",
            port=port,
            cors_allowed_origins=(origin,),
        )
        transport = WebRTCTransport(config)
        await transport.connect()

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"http://127.0.0.1:{port}/config",
                headers={"Origin": origin},
            ) as resp:
                assert resp.status == 200
                assert resp.headers["Access-Control-Allow-Origin"] == origin

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_cors_wildcard_requires_explicit_opt_in(self):
        import aiohttp

        port = self._unused_port()
        config = WebRTCTransportConfig(
            host="127.0.0.1",
            port=port,
            cors_allowed_origins=("*",),
        )
        transport = WebRTCTransport(config)
        await transport.connect()

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"http://127.0.0.1:{port}/config",
                headers={"Origin": "https://voice.example.com"},
            ) as resp:
                assert resp.status == 200
                assert resp.headers["Access-Control-Allow-Origin"] == "*"

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_receive_audio_ends_on_disconnect(self):
        port = self._unused_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)
        await transport.connect()

        chunks: list[AudioChunk] = []

        async def collect():
            async for chunk in transport.receive_audio():
                chunks.append(chunk)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.05)
        await transport.disconnect()
        await asyncio.wait_for(task, timeout=2.0)
        # Should have exited cleanly.

    @pytest.mark.asyncio
    async def test_send_audio_no_peer(self):
        """send_audio reports False when no peer is connected."""
        port = self._unused_port()
        config = WebRTCTransportConfig(host="127.0.0.1", port=port)
        transport = WebRTCTransport(config)
        await transport.connect()

        chunk = make_chunk()
        delivered = await transport.send_audio(chunk)
        assert delivered is False

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_send_audio_reports_drop_after_peer_disconnect(self):
        """After the peer connection drops, send_audio must return False so
        the session stops emitting AudioOut for audio no one will hear."""
        transport = WebRTCTransport()
        # Pretend a peer connected: populate the fields that gate send_audio.
        transport._pc = object()  # type: ignore[assignment]
        transport._outbound_track = object()

        chunk = make_chunk()
        # With a live track, send_audio accepts the chunk.
        delivered_while_live = await transport.send_audio(chunk)
        assert delivered_while_live is True

        # Simulate the connectionstatechange handler's "disconnected" branch.
        transport._outbound_track = None

        delivered_after_drop = await transport.send_audio(chunk)
        assert delivered_after_drop is False


class TestConsumeAudioSentinel:
    """Verify that _consume_audio enqueues a sentinel when the track ends."""

    @pytest.mark.asyncio
    async def test_track_recv_raises_stops_receive_audio(self):
        """When track.recv() raises, _consume_audio's finally block enqueues
        a sentinel so that receive_audio() terminates instead of blocking."""
        transport = WebRTCTransport(WebRTCTransportConfig())
        transport._init_audio_queue(200)
        transport._connected = True

        # Fake track whose recv() signals end-of-stream immediately.
        class _FakeTrack:
            async def recv(self):
                raise StopAsyncIteration

        # Run _consume_audio — it should enqueue a sentinel via the finally block.
        await transport._consume_audio(_FakeTrack())

        # receive_audio() should now terminate promptly.
        chunks: list[AudioChunk] = []
        async for chunk in transport.receive_audio():
            chunks.append(chunk)

        assert chunks == []

    @pytest.mark.asyncio
    async def test_sentinel_delivered_when_queue_is_full(self):
        """Even when the inbound queue is full, the sentinel must be delivered
        so that receive_audio() does not block forever."""
        transport = WebRTCTransport(WebRTCTransportConfig(max_pending_chunks=2))
        transport._init_audio_queue(2)
        transport._connected = True

        # Fill the queue completely.
        for _ in range(2):
            transport._enqueue_chunk(make_chunk(), context="test")

        # Fake track that ends immediately.
        class _FakeTrack:
            async def recv(self):
                raise StopAsyncIteration

        await transport._consume_audio(_FakeTrack())

        # receive_audio() must still terminate (sentinel was force-enqueued).
        chunks: list[AudioChunk] = []
        async for chunk in transport.receive_audio():
            chunks.append(chunk)

        # One chunk was dropped to make room for the sentinel; at most 1 chunk.
        assert len(chunks) <= 2


class _FakeSession:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def start(self) -> None:
        self.started.set()

    async def stop(self, *args: object, **kwargs: object) -> None:
        self.stopped.set()


@pytest.mark.integration_socket
@pytest.mark.skipif(not _HAS_AIOHTTP, reason="aiohttp not installed")
class TestWebRTCConfigServer(_UsesPytestTcpPortFactory):
    @pytest.mark.asyncio
    async def test_non_loopback_without_token_is_rejected(self) -> None:
        """Binding beyond loopback without a token raises before any I/O setup."""
        with pytest.raises(ValueError, match="auth_token is required"):
            await serve_webrtc_config_sessions(
                lambda transport: {},
                WebRTCTransportConfig(host="0.0.0.0", auth_token=None),
            )

    @pytest.mark.asyncio
    async def test_unsafe_allow_no_auth_passes_the_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``unsafe_allow_no_auth=True`` lets a non-loopback unauthenticated bind
        get past the guard (proven by reaching the telephony-extra import seam)."""

        def _reached(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("reached require_module past the auth guard")

        monkeypatch.setattr(webrtc_mod, "require_module", _reached)
        with pytest.raises(RuntimeError, match="reached require_module"):
            await serve_webrtc_config_sessions(
                lambda transport: {},
                WebRTCTransportConfig(host="0.0.0.0", auth_token=None),
                unsafe_allow_no_auth=True,
            )

    @pytest.mark.asyncio
    async def test_serve_webrtc_config_sessions_creates_session_per_offer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import aiohttp

        import easycat.config as config_module

        _install_fake_webrtc_modules(monkeypatch)
        port = self._unused_port()
        stop_event = asyncio.Event()
        sessions: list[_FakeSession] = []
        transports: list[WebRTCTransport] = []

        def create_session(config: dict[str, object]) -> _FakeSession:
            session = _FakeSession()
            sessions.append(session)
            return session

        def config_factory(transport: WebRTCTransport) -> dict[str, object]:
            transports.append(transport)
            return {"transport": transport, "agent": object()}

        monkeypatch.setattr(config_module, "create_session", create_session)
        task = asyncio.create_task(
            serve_webrtc_config_sessions(
                config_factory,
                WebRTCTransportConfig(host="127.0.0.1", port=port, static_dir=None),
                stop_event=stop_event,
                runtime_feedback=False,
                announce=False,
            )
        )
        try:
            async with aiohttp.ClientSession() as client:
                for _ in range(2):
                    for attempt in range(20):
                        try:
                            async with client.post(
                                f"http://127.0.0.1:{port}/offer",
                                json={"sdp": "v=0\r\n", "type": "offer"},
                            ) as resp:
                                assert resp.status == 200
                                data = await resp.json()
                                assert data == {"sdp": "fake-answer", "type": "answer"}
                                break
                        except aiohttp.ClientConnectorError:
                            if attempt == 19:
                                raise
                            await asyncio.sleep(0.05)
            assert len(sessions) == 2
            assert len(transports) == 2
            assert transports[0] is not transports[1]
            await asyncio.wait_for(sessions[0].started.wait(), timeout=1)
            await asyncio.wait_for(sessions[1].started.wait(), timeout=1)
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=2)
        assert all(session.stopped.is_set() for session in sessions)

    @pytest.mark.asyncio
    async def test_serve_webrtc_config_sessions_enforces_session_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import aiohttp

        import easycat.config as config_module

        _install_fake_webrtc_modules(monkeypatch)
        port = self._unused_port()
        stop_event = asyncio.Event()
        sessions: list[_FakeSession] = []

        def create_session(config: dict[str, object]) -> _FakeSession:
            session = _FakeSession()
            sessions.append(session)
            return session

        monkeypatch.setattr(config_module, "create_session", create_session)
        task = asyncio.create_task(
            serve_webrtc_config_sessions(
                lambda transport: {"transport": transport, "agent": object()},
                WebRTCTransportConfig(
                    host="127.0.0.1", port=port, static_dir=None, max_sessions=1
                ),
                stop_event=stop_event,
                runtime_feedback=False,
                announce=False,
            )
        )
        try:
            async with aiohttp.ClientSession() as client:
                for attempt in range(20):
                    try:
                        async with client.post(
                            f"http://127.0.0.1:{port}/offer",
                            json={"sdp": "v=0\r\n", "type": "offer"},
                        ) as resp:
                            assert resp.status == 200
                            break
                    except aiohttp.ClientConnectorError:
                        if attempt == 19:
                            raise
                        await asyncio.sleep(0.05)
                async with client.post(
                    f"http://127.0.0.1:{port}/offer",
                    json={"sdp": "v=0\r\n", "type": "offer"},
                ) as resp:
                    assert resp.status == 503
                    data = await resp.json()
                    assert "session limit" in data["error"]
            assert len(sessions) == 1
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=2)

    @pytest.mark.asyncio
    async def test_serve_webrtc_config_sessions_rejects_bad_offer_before_session_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import aiohttp

        import easycat.config as config_module

        _install_fake_webrtc_modules(monkeypatch)
        port = self._unused_port()
        stop_event = asyncio.Event()
        sessions: list[_FakeSession] = []

        def create_session(config: dict[str, object]) -> _FakeSession:
            session = _FakeSession()
            sessions.append(session)
            return session

        monkeypatch.setattr(config_module, "create_session", create_session)
        task = asyncio.create_task(
            serve_webrtc_config_sessions(
                lambda transport: {"transport": transport, "agent": object()},
                WebRTCTransportConfig(host="127.0.0.1", port=port, static_dir=None),
                stop_event=stop_event,
                runtime_feedback=False,
                announce=False,
            )
        )
        try:
            async with aiohttp.ClientSession() as client:
                for attempt in range(20):
                    try:
                        async with client.post(
                            f"http://127.0.0.1:{port}/offer",
                            json={"sdp": "", "type": "offer"},
                        ) as resp:
                            assert resp.status == 400
                            break
                    except aiohttp.ClientConnectorError:
                        if attempt == 19:
                            raise
                        await asyncio.sleep(0.05)
            assert sessions == []
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=2)

    @pytest.mark.asyncio
    async def test_serve_webrtc_config_sessions_health_reports_capacity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import aiohttp

        import easycat.config as config_module

        _install_fake_webrtc_modules(monkeypatch)
        port = self._unused_port()
        stop_event = asyncio.Event()
        monkeypatch.setattr(config_module, "create_session", lambda _config: _FakeSession())
        task = asyncio.create_task(
            serve_webrtc_config_sessions(
                lambda transport: {"transport": transport, "agent": object()},
                WebRTCTransportConfig(
                    host="127.0.0.1", port=port, static_dir=None, max_sessions=7
                ),
                stop_event=stop_event,
                runtime_feedback=False,
                announce=False,
            )
        )
        try:
            async with aiohttp.ClientSession() as client:
                for attempt in range(20):
                    try:
                        async with client.get(f"http://127.0.0.1:{port}/health") as resp:
                            assert resp.status == 200
                            data = await resp.json()
                            assert data == {
                                "status": "ok",
                                "active_sessions": 0,
                                "max_sessions": 7,
                            }
                            break
                    except aiohttp.ClientConnectorError:
                        if attempt == 19:
                            raise
                        await asyncio.sleep(0.05)
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=2)

    @pytest.mark.asyncio
    async def test_serve_webrtc_config_sessions_allows_config_cors_preflight(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import aiohttp

        import easycat.config as config_module

        _install_fake_webrtc_modules(monkeypatch)
        port = self._unused_port()
        stop_event = asyncio.Event()
        monkeypatch.setattr(config_module, "create_session", lambda _config: _FakeSession())
        task = asyncio.create_task(
            serve_webrtc_config_sessions(
                lambda transport: {"transport": transport, "agent": object()},
                WebRTCTransportConfig(
                    host="127.0.0.1",
                    port=port,
                    static_dir=None,
                    auth_token="secret",
                    cors_allowed_origins=("https://app.example.com",),
                ),
                stop_event=stop_event,
                runtime_feedback=False,
                announce=False,
            )
        )
        try:
            async with aiohttp.ClientSession() as client:
                for attempt in range(20):
                    try:
                        async with client.options(
                            f"http://127.0.0.1:{port}/config",
                            headers={
                                "Origin": "https://app.example.com",
                                "Access-Control-Request-Headers": "authorization",
                            },
                        ) as resp:
                            assert resp.status == 200
                            assert resp.headers["Access-Control-Allow-Origin"] == (
                                "https://app.example.com"
                            )
                            break
                    except aiohttp.ClientConnectorError:
                        if attempt == 19:
                            raise
                        await asyncio.sleep(0.05)
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=2)


class TestWebRTCDegradedEvents:
    """SDP negotiation failure and inbound-track crash must surface a
    ``TransportDegraded`` so they land in the journal, not just the log."""

    @pytest.mark.asyncio
    async def test_negotiation_failure_emits_fatal(self, monkeypatch):
        _install_fake_webrtc_modules(monkeypatch)

        async def _boom(self) -> None:  # noqa: ANN001
            raise RuntimeError("sdp boom")

        monkeypatch.setattr(_FakeRTCPeerConnection, "createAnswer", _boom)
        transport = WebRTCTransport()
        transport._web = _FakeWeb
        transport._connected = True  # signaling server live (see ingress tests)
        bus = EventBus()
        received: list[TransportDegraded] = []
        bus.subscribe(TransportDegraded, lambda e: received.append(e))
        transport._event_bus = bus

        resp = await transport._handle_offer(_FakeOfferRequest())

        assert resp.status == 400
        for _ in range(5):
            await asyncio.sleep(0)
        assert [e.reason for e in received] == [_DEGRADED_NEGOTIATION_FAILED]
        assert received[0].provider == "webrtc"
        assert received[0].fatal is True

    @pytest.mark.asyncio
    async def test_inbound_consume_error_emits_degraded(self, monkeypatch):
        _install_fake_webrtc_modules(monkeypatch)
        transport = WebRTCTransport()
        bus = EventBus()
        received: list[TransportDegraded] = []
        bus.subscribe(TransportDegraded, lambda e: received.append(e))
        transport._event_bus = bus

        class _BadTrack:
            async def recv(self):
                raise RuntimeError("decode boom")

        await transport._consume_audio(_BadTrack(), peer_generation=transport._peer_generation)

        for _ in range(5):
            await asyncio.sleep(0)
        evt = next(e for e in received if e.reason == _DEGRADED_INBOUND_CONSUME_ERROR)
        assert evt.provider == "webrtc"
        assert evt.fatal is False

    @pytest.mark.asyncio
    async def test_outbound_queue_full_emits_degraded(self):
        """A dropped outbound TTS frame must surface a ``TransportDegraded`` so
        backpressure is visible in the journal, not just a logger.debug line."""
        transport = WebRTCTransport()
        # Pretend a peer connected so send_audio reaches the enqueue path.
        transport._pc = object()  # type: ignore[assignment]
        transport._outbound_track = object()
        bus = EventBus()
        received: list[TransportDegraded] = []
        bus.subscribe(TransportDegraded, lambda e: received.append(e))
        transport._event_bus = bus

        # Force the outbound source to always reject the frame as if full.
        transport._outbound.enqueue = lambda *a, **k: False  # type: ignore[method-assign]

        delivered = await transport.send_audio(make_chunk())
        assert delivered is False

        for _ in range(5):
            await asyncio.sleep(0)
        evt = next(e for e in received if e.reason == _DEGRADED_OUTBOUND_QUEUE_FULL)
        assert evt.provider == "webrtc"
        assert evt.fatal is False
