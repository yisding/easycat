"""Compare raw TTS config defaults with transport-resolved EasyConfig output.

Run from the repository root:

    uv run python docs/teaching/00-hello-audio/tts_alignment_probe.py

The probe constructs configuration objects with placeholder keys. It opens
no device or network connection and sends no credential or provider request.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from easycat import EasyConfig
from easycat.audio_format import PCM16_MONO_16K
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTTConfig
from easycat.transports.local import LocalTransportConfig
from easycat.transports.twilio_media import TwilioTransportConfig
from easycat.transports.webrtc import WebRTCTransportConfig
from easycat.transports.websocket import WebSocketTransportConfig
from easycat.tts.cartesia_tts import CartesiaTTSConfig
from easycat.tts.deepgram_tts import DeepgramTTSConfig
from easycat.tts.elevenlabs_tts import ElevenLabsTTSConfig
from easycat.tts.openai_tts import _OPENAI_PCM_FORMAT, OpenAITTSConfig

# OpenAI's raw PCM rate is a fixed provider boundary, not a configurable
# request field. This repo-local drift probe reads the runtime constant;
# application code should not import underscore-prefixed names.
TTS_FACTORIES: dict[str, Callable[[], object]] = {
    "cartesia": lambda: CartesiaTTSConfig(api_key="provider-free-probe"),
    "deepgram": lambda: DeepgramTTSConfig(api_key="provider-free-probe"),
    "elevenlabs": lambda: ElevenLabsTTSConfig(api_key="provider-free-probe"),
    "openai": lambda: OpenAITTSConfig(api_key="provider-free-probe"),
}
TRANSPORT_FACTORIES: dict[str, Callable[[], object]] = {
    "local": LocalTransportConfig,
    "webrtc": WebRTCTransportConfig,
    "websocket": WebSocketTransportConfig,
    "twilio": TwilioTransportConfig,
}


def _transport_output_rate_hz(tts: object) -> int:
    if isinstance(tts, ElevenLabsTTSConfig):
        return tts.audio_format.sample_rate
    if isinstance(tts, (OpenAITTSConfig, DeepgramTTSConfig, CartesiaTTSConfig)):
        return tts.output_format.sample_rate
    raise TypeError(f"Unsupported TTS config: {type(tts).__name__}")


def _provider_request_rate_hz(tts: object) -> int:
    if isinstance(tts, ElevenLabsTTSConfig):
        return int(tts.output_format.removeprefix("pcm_"))
    if isinstance(tts, (DeepgramTTSConfig, CartesiaTTSConfig)):
        return tts.sample_rate
    if isinstance(tts, OpenAITTSConfig):
        return _OPENAI_PCM_FORMAT.sample_rate
    raise TypeError(f"Unsupported TTS config: {type(tts).__name__}")


def _rates(tts: object) -> dict[str, int]:
    return {
        "provider_request_rate_hz": _provider_request_rate_hz(tts),
        "transport_output_rate_hz": _transport_output_rate_hz(tts),
    }


def _resolve(tts: object, transport: object, *, auto_align: bool = True) -> object:
    config = EasyConfig(
        stt=OpenAIRealtimeSTTConfig(api_key="provider-free-probe"),
        tts=tts,
        transport=transport,
        auto_align_tts_output_to_transport=auto_align,
    )
    return config.tts


def probe() -> dict[str, object]:
    raw_defaults = {name: _rates(factory()) for name, factory in TTS_FACTORIES.items()}
    resolved = {
        transport_name: {
            provider_name: _rates(_resolve(tts_factory(), transport_factory()))
            for provider_name, tts_factory in TTS_FACTORIES.items()
        }
        for transport_name, transport_factory in TRANSPORT_FACTORIES.items()
    }

    explicit_openai = OpenAITTSConfig(
        api_key="provider-free-probe",
        output_format=PCM16_MONO_16K,
    )
    alignment_disabled = OpenAITTSConfig(api_key="provider-free-probe")

    return {
        "controls": {
            "twilio_auto_align_disabled": _rates(
                _resolve(alignment_disabled, TwilioTransportConfig(), auto_align=False)
            ),
            "twilio_explicit_16k_preserved": _rates(
                _resolve(explicit_openai, TwilioTransportConfig())
            ),
        },
        "raw_defaults": raw_defaults,
        "resolved": resolved,
    }


if __name__ == "__main__":
    print(json.dumps(probe(), indent=2, sort_keys=True))
