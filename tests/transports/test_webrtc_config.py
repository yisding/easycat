"""WebRTC transport configuration and conformance tests."""

from __future__ import annotations

import pytest

from easycat.audio_format import PCM16_MONO_16K
from easycat.transports.webrtc import (
    ICEServer,
    WebRTCTransport,
    WebRTCTransportConfig,
    webrtc_ice_servers_from_env,
    webrtc_transport_config_from_env,
)


class TestWebRTCTransportConfig:
    def test_defaults(self):
        config = WebRTCTransportConfig()
        assert config.host == "127.0.0.1"
        assert config.port == 8080
        assert config.audio_format == PCM16_MONO_16K
        assert config.max_pending_chunks == 200
        assert config.static_dir == WebRTCTransportConfig._USE_BUNDLED
        assert len(config.ice_servers) == 1
        assert "stun:" in config.ice_servers[0].urls[0]
        assert config.cors_allowed_origins == ()
        assert config.stats_path is None

    @pytest.mark.asyncio
    async def test_non_loopback_bind_requires_auth_token(self):
        transport = WebRTCTransport(WebRTCTransportConfig(host="0.0.0.0"))

        with pytest.raises(ValueError, match="auth_token"):
            await transport.connect()

    def test_stats_path_defaults_from_validation_env(self, monkeypatch):
        monkeypatch.setenv("EASYCAT_WEBRTC_STATS_PATH", "/tmp/easycat-webrtc-stats.jsonl")

        config = WebRTCTransportConfig()

        assert config.stats_path == "/tmp/easycat-webrtc-stats.jsonl"

    def test_custom_ice_servers(self):
        servers = [
            ICEServer(urls="stun:stun.example.com:3478"),
            ICEServer(
                urls=["turn:turn.example.com:3478", "turns:turn.example.com:5349"],
                username="user",
                credential="pass",
            ),
        ]
        config = WebRTCTransportConfig(ice_servers=servers)
        assert len(config.ice_servers) == 2
        assert config.ice_servers[1].username == "user"
        assert config.ice_servers[1].credential == "pass"

    def test_ice_server_single_url_normalized_to_list(self):
        srv = ICEServer(urls="stun:stun.l.google.com:19302")
        assert srv.urls == ["stun:stun.l.google.com:19302"]
        assert srv.username is None
        assert srv.credential is None

    def test_ice_server_multiple_urls(self):
        srv = ICEServer(urls=["turn:a.example.com:3478", "turn:b.example.com:3478"])
        assert isinstance(srv.urls, list)
        assert len(srv.urls) == 2

    def test_env_config_defaults_to_loopback_and_public_stun(self, monkeypatch):
        for name in (
            "SIGNALING_HOST",
            "SIGNALING_PORT",
            "TURN_SERVER_URL",
            "TURN_USERNAME",
            "TURN_CREDENTIAL",
            "WEBRTC_EXPOSE_ICE_CREDENTIALS",
            "WEBRTC_SIGNALING_TOKEN",
        ):
            monkeypatch.delenv(name, raising=False)

        config = webrtc_transport_config_from_env()

        assert config.host == "127.0.0.1"
        assert config.port == 8080
        assert config.static_dir == WebRTCTransportConfig._USE_BUNDLED
        assert config.auth_token is None
        assert config.expose_ice_credentials is False
        assert len(config.ice_servers) == 1
        assert config.ice_servers[0].urls == ["stun:stun.l.google.com:19302"]

    def test_env_config_reads_turn_and_signaling_env(self, monkeypatch):
        monkeypatch.setenv("SIGNALING_HOST", "0.0.0.0")
        monkeypatch.setenv("SIGNALING_PORT", "9090")
        monkeypatch.setenv("TURN_SERVER_URL", "turn:example.com:3478")
        monkeypatch.setenv("TURN_USERNAME", "turn-user")
        monkeypatch.setenv("TURN_CREDENTIAL", "turn-secret")
        monkeypatch.setenv("WEBRTC_EXPOSE_ICE_CREDENTIALS", "yes")
        monkeypatch.setenv("WEBRTC_SIGNALING_TOKEN", "signaling-secret")

        config = webrtc_transport_config_from_env(static_dir="/tmp/web")

        assert config.host == "0.0.0.0"
        assert config.port == 9090
        assert config.static_dir == "/tmp/web"
        assert config.auth_token == "signaling-secret"
        assert config.expose_ice_credentials is True
        assert [server.urls[0] for server in config.ice_servers] == [
            "stun:stun.l.google.com:19302",
            "turn:example.com:3478",
        ]
        assert config.ice_servers[1].username == "turn-user"
        assert config.ice_servers[1].credential == "turn-secret"

    def test_env_ice_server_helper_can_skip_public_stun(self, monkeypatch):
        monkeypatch.setenv("TURN_SERVER_URL", "turn:example.com:3478")

        servers = webrtc_ice_servers_from_env(include_public_stun=False)

        assert len(servers) == 1
        assert servers[0].urls == ["turn:example.com:3478"]


class TestWebRTCTransportConformance:
    def test_has_protocol_methods(self):
        t = WebRTCTransport()
        assert callable(t.connect)
        assert callable(t.disconnect)
        assert callable(t.receive_audio)
        assert callable(t.send_audio)
        assert callable(t.clear_audio)

    def test_is_transport_protocol(self):
        from easycat.providers import Transport

        t = WebRTCTransport()
        assert isinstance(t, Transport)

    def test_initial_state(self):
        t = WebRTCTransport()
        assert not t.is_connected
        assert not t.has_client

    def test_echo_cancellation_default_is_on(self):
        # Deliberate flip from the prior implicit ``False`` default: WebRTC is a
        # browser-mic transport and matches WebSocket's EasyCat-side AEC default.
        from easycat.runtime.capabilities import default_echo_cancellation_enabled

        assert default_echo_cancellation_enabled(WebRTCTransport()) is True
