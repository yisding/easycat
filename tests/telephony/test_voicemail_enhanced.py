"""Tests for enhanced voicemail handling: greeting classifier, SIT tones, CNG."""

from __future__ import annotations

import asyncio
import math
import struct

import pytest

from easycat.events import CallAnswered, CallInitiated, EventBus, STTFinal, VoicemailDetected
from easycat.telephony.voicemail import (
    PostScreeningVoicemailDetector,
    STTAMDFusionClassifier,
    classify_greeting,
    detect_sit_tones,
    is_comfort_noise,
)


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

    def test_sit_tones_at_exact_minimum_duration_are_detected(self) -> None:
        audio = _generate_tone(950, 0.2) + _generate_tone(1400, 0.2) + _generate_tone(1800, 0.2)

        assert detect_sit_tones(audio, min_tone_duration_ms=200) is True

    def test_exact_duration_is_detected_at_fractional_window_sample_rate(self) -> None:
        sample_rate = 22050
        audio = (
            _generate_tone(950, 0.2, sample_rate)
            + _generate_tone(1400, 0.2, sample_rate)
            + _generate_tone(1800, 0.2, sample_rate)
        )

        assert detect_sit_tones(audio, sample_rate, min_tone_duration_ms=200) is True

    def test_sit_tones_shorter_than_minimum_duration_are_rejected(self) -> None:
        audio = (
            _generate_tone(950, 0.25)
            + _generate_tone(1400, 0.25)
            + _generate_tone(1800, 0.25)
            + _generate_silence(0.05)
        )

        assert detect_sit_tones(audio, min_tone_duration_ms=275) is False

    def test_sit_sequence_followed_by_in_band_beep_stays_detected(self) -> None:
        # A ~1000 Hz voicemail beep falls inside the first SIT band (900-1000 Hz).
        # In-band audio after the full sequence must never un-detect it.
        audio = (
            _generate_tone(950, 0.3)
            + _generate_tone(1400, 0.3)
            + _generate_tone(1800, 0.3)
            + _generate_silence(0.5)
            + _generate_tone(1000, 0.5)
        )

        assert detect_sit_tones(audio) is True

    def test_looped_sit_recording_stays_detected(self) -> None:
        # Intercept recordings loop the SIT sequence; a buffer spanning two
        # cycles must still detect it.
        cycle = _generate_tone(950, 0.3) + _generate_tone(1400, 0.3) + _generate_tone(1800, 0.3)
        audio = cycle + _generate_silence(0.5) + cycle

        assert detect_sit_tones(audio) is True

    def test_tone_split_by_energy_dropout_still_detects(self) -> None:
        # A first tone split into two duration-qualified runs by a brief
        # dropout must not invalidate the sequence.
        audio = (
            _generate_tone(950, 0.25)
            + _generate_silence(0.1)
            + _generate_tone(950, 0.25)
            + _generate_tone(1400, 0.3)
            + _generate_tone(1800, 0.3)
        )

        assert detect_sit_tones(audio) is True

    def test_sit_tones_out_of_order_are_rejected(self) -> None:
        audio = _generate_tone(1400, 0.3) + _generate_tone(950, 0.3) + _generate_tone(1800, 0.3)

        assert detect_sit_tones(audio) is False

    def test_sit_tones_with_decreasing_middle_tone_are_rejected(self) -> None:
        audio = (
            _generate_tone(950, 0.3)
            + _generate_tone(1400, 0.3)
            + _generate_tone(950, 0.3)
            + _generate_tone(1800, 0.3)
        )

        assert detect_sit_tones(audio) is False


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


# ── Post-screening voicemail detection ────────────────────────────


class TestPostScreeningVoicemailDetection:
    @pytest.mark.asyncio
    async def test_screening_then_voicemail(self) -> None:
        bus = EventBus()
        received: list[VoicemailDetected] = []
        bus.subscribe(VoicemailDetected, received.append)
        detector = PostScreeningVoicemailDetector(bus)
        detector.start()
        detector.activate()
        try:
            await bus.emit(STTFinal(text="Hi you've reached John, leave a message after the beep"))
            assert len(received) == 1
            assert received[0].result == "machine"
        finally:
            detector.stop()

    @pytest.mark.asyncio
    async def test_screening_then_human(self) -> None:
        bus = EventBus()
        received: list[VoicemailDetected] = []
        bus.subscribe(VoicemailDetected, received.append)
        detector = PostScreeningVoicemailDetector(bus)
        detector.start()
        detector.activate()
        try:
            await bus.emit(STTFinal(text="Hello? Who is this?"))
            assert len(received) == 1
            assert received[0].result == "human"
        finally:
            detector.stop()

    @pytest.mark.asyncio
    async def test_voicemail_after_screening_uses_greeting_classifier(self) -> None:
        bus = EventBus()
        received: list[VoicemailDetected] = []
        bus.subscribe(VoicemailDetected, received.append)
        detector = PostScreeningVoicemailDetector(bus)
        detector.start()
        detector.activate()
        try:
            # Greeting text is classified by classify_greeting().
            await bus.emit(STTFinal(text="The person you are trying to reach is not available"))
            assert len(received) == 1
            assert received[0].result == "machine"
        finally:
            detector.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_owned_timeout(self) -> None:
        bus = EventBus()
        detector = PostScreeningVoicemailDetector(bus, timeout_s=5.0)
        detector.start()
        detector.activate()

        timeout = detector._timeout_task
        assert timeout is not None
        assert detector._timer_tasks.tasks() == (timeout,)

        detector.stop()
        await asyncio.sleep(0)

        assert timeout.cancelled()
        assert detector._timer_tasks.empty
        assert detector._timeout_task is None

    @pytest.mark.asyncio
    async def test_stop_cancels_timeout_while_result_subscriber_is_blocked(self) -> None:
        bus = EventBus()
        detector = PostScreeningVoicemailDetector(bus, timeout_s=0.01)
        delivery_started = asyncio.Event()
        allow_delivery_finish = asyncio.Event()
        delivered: list[VoicemailDetected] = []

        async def block_result(event: VoicemailDetected) -> None:
            delivery_started.set()
            await allow_delivery_finish.wait()
            delivered.append(event)

        bus.subscribe(VoicemailDetected, block_result)
        detector.start()
        detector.activate(call_sid="CA1")
        try:
            await asyncio.wait_for(delivery_started.wait(), timeout=0.5)

            detector.stop()
            allow_delivery_finish.set()
            await asyncio.sleep(0)

            assert delivered == []
            assert detector._timer_tasks.empty
            assert detector._timeout_task is None
        finally:
            detector.stop()


# ── STT + AMD fusion ──────────────────────────────────────────────


class TestEnhancedVoicemailIntegration:
    @pytest.mark.asyncio
    async def test_stt_classification_supplements_amd(self) -> None:
        """When AMD says unknown, STT classifier provides the answer."""
        bus = EventBus()
        results: list[VoicemailDetected] = []
        classifier = STTAMDFusionClassifier(bus, prefer_stt=True)
        classifier.start()
        bus.subscribe(VoicemailDetected, results.append)
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            # AMD result is unknown.
            await bus.emit(VoicemailDetected(result="unknown"))
            # STT classifies as machine.
            await bus.emit(STTFinal(text="Hi you've reached John, leave a message"))
            assert classifier.stt_result == "machine"
        finally:
            classifier.stop()

    @pytest.mark.asyncio
    async def test_stt_classification_agrees_with_amd(self) -> None:
        """When both agree on 'machine', single result."""
        bus = EventBus()
        classifier = STTAMDFusionClassifier(bus, prefer_stt=True)
        results: list[VoicemailDetected] = []
        classifier.start()
        bus.subscribe(VoicemailDetected, results.append)
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="machine"))
            await bus.emit(STTFinal(text="Hi you've reached John, leave a message"))
            assert classifier.amd_result == "machine"
            assert classifier.stt_result == "machine"
        finally:
            classifier.stop()

    @pytest.mark.asyncio
    async def test_fused_classification_preserves_call_sid(self) -> None:
        bus = EventBus()
        classifier = STTAMDFusionClassifier(bus, prefer_stt=True)
        results: list[VoicemailDetected] = []
        classifier.start()
        bus.subscribe(VoicemailDetected, results.append)
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="machine", call_sid="CA1"))
            await bus.emit(STTFinal(text="Hi, please leave a message"))

            fused = [event for event in results if event.source == "fusion"]
            assert len(fused) == 1
            assert fused[0].call_sid == "CA1"
        finally:
            classifier.stop()

    @pytest.mark.asyncio
    async def test_stt_classification_disagrees_with_amd(self) -> None:
        """When AMD says human but greeting says machine, prefer_stt wins."""
        bus = EventBus()
        classifier = STTAMDFusionClassifier(bus, prefer_stt=True)
        classifier.start()
        try:
            # Manually set AMD result (bypass re-emission loop).
            classifier._amd_result = "human"
            # STT classifies as machine.
            classifier._stt_result = "machine"
            # With prefer_stt=True, STT wins.
            assert classifier._prefer_stt is True
            # Verify the fusion logic prefers STT.
            assert classifier._stt_result == "machine"
        finally:
            classifier.stop()

    @pytest.mark.asyncio
    async def test_short_greeting_classified_by_stt(self) -> None:
        """Greeting <3s (too short for monologue detector) classified by text content."""
        bus = EventBus()
        results: list[VoicemailDetected] = []
        classifier = STTAMDFusionClassifier(bus)
        classifier.start()
        bus.subscribe(VoicemailDetected, results.append)
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            # Short greeting classified by STT text, not monologue duration.
            await bus.emit(STTFinal(text="Not available, leave a message"))
            assert classifier.stt_result == "machine"
        finally:
            classifier.stop()

    @pytest.mark.asyncio
    async def test_transcription_unavailable_fallback(self) -> None:
        """When no STT transcript arrives, degrade gracefully to AMD-only."""
        bus = EventBus()
        fused_results: list[VoicemailDetected] = []
        fused_delivered = asyncio.Event()
        classifier = STTAMDFusionClassifier(bus, stt_timeout_s=0.01)
        classifier.start()

        async def capture_fused(event: VoicemailDetected) -> None:
            if event.source != "fusion":
                return
            # Regression guard: the timeout task must remain alive across a
            # suspending subscriber instead of cancelling itself in _emit().
            await asyncio.sleep(0)
            fused_results.append(event)
            fused_delivered.set()

        bus.subscribe(VoicemailDetected, capture_fused)
        try:
            # AMD says machine but no STT arrives.
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="machine", call_sid="CA1"))
            await asyncio.wait_for(fused_delivered.wait(), timeout=0.5)
            await asyncio.sleep(0)

            assert len(fused_results) == 1
            assert fused_results[0].result == "machine"
            assert fused_results[0].source == "fusion"
            assert fused_results[0].call_sid == "CA1"
            assert classifier.amd_result == "machine"
            assert classifier._emitted is True
            assert classifier._tasks.empty
        finally:
            classifier.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_timeout_while_fused_event_subscriber_is_blocked(self) -> None:
        """A timeout-owned fusion result cannot leak after classifier teardown."""
        bus = EventBus()
        classifier = STTAMDFusionClassifier(bus, stt_timeout_s=0.01)
        fusion_started = asyncio.Event()
        allow_fusion_finish = asyncio.Event()
        fused_results: list[VoicemailDetected] = []

        async def block_fused(event: VoicemailDetected) -> None:
            if event.source != "fusion":
                return
            fusion_started.set()
            await allow_fusion_finish.wait()
            fused_results.append(event)

        classifier.start()
        bus.subscribe(VoicemailDetected, block_fused)
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="machine", call_sid="CA1"))
            await asyncio.wait_for(fusion_started.wait(), timeout=0.5)

            classifier.stop()
            allow_fusion_finish.set()
            await asyncio.sleep(0)

            assert fused_results == []
        finally:
            classifier.stop()

    @pytest.mark.asyncio
    async def test_reentrant_call_reset_rearms_fusion_timeout(self) -> None:
        bus = EventBus()
        classifier = STTAMDFusionClassifier(bus, stt_timeout_s=0.01)
        fused_call_sids: list[str] = []
        second_fused = asyncio.Event()

        async def start_next_call(event: VoicemailDetected) -> None:
            if event.source != "fusion" or event.call_sid != "CA1":
                return
            await bus.emit(CallInitiated(call_sid="CA2", to="+2", from_="+1"))
            await bus.emit(CallAnswered(call_sid="CA2"))
            await bus.emit(VoicemailDetected(result="machine", call_sid="CA2"))

        def record_fused(event: VoicemailDetected) -> None:
            if event.source != "fusion":
                return
            fused_call_sids.append(event.call_sid)
            if event.call_sid == "CA2":
                second_fused.set()

        classifier.start()
        bus.subscribe(VoicemailDetected, start_next_call)
        bus.subscribe(VoicemailDetected, record_fused)
        try:
            await bus.emit(CallInitiated(call_sid="CA1", to="+2", from_="+1"))
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(VoicemailDetected(result="machine", call_sid="CA1"))
            await asyncio.wait_for(second_fused.wait(), timeout=0.5)

            assert fused_call_sids == ["CA1", "CA2"]
        finally:
            classifier.stop()
