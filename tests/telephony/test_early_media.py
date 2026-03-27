"""Tests for early media detection."""

from __future__ import annotations

from easycat.telephony.ml_voicemail import EarlyMediaDetector


class TestEarlyMediaDetector:
    def test_early_media_phase_detected(self) -> None:
        detector = EarlyMediaDetector()
        assert detector.in_early_media

    def test_early_media_announcements_ignored(self) -> None:
        detector = EarlyMediaDetector()
        assert detector.is_early_media_text("This call may be monitored for quality")
        assert not detector.is_early_media_text("Hello, how can I help you?")

    def test_early_media_phase_ends_on_answer(self) -> None:
        detector = EarlyMediaDetector()
        assert detector.in_early_media
        detector.on_call_answered()
        assert not detector.in_early_media

    def test_early_media_ring_back_tone_not_classified(self) -> None:
        detector = EarlyMediaDetector()
        # During early media, text shouldn't trigger classification.
        detector.record_early_text("Please hold while we connect your call")
        assert len(detector._early_media_texts) == 1
        # After answer, early media phase is over.
        detector.on_call_answered()
        assert not detector.in_early_media
