from __future__ import annotations

import pytest

from easycat.audio_format import AudioChunk


class _DummyAgent:
    async def run(self, text: str) -> str:
        return text


class _DummyWebSocket:
    async def send(self, _message):
        return None

    async def close(self):
        return None


class _CapabilityTransportConfig:
    default_echo_cancellation_enabled = True


class _IdentitySinkTransport:
    def __init__(self) -> None:
        self.identity_sink = None

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive_audio(self):
        return
        yield

    async def send_audio(self, chunk: AudioChunk) -> bool:
        return True

    async def clear_audio(self) -> None:
        pass

    def bind_identity_sink(self, sink) -> None:
        self.identity_sink = sink

    def version_info(self) -> dict[str, str]:
        return {"provider": "identity-sink"}


def _stub_audio_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub VAD/noise reduction so create_session does not need real backends."""

    class _VAD:
        async def process(self, chunk):
            if False:
                yield chunk

        def configure(self, **kwargs):
            pass

    class _NoiseReducer:
        async def process(self, chunk):
            return chunk

    monkeypatch.setattr("easycat.config._factory.create_vad", lambda *_a, **_k: _VAD())
    monkeypatch.setattr(
        "easycat.config._factory.create_noise_reducer", lambda *_a, **_k: _NoiseReducer()
    )
