"""WebRTC client stats endpoint and artifact tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import easycat.transports.webrtc as webrtc_mod
from easycat.transports.webrtc import WebRTCTransport, WebRTCTransportConfig

from ._webrtc_fakes import _FakeJsonRequest, _FakeWeb


class _FakeSameOriginJsonRequest(_FakeJsonRequest):
    scheme = "http"
    host = "127.0.0.1:8080"

    def __init__(self, payload: object) -> None:
        super().__init__(payload)
        self.headers = {"Origin": "http://127.0.0.1:8080"}


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

    def test_bundled_client_posts_webrtc_stats_milestones(self):
        html_path = Path(webrtc_mod.__file__).with_name("static") / "webrtc_client.html"
        html = html_path.read_text(encoding="utf-8")

        # M7: the client paths are templated through a ``?webrtc=`` base so the
        # SAME page serves both the flat helper and the namespaced VoiceServer.
        assert 'fetch(baseUrl + WEBRTC_BASE + "/stats"' in html
        for label in (
            "before_speech",
            "client_speech_end",
            "first_received_audio",
            "teardown",
        ):
            assert f'postStatsSnapshot("{label}"' in html
