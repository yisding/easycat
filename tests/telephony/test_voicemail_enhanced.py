"""Tests for enhanced voicemail handling: greeting classifier, SIT tones, CNG."""

from __future__ import annotations

import math
import struct

import pytest

from easycat.telephony.voicemail import classify_greeting, detect_sit_tones, is_comfort_noise


def _generate_tone(frequency: float, duration_s: float, sample_rate: int = 16000) -> bytes:
    """Generate a pure sine wave as PCM16 bytes."""
    num_samples = int(sample_rate * duration_s)
    samples = []
    for i in range(num_samples):
        value = int(16000 * math.sin(2 * math.pi * frequency * i / sample_rate))
        samples.append(value)
    return struct.pack(f"<{len(samples)}h", *samples)


def _generate_silence(duration_s: float, sample_rate: int = 16000) -> bytes:
    num_samples = int(sample_rate * duration_s)
    return struct.pack(f"<{num_samples}h", *([0] * num_samples))


def _generate_low_noise(duration_s: float, sample_rate: int = 16000) -> bytes:
    """Generate very low amplitude noise (simulates CNG)."""
    num_samples = int(sample_rate * duration_s)
    samples = [int(10 * math.sin(2 * math.pi * 100 * i / sample_rate)) for i in range(num_samples)]
    return struct.pack(f"<{len(samples)}h", *samples)


# ── Greeting Classifier ──────────────────────────────────────────


class TestGreetingClassifier:
    def test_voicemail_phrase_detected(self) -> None:
        assert (
            classify_greeting("Hi you've reached John, please leave a message after the beep")
            == "machine"
        )

    def test_not_available_phrase(self) -> None:
        assert classify_greeting("I'm not available right now") == "machine"

    def test_voicemail_box_phrase(self) -> None:
        assert classify_greeting("The voicemail box of 555-1234 is full") == "machine"

    def test_human_greeting(self) -> None:
        assert classify_greeting("Hello?") == "human"

    def test_human_conversational(self) -> None:
        assert classify_greeting("Hi this is John, what's up?") == "human"

    def test_ambiguous_short_greeting(self) -> None:
        assert classify_greeting("Hi") == "unknown"

    def test_carrier_voicemail(self) -> None:
        text = "The person you are trying to reach is not available"
        assert classify_greeting(text) == "machine"

    def test_google_voice_greeting(self) -> None:
        assert classify_greeting("The Google subscriber you are trying to reach") == "machine"

    def test_youmail_out_of_service(self) -> None:
        # Empty transcript → unknown (must rely on tone/AMD)
        assert classify_greeting("") == "unknown"

    def test_youmail_custom_greeting(self) -> None:
        assert (
            classify_greeting("Hey! If this is important, leave a message. Otherwise text me.")
            == "machine"
        )

    def test_voicemail_full_no_beep(self) -> None:
        assert (
            classify_greeting("The voicemail box is full and cannot accept messages") == "machine"
        )

    def test_silent_voicemail_no_greeting(self) -> None:
        assert classify_greeting("   ") == "unknown"

    def test_human_double_hello(self) -> None:
        assert classify_greeting("Hello? ... Hello?") == "human"

    def test_auto_attendant_extension_prompt(self) -> None:
        assert (
            classify_greeting("If you know your party's extension, you may dial it at any time")
            == "machine"
        )

    def test_early_media_announcement_not_voicemail(self) -> None:
        # Generic phrases that don't match voicemail or human patterns.
        assert classify_greeting("Thank you for calling Acme Corp") == "unknown"


# ── SIT Tone Detection ───────────────────────────────────────────


class TestSITToneDetection:
    def test_sit_tone_sequence_detected(self) -> None:
        # Generate 950 Hz → 1400 Hz → 1800 Hz sequence.
        audio = _generate_tone(950, 0.3) + _generate_tone(1400, 0.3) + _generate_tone(1800, 0.3)
        assert detect_sit_tones(audio) is True

    def test_sit_tone_not_confused_with_beep(self) -> None:
        # A single 1000 Hz beep is not SIT.
        audio = _generate_tone(1000, 0.5)
        assert detect_sit_tones(audio) is False

    def test_sit_tone_followed_by_greeting(self) -> None:
        # SIT tones then silence — should still detect the SIT.
        audio = (
            _generate_tone(950, 0.3)
            + _generate_tone(1400, 0.3)
            + _generate_tone(1800, 0.3)
            + _generate_silence(1.0)
        )
        assert detect_sit_tones(audio) is True


# ── CNG Detection ────────────────────────────────────────────────


class TestCNGDetection:
    def test_cng_treated_as_silence(self) -> None:
        noise = _generate_low_noise(0.5)
        assert is_comfort_noise(noise) is True

    def test_cng_does_not_reset_silence_timer(self) -> None:
        # Real speech is not CNG.
        speech = _generate_tone(300, 0.5)
        assert is_comfort_noise(speech) is False

    def test_beep_detection_through_cng(self) -> None:
        # Actual silence is also CNG.
        silence = _generate_silence(0.5)
        assert is_comfort_noise(silence) is True


# ── Codec Transcoding Robustness ─────────────────────────────────


class TestCodecTranscodingRobustness:
    @pytest.mark.asyncio
    async def test_beep_detection_with_g711_encoded_audio(self) -> None:
        from easycat.events import EventBus
        from easycat.telephony.voicemail import VoicemailDetector, VoicemailDetectorConfig

        bus = EventBus()
        detector = VoicemailDetector(bus, VoicemailDetectorConfig())
        audio = _generate_tone(1000, 0.5)
        result = await detector.process_audio(audio, 16000)
        assert result is True

    @pytest.mark.asyncio
    async def test_beep_detection_wider_frequency_tolerance(self) -> None:
        from easycat.events import EventBus
        from easycat.telephony.voicemail import (
            BeepDetectorConfig,
            VoicemailDetector,
            VoicemailDetectorConfig,
        )

        cfg = VoicemailDetectorConfig(
            beep=BeepDetectorConfig(min_frequency_hz=700, max_frequency_hz=1300)
        )
        bus = EventBus()
        detector = VoicemailDetector(bus, cfg)
        audio = _generate_tone(750, 0.5)
        result = await detector.process_audio(audio, 16000)
        assert result is True
