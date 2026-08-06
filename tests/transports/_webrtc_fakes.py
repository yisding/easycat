"""Shared fake aiortc/aiohttp objects for WebRTC transport tests."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
from collections.abc import Callable

import pytest

import easycat.transports._webrtc_audio as webrtc_audio_mod
import easycat.transports.webrtc as webrtc_mod

_HAS_AIORTC = importlib.util.find_spec("aiortc") is not None
_HAS_AIOHTTP = importlib.util.find_spec("aiohttp") is not None
_HAS_WEBRTC_DEPS = _HAS_AIORTC and _HAS_AIOHTTP


class _UsesPytestTcpPortFactory:
    _unused_tcp_port_factory: Callable[[], int]

    @pytest.fixture(autouse=True)
    def _set_unused_tcp_port_factory(
        self,
        unused_tcp_port_factory: Callable[[], int],
    ) -> None:
        self._unused_tcp_port_factory = unused_tcp_port_factory

    def _unused_port(self) -> int:
        return self._unused_tcp_port_factory()


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        text: str = "",
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.text = text
        self.content_type = content_type
        self.headers = headers or {}


class _FakeWeb:
    Response = _FakeResponse


class _FakeOfferRequest:
    async def json(self) -> dict[str, str]:
        return {"sdp": "v=0\r\n", "type": "offer"}


class _FakeJsonRequest:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    async def json(self) -> object:
        return self._payload


class _FakeSessionDescription:
    def __init__(self, *, sdp: str, type: str) -> None:
        self.sdp = sdp
        self.type = type


class _FakeRTCConfiguration:
    def __init__(self, *, iceServers: list[object]) -> None:
        self.iceServers = iceServers


class _FakeRTCIceServer:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


class _FakeMediaStreamTrack:
    def __init__(self) -> None:
        pass


class _FakeMediaStreamError(Exception):
    pass


class _FakeInboundTrack:
    """Fake remote audio track delivered via the synchronous ``track`` event.

    ``recv()`` yields each queued frame once, then blocks forever so the
    consumer task stays alive (mirroring a live RTP stream that has not yet
    ended).
    """

    kind = "audio"

    def __init__(self, frames: list[object] | None = None) -> None:
        self._frames: list[object] = list(frames or [])
        self._handlers: dict[str, object] = {}

    def on(self, event: str):
        def decorator(callback):
            self._handlers[event] = callback
            return callback

        return decorator

    async def recv(self) -> object:
        if self._frames:
            return self._frames.pop(0)
        # No more frames: block forever so the consume task does not exit and
        # enqueue a sentinel that would prematurely end receive_audio().
        await asyncio.Event().wait()


class _FakeAudioFrame:
    """Minimal av.AudioFrame stand-in for the inbound decode path."""

    def __init__(self, pcm: bytes, *, sample_rate: int = 48000) -> None:
        self.planes = [pcm]
        self.sample_rate = sample_rate
        self.layout = None


class _FakeRTCPeerConnection:
    instances: list[_FakeRTCPeerConnection] = []  # noqa: RUF012 test fake uses shared class fixture

    # When set on the class before an offer, the next constructed peer fires
    # its registered ``track`` handler synchronously during
    # ``setRemoteDescription`` with this track — mirroring aiortc, which emits
    # ``track`` before the offer handler publishes the new peer.
    next_inbound_track: object | None = None

    def __init__(self, config: _FakeRTCConfiguration) -> None:
        self.config = config
        self.connectionState = "new"
        self.iceGatheringState = "complete"
        self.localDescription: _FakeSessionDescription | None = None
        self.remoteDescription: _FakeSessionDescription | None = None
        self.closed = False
        self.tracks: list[object] = []
        self._handlers: dict[str, object] = {}
        self._inbound_track = type(self).next_inbound_track
        type(self).next_inbound_track = None
        self.instances.append(self)

    def addTrack(self, track: object) -> None:
        self.tracks.append(track)

    def on(self, event: str):
        def decorator(callback):
            self._handlers[event] = callback
            return callback

        return decorator

    async def setRemoteDescription(self, offer: _FakeSessionDescription) -> None:
        self.remoteDescription = offer
        # aiortc fires the synchronous ``track`` event during
        # setRemoteDescription — before the offer handler commits the new
        # peer. Replicate that ordering so regressions in the deferred
        # consume-task start are caught.
        if self._inbound_track is not None:
            callback = self._handlers.get("track")
            if callback is not None:
                callback(self._inbound_track)

    async def createAnswer(self) -> _FakeSessionDescription:
        return _FakeSessionDescription(sdp="fake-answer", type="answer")

    async def setLocalDescription(self, answer: _FakeSessionDescription) -> None:
        self.localDescription = answer

    async def close(self) -> None:
        self.closed = True
        self.connectionState = "closed"
        callback = self._handlers.get("connectionstatechange")
        if callback is not None:
            result = callback()
            if asyncio.iscoroutine(result):
                await result
        await asyncio.sleep(0)


class _FakeAiortc:
    MediaStreamError = _FakeMediaStreamError
    MediaStreamTrack = _FakeMediaStreamTrack
    RTCConfiguration = _FakeRTCConfiguration
    RTCIceServer = _FakeRTCIceServer
    RTCPeerConnection = _FakeRTCPeerConnection
    RTCSessionDescription = _FakeSessionDescription


def _install_fake_webrtc_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeRTCPeerConnection.instances.clear()
    _FakeRTCPeerConnection.next_inbound_track = None

    def fake_require_module(name: str, **_: object) -> object:
        if name == "aiortc":
            return _FakeAiortc
        if name == "aiohttp.web":
            return importlib.import_module(name)
        raise AssertionError(f"unexpected module request: {name}")

    monkeypatch.setattr(webrtc_mod, "require_module", fake_require_module)
    monkeypatch.setattr(webrtc_audio_mod, "require_module", fake_require_module)


class _FakeEventsChannel:
    label = "events"

    def __init__(self, ready_state: str = "open") -> None:
        self.readyState = ready_state
        self.sent: list[str] = []

    def send(self, data: str) -> None:
        self.sent.append(data)


class _FakeAuthorizedOfferRequest(_FakeOfferRequest):
    def __init__(self, token: str) -> None:
        self.headers = {"Authorization": f"Bearer {token}"}


class _FakeQueryTokenStatsRequest(_FakeJsonRequest):
    def __init__(self, payload: object, token: str) -> None:
        super().__init__(payload)
        self.query = {"token": token}
