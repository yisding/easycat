"""WebRTC client stats endpoint and artifact tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import easycat.transports.webrtc as webrtc_mod
from easycat.transports._webrtc_stats import append_webrtc_stats_record
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
    async def test_stats_endpoint_enforces_record_limit_via_in_memory_counter(
        self, tmp_path, monkeypatch
    ):
        stats_path = tmp_path / "webrtc-stats.jsonl"
        transport = WebRTCTransport(
            WebRTCTransportConfig(stats_path=str(stats_path), stats_max_records=2)
        )
        transport._web = _FakeWeb

        # The append must run off the event loop via ``asyncio.to_thread`` and the
        # record-count quota must be served from an in-memory counter without
        # re-reading the whole JSONL artifact on every request.
        to_thread_calls = 0
        real_to_thread = webrtc_mod.asyncio.to_thread

        async def _counting_to_thread(func, *args, **kwargs):
            nonlocal to_thread_calls
            to_thread_calls += 1
            return await real_to_thread(func, *args, **kwargs)

        monkeypatch.setattr(webrtc_mod.asyncio, "to_thread", _counting_to_thread)

        def _post():
            return transport._handle_stats(
                _FakeSameOriginJsonRequest({"kind": "webrtc_client_stats", "schema_version": 1})
            )

        assert (await _post()).status == 200
        assert (await _post()).status == 200
        over_limit = await _post()

        assert over_limit.status == 429
        assert "record limit exceeded" in over_limit.text
        assert to_thread_calls == 2
        lines = stats_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2

    @pytest.mark.asyncio
    async def test_stats_endpoint_quota_check_is_atomic_under_concurrency(self, tmp_path):
        stats_path = tmp_path / "webrtc-stats.jsonl"
        transport = WebRTCTransport(
            WebRTCTransportConfig(stats_path=str(stats_path), stats_max_records=2)
        )
        transport._web = _FakeWeb

        # The append is offloaded via ``asyncio.to_thread``, so each handler
        # yields the loop between the quota check and the counter update.
        # Without the per-server write lock every concurrent post observes the
        # same pre-write counters and appends, blowing past ``stats_max_records``.
        responses = await asyncio.gather(
            *(
                transport._handle_stats(
                    _FakeSameOriginJsonRequest(
                        {"kind": "webrtc_client_stats", "schema_version": 1}
                    )
                )
                for _ in range(6)
            )
        )

        assert sorted(response.status for response in responses) == [200, 200, 429, 429, 429, 429]
        lines = stats_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2

    @pytest.mark.asyncio
    async def test_stats_endpoint_record_cap_resets_after_external_rotation(self, tmp_path):
        stats_path = tmp_path / "webrtc-stats.jsonl"
        transport = WebRTCTransport(
            WebRTCTransportConfig(stats_path=str(stats_path), stats_max_records=1)
        )
        transport._web = _FakeWeb

        def _post():
            return transport._handle_stats(
                _FakeSameOriginJsonRequest({"kind": "webrtc_client_stats", "schema_version": 1})
            )

        assert (await _post()).status == 200
        assert (await _post()).status == 429

        # An operator rotating the artifact must not brick /stats with 429s
        # until process restart: the cached in-memory count resets with it.
        stats_path.unlink()
        assert (await _post()).status == 200
        assert len(stats_path.read_text(encoding="utf-8").splitlines()) == 1

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
    async def test_stats_endpoint_drops_nonfinite_numbers(self, tmp_path):
        stats_path = tmp_path / "webrtc-stats.jsonl"
        transport = WebRTCTransport(WebRTCTransportConfig(stats_path=str(stats_path)))
        transport._web = _FakeWeb

        response = await transport._handle_stats(
            _FakeSameOriginJsonRequest(
                {
                    "sequence": float("nan"),
                    "inbound_audio": {
                        "jitter_ms": float("inf"),
                        "packets_received": 42,
                    },
                }
            )
        )

        assert response.status == 200
        line = stats_path.read_text(encoding="utf-8").strip()
        assert "NaN" not in line
        assert "Infinity" not in line
        assert json.loads(line)["inbound_audio"] == {"packets_received": 42}

    def test_stats_writer_rejects_unsanitized_nonfinite_numbers(self, tmp_path):
        stats_path = tmp_path / "webrtc-stats.jsonl"

        with pytest.raises(ValueError, match="Out of range float values"):
            append_webrtc_stats_record(stats_path, {"sequence": float("-inf")})

        assert not stats_path.exists()

    def test_bundled_client_posts_webrtc_stats_milestones(self):
        html_path = Path(webrtc_mod.__file__).with_name("static") / "webrtc_client.html"
        html = html_path.read_text(encoding="utf-8")

        # M7: the client paths are templated through a ``?webrtc=`` base so the
        # SAME page serves both the flat helper and the namespaced VoiceServer.
        assert 'fetch(baseUrl + WEBRTC_BASE + "/stats"' in html
        assert 'safeWebRTCBase(new URLSearchParams(location.search).get("webrtc") || "")' in html
        for label in (
            "before_speech",
            "client_speech_end",
            "first_received_audio",
            "teardown",
        ):
            assert f'postStatsSnapshot("{label}"' in html
