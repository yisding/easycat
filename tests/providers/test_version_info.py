"""CI guard: every provider class must have a working version_info() method."""

from __future__ import annotations

from easycat._provider_helpers import get_package_version
from easycat.stt.base import STTBase
from easycat.stt.deepgram_provider import DeepgramSTT, DeepgramSTTConfig
from easycat.stt.elevenlabs_provider import ElevenLabsSTT, ElevenLabsSTTConfig
from easycat.stt.openai_provider import OpenAISTT, OpenAISTTConfig
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTT, OpenAIRealtimeSTTConfig
from easycat.transports._base import AudioQueueMixin, make_version_info
from easycat.transports.local import LocalTransport
from easycat.transports.telnyx_media import TelnyxConnectionTransport, TelnyxTransport
from easycat.transports.twilio_media import TwilioConnectionTransport, TwilioTransport
from easycat.transports.webrtc import WebRTCTransport
from easycat.transports.websocket import WebSocketConnectionTransport, WebSocketTransport
from easycat.transports.webtransport import (
    WebTransportConnectionTransport,
    WebTransportTransport,
)
from easycat.tts.base import TTSBase
from easycat.tts.deepgram_tts import DeepgramTTS, DeepgramTTSConfig
from easycat.tts.elevenlabs_tts import ElevenLabsTTS, ElevenLabsTTSConfig
from easycat.tts.openai_tts import OpenAITTS, OpenAITTSConfig

EXPECTED_KEYS = {"provider", "model", "api_version", "sdk_version"}


class TestBaseClassesHaveVersionInfo:
    def test_stt_base(self):
        info = STTBase().version_info()
        assert set(info.keys()) == EXPECTED_KEYS

    def test_tts_base(self):
        info = TTSBase().version_info()
        assert set(info.keys()) == EXPECTED_KEYS

    def test_transport_base(self):
        # AudioQueueMixin isn't instantiated directly, but check the method exists.
        assert hasattr(AudioQueueMixin, "version_info")


class TestSTTProviderVersionInfo:
    """Each STT provider returns stable-shape version_info."""

    def test_openai_stt(self):
        config = OpenAISTTConfig(api_key="test")
        info = OpenAISTT(config).version_info()
        assert set(info.keys()) == EXPECTED_KEYS
        assert info["provider"] == "openai"
        assert info["model"] == "gpt-4o-transcribe"

    def test_openai_realtime_stt(self):
        config = OpenAIRealtimeSTTConfig(api_key="test")
        info = OpenAIRealtimeSTT(config).version_info()
        assert set(info.keys()) == EXPECTED_KEYS
        assert info["provider"] == "openai-realtime"
        assert info["model"] == "gpt-realtime-whisper"

    def test_deepgram_stt(self):
        config = DeepgramSTTConfig(api_key="test")
        info = DeepgramSTT(config).version_info()
        assert set(info.keys()) == EXPECTED_KEYS
        assert info["provider"] == "deepgram"
        assert info["model"] == "nova-2"

    def test_elevenlabs_stt(self):
        config = ElevenLabsSTTConfig(api_key="test")
        info = ElevenLabsSTT(config).version_info()
        assert set(info.keys()) == EXPECTED_KEYS
        assert info["provider"] == "elevenlabs"


class TestTTSProviderVersionInfo:
    """Each TTS provider returns stable-shape version_info."""

    def test_openai_tts(self):
        config = OpenAITTSConfig(api_key="test")
        info = OpenAITTS(config).version_info()
        assert set(info.keys()) == EXPECTED_KEYS
        assert info["provider"] == "openai"
        assert info["model"] == "gpt-4o-mini-tts"

    def test_deepgram_tts(self):
        config = DeepgramTTSConfig(api_key="test")
        info = DeepgramTTS(config).version_info()
        assert set(info.keys()) == EXPECTED_KEYS
        assert info["provider"] == "deepgram"

    def test_elevenlabs_tts(self):
        config = ElevenLabsTTSConfig(api_key="test")
        info = ElevenLabsTTS(config).version_info()
        assert set(info.keys()) == EXPECTED_KEYS
        assert info["provider"] == "elevenlabs"


class TestTransportVersionInfo:
    """Each transport returns stable-shape version_info."""

    def test_local_transport(self):
        info = LocalTransport().version_info()
        assert set(info.keys()) == EXPECTED_KEYS
        assert info["provider"] == "local"

    def test_websocket_transport(self):
        info = WebSocketTransport().version_info()
        assert set(info.keys()) == EXPECTED_KEYS
        assert info["provider"] == "websocket"

    def test_webrtc_transport(self):
        info = WebRTCTransport().version_info()
        assert set(info.keys()) == EXPECTED_KEYS
        assert info["provider"] == "webrtc"

    def test_twilio_transport(self):
        # version_info() does not touch instance state; invoke unbound.
        info = TwilioTransport.version_info(None)
        assert set(info.keys()) == EXPECTED_KEYS
        assert info["provider"] == "twilio"

    def test_telnyx_transport(self):
        info = TelnyxTransport.version_info(None)
        assert set(info.keys()) == EXPECTED_KEYS
        assert info["provider"] == "telnyx"

    def test_telnyx_connection_transport(self):
        info = TelnyxConnectionTransport.version_info(None)
        assert set(info.keys()) == EXPECTED_KEYS
        assert info["provider"] == "telnyx-connection"

    def test_twilio_connection_transport(self):
        info = TwilioConnectionTransport.version_info(None)
        assert set(info.keys()) == EXPECTED_KEYS
        assert info["provider"] == "twilio-connection"

    def test_websocket_connection_transport(self):
        info = WebSocketConnectionTransport.version_info(None)
        assert set(info.keys()) == EXPECTED_KEYS
        assert info["provider"] == "websocket-connection"

    def test_webtransport_transport(self):
        info = WebTransportTransport.version_info(None)
        assert set(info.keys()) == EXPECTED_KEYS
        assert info["provider"] == "webtransport"
        assert info["api_version"] == "h3"

    def test_webtransport_connection_transport(self):
        info = WebTransportConnectionTransport.version_info(None)
        assert set(info.keys()) == EXPECTED_KEYS
        assert info["provider"] == "webtransport-connection"
        assert info["api_version"] == "h3"


class TestVersionInfoHelpers:
    """Unit coverage for the shared ``_base`` version helpers."""

    def test_sdk_version_unknown_for_missing_package(self):
        assert get_package_version("definitely-not-a-real-pkg") == "unknown"

    def test_make_version_info_shape_and_api_version(self):
        info = make_version_info("x", "definitely-not-a-real-pkg", api_version="h3")
        assert set(info.keys()) == EXPECTED_KEYS
        assert info["provider"] == "x"
        assert info["model"] == "unknown"
        assert info["api_version"] == "h3"
        assert info["sdk_version"] == "unknown"

    def test_make_version_info_default_api_version(self):
        info = make_version_info("y", "definitely-not-a-real-pkg")
        assert info["api_version"] == "unknown"


class TestVersionInfoShapeInvariant:
    """All values must be strings and no key may be missing."""

    def _check(self, info: dict[str, str]) -> None:
        assert set(info.keys()) == EXPECTED_KEYS
        for k, v in info.items():
            assert isinstance(v, str), f"{k} should be str, got {type(v)}"
            assert v != "", f"{k} should not be empty"

    def test_all_stt(self):
        for cfg_cls, prov_cls in [
            (OpenAISTTConfig, OpenAISTT),
            (OpenAIRealtimeSTTConfig, OpenAIRealtimeSTT),
            (DeepgramSTTConfig, DeepgramSTT),
            (ElevenLabsSTTConfig, ElevenLabsSTT),
        ]:
            self._check(prov_cls(cfg_cls(api_key="test")).version_info())

    def test_all_tts(self):
        for cfg_cls, prov_cls in [
            (OpenAITTSConfig, OpenAITTS),
            (DeepgramTTSConfig, DeepgramTTS),
            (ElevenLabsTTSConfig, ElevenLabsTTS),
        ]:
            self._check(prov_cls(cfg_cls(api_key="test")).version_info())
