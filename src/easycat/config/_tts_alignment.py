"""TTS-output-format-to-transport alignment.

Extracted from :mod:`easycat.config.easy` so the lightweight config
dataclasses stay scannable. :meth:`EasyConfig.__post_init__` calls
:func:`align_tts_config_to_transport` to re-target a default TTS config to
whatever PCM rate the transport prefers — e.g. a Twilio 8kHz mulaw stream
shouldn't get the 24kHz studio default.
"""

from __future__ import annotations

from dataclasses import replace

from easycat.audio_format import PCM16_MONO_24K, AudioFormat
from easycat.tts.cartesia_tts import CartesiaTTSConfig
from easycat.tts.deepgram_tts import DeepgramTTSConfig
from easycat.tts.elevenlabs_tts import ElevenLabsTTSConfig
from easycat.tts.factory import TTSConfig
from easycat.tts.openai_tts import OpenAITTSConfig

# The ElevenLabs PCM output endpoint only accepts a fixed set of rates, so we
# snap the transport's preferred rate to the closest supported one.
_ELEVENLABS_PCM_OUTPUT_RATES = (16000, 22050, 24000, 44100)


def _transport_tts_output_format(transport: object) -> AudioFormat | None:
    preferred = getattr(transport, "preferred_tts_output_format", None)
    if isinstance(preferred, AudioFormat):
        return preferred

    transport_fmt = getattr(transport, "audio_format", None)
    if isinstance(transport_fmt, AudioFormat):
        return transport_fmt
    return None


def _closest_elevenlabs_output_format(rate: int) -> str:
    closest = min(_ELEVENLABS_PCM_OUTPUT_RATES, key=lambda candidate: abs(candidate - rate))
    return f"pcm_{closest}"


def _align_pcm_rate_tts_config_to_transport(
    tts_config: DeepgramTTSConfig | CartesiaTTSConfig,
    target_format: AudioFormat,
) -> TTSConfig:
    """Align a PCM-rate TTS config (Deepgram/Cartesia) to the transport.

    Shared by the two providers that expose a ``sample_rate`` field and a
    PCM ``output_format``: only re-target when the config still holds the
    24kHz defaults, then clamp the provider sample rate to a supported
    16k/24k value before adopting the transport's output format.
    """
    if tts_config.sample_rate != 24000 or tts_config.output_format != PCM16_MONO_24K:
        return tts_config
    provider_rate = (
        target_format.sample_rate if target_format.sample_rate in (16000, 24000) else 24000
    )
    return replace(
        tts_config,
        sample_rate=provider_rate,
        output_format=target_format,
    )


def align_tts_config_to_transport(
    tts_config: TTSConfig,
    transport: object,
) -> TTSConfig:
    """Re-target a default TTS config to the transport's preferred PCM rate."""
    target_format = _transport_tts_output_format(transport)
    if target_format is None:
        return tts_config

    if isinstance(tts_config, OpenAITTSConfig):
        if tts_config.output_format != PCM16_MONO_24K:
            return tts_config
        return replace(tts_config, output_format=target_format)

    # Deepgram and Cartesia share an identical "clamp the provider sample_rate
    # to 16k/24k, then adopt the transport's PCM output_format" rule, so the
    # logic lives in one shared helper rather than two copy-paste branches that
    # could drift apart. Both configs expose ``sample_rate``/``output_format``.
    if isinstance(tts_config, (DeepgramTTSConfig, CartesiaTTSConfig)):
        return _align_pcm_rate_tts_config_to_transport(tts_config, target_format)

    if isinstance(tts_config, ElevenLabsTTSConfig):
        if tts_config.output_format != "pcm_24000" or tts_config.audio_format != PCM16_MONO_24K:
            return tts_config
        return replace(
            tts_config,
            output_format=_closest_elevenlabs_output_format(target_format.sample_rate),
            audio_format=target_format,
        )

    return tts_config
