"""Print EasyCat's current default audio-rate boundaries without opening devices.

Run from the repository root:

    uv run python docs/teaching/00-hello-audio/format_boundaries.py

The probe constructs configuration objects only. It makes no provider
requests and opens no microphone, speaker, socket, or server.
"""

from __future__ import annotations

import json

from easycat.audio_format import AudioFormat
from easycat.stt.cartesia_provider import CartesiaSTTConfig
from easycat.stt.deepgram_provider import DeepgramSTTConfig
from easycat.stt.elevenlabs_provider import ElevenLabsSTTConfig

# These two rates are fixed protocol boundaries rather than configurable
# fields. This repo-local drift probe intentionally reads their runtime
# constants; application code should not import underscore-prefixed names.
from easycat.stt.openai_realtime_provider import _REALTIME_SAMPLE_RATE
from easycat.transports.local import LocalTransportConfig
from easycat.transports.twilio_media import MULAW_8K, TwilioTransportConfig
from easycat.transports.webrtc import _WEBRTC_SAMPLE_RATE, WebRTCTransportConfig
from easycat.transports.websocket import WebSocketTransportConfig
from easycat.tts.cartesia_tts import CartesiaTTSConfig
from easycat.tts.deepgram_tts import DeepgramTTSConfig
from easycat.tts.elevenlabs_tts import ElevenLabsTTSConfig
from easycat.tts.openai_tts import OpenAITTSConfig


def _row(name: str, role: str, sample_rate_hz: int, encoding: str = "pcm") -> dict[str, object]:
    return {
        "encoding": encoding,
        "name": name,
        "role": role,
        "sample_rate_hz": sample_rate_hz,
    }


def _format_row(name: str, role: str, audio_format: AudioFormat) -> dict[str, object]:
    return _row(name, role, audio_format.sample_rate, audio_format.encoding)


def catalog() -> list[dict[str, object]]:
    """Return one row per current boundary, ordered by sample rate."""
    return [
        _format_row("twilio_wire", "wire", MULAW_8K),
        _row("deepgram_stt_target", "provider_input", DeepgramSTTConfig().sample_rate),
        _row("cartesia_stt_target", "provider_input", CartesiaSTTConfig().sample_rate),
        _row(
            "elevenlabs_realtime_stt_target",
            "provider_input",
            ElevenLabsSTTConfig().realtime_sample_rate,
        ),
        _format_row(
            "websocket_pipeline_target",
            "pipeline",
            WebSocketTransportConfig().audio_format,
        ),
        _format_row(
            "webrtc_pipeline_target",
            "pipeline",
            WebRTCTransportConfig().audio_format,
        ),
        _format_row(
            "twilio_pipeline_target",
            "pipeline",
            TwilioTransportConfig().audio_format,
        ),
        _row("openai_realtime_stt_input", "provider_input", _REALTIME_SAMPLE_RATE),
        _format_row("local_pipeline", "pipeline", LocalTransportConfig().audio_format),
        _format_row(
            "openai_tts_config_default",
            "provider_config_default",
            OpenAITTSConfig().output_format,
        ),
        _format_row(
            "deepgram_tts_config_default",
            "provider_config_default",
            DeepgramTTSConfig().output_format,
        ),
        _format_row(
            "cartesia_tts_config_default",
            "provider_config_default",
            CartesiaTTSConfig().output_format,
        ),
        _format_row(
            "elevenlabs_tts_config_default",
            "provider_config_default",
            ElevenLabsTTSConfig().audio_format,
        ),
        _row("webrtc_media_frames", "media", _WEBRTC_SAMPLE_RATE),
    ]


if __name__ == "__main__":
    print(json.dumps(catalog(), indent=2, sort_keys=True))
