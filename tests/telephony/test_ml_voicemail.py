"""Tests for conversation coherence detection."""

from __future__ import annotations

from easycat.telephony.ml_voicemail import ConversationCoherenceDetector


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
