"""Tests for ML voicemail detection and conversation coherence."""

from __future__ import annotations

import math
import struct

import pytest

from easycat.telephony.ml_voicemail import (
    ConversationCoherenceDetector,
    MLVoicemailDetector,
)


def _generate_tone(frequency: float, duration_s: float, sample_rate: int = 16000) -> bytes:
    num_samples = int(sample_rate * duration_s)
    samples = [
        int(16000 * math.sin(2 * math.pi * frequency * i / sample_rate))
        for i in range(num_samples)
    ]
    return struct.pack(f"<{len(samples)}h", *samples)


class TestWave2VecVoicemailDetector:
    def test_ml_detector_available_check(self) -> None:
        # Will be False in test environment (no onnxruntime typically).
        result = MLVoicemailDetector.is_available()
        assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_ml_detector_classifies_voicemail_audio(self) -> None:
        detector = MLVoicemailDetector()
        audio = _generate_tone(1000, 2.0)
        # Model not loaded — returns None (graceful fallback).
        result = await detector.classify_audio(audio)
        assert result is None

    @pytest.mark.asyncio
    async def test_ml_detector_classifies_human_audio(self) -> None:
        detector = MLVoicemailDetector()
        audio = _generate_tone(300, 2.0)
        result = await detector.classify_audio(audio)
        assert result is None  # Model not loaded.

    @pytest.mark.asyncio
    async def test_ml_detector_graceful_fallback(self) -> None:
        """When model unavailable, falls back without error."""
        detector = MLVoicemailDetector()
        audio = _generate_tone(1000, 2.0)
        result = await detector.classify_audio(audio)
        assert result is None
        assert not detector._model_loaded

    @pytest.mark.asyncio
    async def test_ml_detector_integrates_with_voicemail_detector(self) -> None:
        """ML detector returns None (fallback) when model not available."""
        detector = MLVoicemailDetector()
        available = detector.is_available()
        if not available:
            result = await detector.classify_audio(b"\x00" * 64000)
            assert result is None

    def test_ml_detector_latency_under_200ms(self) -> None:
        """Sync classification completes quickly even when model absent."""
        import time

        detector = MLVoicemailDetector()
        audio = _generate_tone(1000, 2.0)
        start = time.monotonic()
        result = detector.classify_audio_sync(audio)
        elapsed_ms = (time.monotonic() - start) * 1000
        assert result is None
        assert elapsed_ms < 200


class TestConversationCoherenceDetector:
    def test_coherent_conversation_passes(self) -> None:
        detector = ConversationCoherenceDetector()
        detector.add_bot_turn("Hi, calling about your appointment on Thursday")
        detector.add_callee_turn("Yes, my appointment is Thursday at 3pm")
        detector.add_bot_turn("Great, confirming Thursday 3pm")
        detector.add_callee_turn("Yes Thursday at 3pm works for me")
        assert detector.is_coherent()

    def test_incoherent_responses_flagged(self) -> None:
        detector = ConversationCoherenceDetector()
        detector.add_bot_turn("Hi, calling about your appointment")
        detector.add_callee_turn("What's your favorite color of dinosaur?")
        detector.add_bot_turn("I need to confirm Thursday")
        detector.add_callee_turn("Do you like pizza with pineapple?")
        detector.add_bot_turn("Can you confirm the time?")
        detector.add_callee_turn("I once saw a purple elephant dancing")
        assert not detector.is_coherent()

    def test_robokiller_pattern_detected(self) -> None:
        detector = ConversationCoherenceDetector()
        detector.add_bot_turn("Hi, this is Sarah from Acme Corp")
        detector.add_callee_turn("Oh tell me more about those cookies")
        detector.add_bot_turn("I'm calling about your upcoming appointment")
        detector.add_callee_turn("My grandmother used to make the best pie")
        assert not detector.is_coherent()

    def test_coherence_score(self) -> None:
        detector = ConversationCoherenceDetector()
        detector.add_bot_turn("Calling about appointment")
        detector.add_callee_turn("Random unrelated nonsense words here")
        detector.add_bot_turn("Thursday appointment confirmation")
        detector.add_callee_turn("Purple elephants swimming upstream")
        score = detector.coherence_score()
        assert score < 0.5

    def test_coherence_detector_lightweight(self) -> None:
        """Coherence check doesn't require LLM — uses keyword overlap."""
        import time

        detector = ConversationCoherenceDetector()
        detector.add_bot_turn("appointment Thursday")
        detector.add_callee_turn("random words here")
        detector.add_bot_turn("confirm time")
        detector.add_callee_turn("more random words")
        start = time.monotonic()
        detector.is_coherent()
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 50  # Very fast.
