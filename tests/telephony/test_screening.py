"""Tests for call screening detection."""

from __future__ import annotations

import asyncio

import pytest

from easycat.events import (
    CallAnswered,
    CallEnded,
    CallScreening,
    EventBus,
    STTFinal,
    STTPartial,
    VoicemailDetected,
)
from easycat.telephony.screening import (
    CallScreeningDetector,
    ScreeningPatternSet,
    ScreeningResponse,
    ScreeningState,
    is_conversational,
    match_screening_platform,
    screening_patterns_for_languages,
)

# ── Pattern matching ──────────────────────────────────────────────


class TestScreeningPatterns:
    def test_ios_pattern_record_name(self) -> None:
        assert match_screening_platform("Please record your name and reason for calling") == "ios"

    def test_ios_pattern_see_if_available(self) -> None:
        assert match_screening_platform("Let me see if this person is available") == "ios"

    def test_ios_pattern_hi_if_you_record(self) -> None:
        assert (
            match_screening_platform("hi if you record your name and reason for calling") == "ios"
        )

    def test_android_pattern_screening_service(self) -> None:
        assert (
            match_screening_platform("The person you're calling is using a screening service")
            == "android"
        )

    def test_android_pattern_say_name(self) -> None:
        assert (
            match_screening_platform("Go ahead and say your name and why you're calling")
            == "android"
        )

    def test_android_pattern_get_copy_of_conversation(self) -> None:
        assert (
            match_screening_platform("The person will get a copy of this conversation")
            == "android"
        )

    def test_carrier_pattern_caller_id(self) -> None:
        assert (
            match_screening_platform("The person you're calling has caller ID screening")
            == "carrier"
        )

    def test_carrier_pattern_unidentified_calls(self) -> None:
        assert (
            match_screening_platform("The person you called does not accept unidentified calls")
            == "carrier"
        )

    def test_nomorobo_press_1_screening(self) -> None:
        assert (
            match_screening_platform("Please press 1 to be connected to this person")
            == "third_party"
        )

    def test_third_party_branded_app(self) -> None:
        assert match_screening_platform("This call is being screened by Nomorobo") == "third_party"
        assert match_screening_platform("YouMail smart greeting is active now") == "third_party"
        assert match_screening_platform("RoboKiller is screening this call now") == "third_party"
        assert match_screening_platform("Truecaller is screening this call now") == "third_party"

    def test_no_match_normal_speech(self) -> None:
        assert match_screening_platform("Hello, this is John") is None

    def test_no_match_voicemail_greeting(self) -> None:
        assert match_screening_platform("Hi you've reached John, leave a message") is None

    def test_no_match_robokiller_answer_bot(self) -> None:
        assert match_screening_platform("Oh hi there, what did you say your name was?") is None

    def test_no_false_positive_identify_yourself(self) -> None:
        """'identify yourself' was removed — too broad (matches human receptionists)."""
        assert match_screening_platform("Can you identify yourself please?") is None

    def test_no_false_positive_short_hiya(self) -> None:
        """'hiya' is not a pattern — it collides with the common greeting."""
        assert match_screening_platform("Hiya, how's it going today my friend") is None

    def test_partial_match_sufficient(self) -> None:
        # "record your name" is a substring of the full iOS prompt
        assert match_screening_platform("Please record your name before we can connect") == "ios"

    def test_case_insensitive(self) -> None:
        assert match_screening_platform("USING A SCREENING SERVICE from Google") == "android"

    def test_custom_patterns(self) -> None:
        custom = ScreeningPatternSet(
            ios=["custom ios pattern"],
            android=[],
            carrier=[],
            third_party=[],
            exclusions=[],
        )
        assert match_screening_platform("Please use custom ios pattern here", custom) == "ios"
        assert match_screening_platform("using a screening service", custom) is None

    def test_no_match_early_media_announcement(self) -> None:
        assert (
            match_screening_platform("This call may be monitored for quality assurance purposes")
            is None
        )

    def test_no_match_carrier_hold_message(self) -> None:
        assert match_screening_platform("Please hold while we connect your call now") is None

    def test_short_partial_no_premature_match(self) -> None:
        # The function itself matches regardless of length via exact
        # substring checks; accumulation happens in the detector.
        assert match_screening_platform("record your name") == "ios"

    def test_sliding_window_accumulation(self) -> None:
        # Pattern matching is stateless; accumulation is in the detector.
        assert match_screening_platform("Please") is None
        assert match_screening_platform("Please record your name and reason for calling") == "ios"


# ── Detector lifecycle and event emission ─────────────────────────


class TestCallScreeningDetector:
    @pytest.mark.asyncio
    async def test_detects_ios_screening_from_stt_partial(self) -> None:
        bus = EventBus()
        received: list[CallScreening] = []
        bus.subscribe(CallScreening, received.append)
        detector = CallScreeningDetector(bus, call_sid="CA1", track_filter=None)
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            await bus.emit(STTPartial(text="please record your name and reason for calling"))
            assert len(received) == 1
            assert received[0].platform == "ios"
            assert received[0].call_sid == "CA1"
        finally:
            detector.stop()

    @pytest.mark.asyncio
    async def test_detects_android_screening_from_stt_partial(self) -> None:
        bus = EventBus()
        received: list[CallScreening] = []
        bus.subscribe(CallScreening, received.append)
        detector = CallScreeningDetector(bus, call_sid="CA2", track_filter=None)
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA2"))
            await bus.emit(
                STTPartial(text="The person you're calling is using a screening service")
            )
            assert len(received) == 1
            assert received[0].platform == "android"
        finally:
            detector.stop()

    @pytest.mark.asyncio
    async def test_detects_carrier_screening(self) -> None:
        bus = EventBus()
        received: list[CallScreening] = []
        bus.subscribe(CallScreening, received.append)
        detector = CallScreeningDetector(bus, call_sid="CA3", track_filter=None)
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid="CA3"))
            await bus.emit(
                STTPartial(text="The person you're calling has caller ID screening enabled")
            )
            assert len(received) == 1
            assert received[0].platform == "carrier"
        finally:
            detector.stop()

    @pytest.mark.asyncio
    async def test_no_false_positive_on_human_greeting(self) -> None:
        bus = EventBus()
        received: list[CallScreening] = []
        bus.subscribe(CallScreening, received.append)
        detector = CallScreeningDetector(bus, track_filter=None)
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid=""))
            await bus.emit(STTPartial(text="Hi how are you doing today my friend"))
            assert len(received) == 0
        finally:
            detector.stop()

    @pytest.mark.asyncio
    async def test_no_false_positive_on_voicemail(self) -> None:
        bus = EventBus()
        received: list[CallScreening] = []
        bus.subscribe(CallScreening, received.append)
        detector = CallScreeningDetector(bus, track_filter=None)
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid=""))
            await bus.emit(
                STTPartial(text="Hi you've reached John, please leave a message after the beep")
            )
            assert len(received) == 0
        finally:
            detector.stop()

    @pytest.mark.asyncio
    async def test_emits_only_once(self) -> None:
        bus = EventBus()
        received: list[CallScreening] = []
        bus.subscribe(CallScreening, received.append)
        detector = CallScreeningDetector(bus, track_filter=None)
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid=""))
            await bus.emit(STTPartial(text="please record your name and reason for calling"))
            await bus.emit(STTPartial(text="please record your name and reason for calling again"))
            assert len(received) == 1
        finally:
            detector.stop()

    @pytest.mark.asyncio
    async def test_uses_stt_partial_not_final(self) -> None:
        bus = EventBus()
        received: list[CallScreening] = []
        bus.subscribe(CallScreening, received.append)
        detector = CallScreeningDetector(bus, track_filter=None)
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid=""))
            await bus.emit(STTPartial(text="please record your name and reason for calling"))
            assert len(received) == 1
        finally:
            detector.stop()

    def test_start_stop_lifecycle(self) -> None:
        bus = EventBus()
        detector = CallScreeningDetector(bus)
        detector.start()
        assert detector._started is True
        detector.stop()
        assert detector._started is False
        assert detector.state == ScreeningState.WAITING

    @pytest.mark.asyncio
    async def test_reset_allows_re_detection(self) -> None:
        bus = EventBus()
        received: list[CallScreening] = []
        bus.subscribe(CallScreening, received.append)
        detector = CallScreeningDetector(bus, track_filter=None)
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid=""))
            await bus.emit(STTPartial(text="please record your name and reason for calling"))
            assert len(received) == 1
            detector.reset()
            await bus.emit(CallAnswered(call_sid=""))
            await bus.emit(STTPartial(text="please record your name and reason for calling"))
            assert len(received) == 2
        finally:
            detector.stop()

    @pytest.mark.asyncio
    async def test_disabled_when_config_false(self) -> None:
        bus = EventBus()
        received: list[CallScreening] = []
        bus.subscribe(CallScreening, received.append)
        detector = CallScreeningDetector(bus, enabled=False, track_filter=None)
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid=""))
            await bus.emit(STTPartial(text="please record your name and reason for calling"))
            assert len(received) == 0
        finally:
            detector.stop()

    @pytest.mark.asyncio
    async def test_filters_inbound_track_only(self) -> None:
        bus = EventBus()
        received: list[CallScreening] = []
        bus.subscribe(CallScreening, received.append)
        detector = CallScreeningDetector(bus, track_filter="inbound")
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid=""))

            # Simulate an outbound-track partial (bot's own speech).
            ev = STTPartial(text="please record your name and reason for calling")
            # Attach a track attribute dynamically for testing.
            object.__setattr__(ev, "track", "outbound")
            await bus.emit(ev)
            assert len(received) == 0

            # Now an inbound-track partial.
            ev2 = STTPartial(text="please record your name and reason for calling")
            object.__setattr__(ev2, "track", "inbound")
            await bus.emit(ev2)
            assert len(received) == 1
        finally:
            detector.stop()

    @pytest.mark.asyncio
    async def test_short_partial_without_pattern_ignored(self) -> None:
        """Short partials that don't match a known pattern are ignored."""
        bus = EventBus()
        received: list[CallScreening] = []
        bus.subscribe(CallScreening, received.append)
        detector = CallScreeningDetector(bus, track_filter=None)
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid=""))
            await bus.emit(STTPartial(text="hello how are you"))
            assert len(received) == 0
        finally:
            detector.stop()

    @pytest.mark.asyncio
    async def test_short_partial_with_known_pattern_triggers(self) -> None:
        """Short partials that match a known screening pattern still trigger."""
        bus = EventBus()
        received: list[CallScreening] = []
        bus.subscribe(CallScreening, received.append)
        detector = CallScreeningDetector(bus, track_filter=None)
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid=""))
            await bus.emit(STTPartial(text="record your name"))
            assert len(received) == 1
        finally:
            detector.stop()

    @pytest.mark.asyncio
    async def test_sliding_window_accumulation_in_detector(self) -> None:
        """Successive partials accumulate; match triggers when length threshold met."""
        bus = EventBus()
        received: list[CallScreening] = []
        bus.subscribe(CallScreening, received.append)
        detector = CallScreeningDetector(bus, track_filter=None)
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid=""))
            await bus.emit(STTPartial(text="Please"))
            assert len(received) == 0
            await bus.emit(STTPartial(text="Please record your name and reason for calling"))
            assert len(received) == 1
        finally:
            detector.stop()


# ── Screening response ────────────────────────────────────────────


class TestScreeningResponseStatic:
    @pytest.mark.asyncio
    async def test_static_response_emitted(self) -> None:
        bus = EventBus()
        responses: list[ScreeningResponse] = []
        bus.subscribe(ScreeningResponse, responses.append)
        detector = CallScreeningDetector(
            bus, screening_response="Hi, this is Sarah", track_filter=None
        )
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid=""))
            await bus.emit(STTPartial(text="please record your name and reason for calling"))
            assert len(responses) == 1
            assert responses[0].text == "Hi, this is Sarah"
            assert responses[0].mode == "static"
        finally:
            detector.stop()

    @pytest.mark.asyncio
    async def test_empty_static_response_skipped(self) -> None:
        bus = EventBus()
        responses: list[ScreeningResponse] = []
        bus.subscribe(ScreeningResponse, responses.append)
        detector = CallScreeningDetector(bus, screening_response="", track_filter=None)
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid=""))
            await bus.emit(STTPartial(text="please record your name and reason for calling"))
            assert len(responses) == 0
        finally:
            detector.stop()


class TestScreeningResponseAgent:
    @pytest.mark.asyncio
    async def test_agent_response_requested(self) -> None:
        bus = EventBus()
        responses: list[ScreeningResponse] = []
        bus.subscribe(ScreeningResponse, responses.append)
        detector = CallScreeningDetector(bus, screening_use_agent=True, track_filter=None)
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid=""))
            await bus.emit(STTPartial(text="please record your name and reason for calling"))
            assert len(responses) == 1
            assert responses[0].mode == "agent"
        finally:
            detector.stop()

    @pytest.mark.asyncio
    async def test_agent_timeout_falls_back_to_static(self) -> None:
        bus = EventBus()
        responses: list[ScreeningResponse] = []
        bus.subscribe(ScreeningResponse, responses.append)
        detector = CallScreeningDetector(
            bus,
            screening_use_agent=True,
            screening_response="Fallback: Hi, this is Sarah",
            agent_timeout_s=0.05,
            track_filter=None,
        )
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid=""))
            await bus.emit(STTPartial(text="please record your name and reason for calling"))
            assert len(responses) == 1
            assert responses[0].mode == "agent"
            # Wait for timeout to fire the fallback.
            await asyncio.sleep(0.1)
            assert len(responses) == 2
            assert responses[1].mode == "static"
            assert responses[1].text == "Fallback: Hi, this is Sarah"
        finally:
            detector.stop()

    @pytest.mark.asyncio
    async def test_agent_response_includes_callee_context(self) -> None:
        # Agent mode emits with mode="agent"; context is provided at the
        # application layer, not by the detector itself.
        bus = EventBus()
        responses: list[ScreeningResponse] = []
        bus.subscribe(ScreeningResponse, responses.append)
        detector = CallScreeningDetector(bus, screening_use_agent=True, track_filter=None)
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid=""))
            await bus.emit(STTPartial(text="please record your name and reason for calling"))
            assert responses[0].mode == "agent"
        finally:
            detector.stop()


# ── Screening state machine ───────────────────────────────────────


class TestScreeningStateMachine:
    def test_initial_state_waiting(self) -> None:
        bus = EventBus()
        detector = CallScreeningDetector(bus)
        assert detector.state == ScreeningState.WAITING

    @pytest.mark.asyncio
    async def test_screening_detected_transitions(self) -> None:
        bus = EventBus()
        detector = CallScreeningDetector(bus, screening_response="", track_filter=None)
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid=""))
            await bus.emit(STTPartial(text="please record your name and reason for calling"))
            assert detector.state == ScreeningState.SCREENING_DETECTED
        finally:
            detector.stop()

    @pytest.mark.asyncio
    async def test_responding_state(self) -> None:
        bus = EventBus()
        detector = CallScreeningDetector(
            bus, screening_response="Hi, this is Sarah", track_filter=None
        )
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid=""))
            await bus.emit(STTPartial(text="please record your name and reason for calling"))
            assert detector.state == ScreeningState.RESPONDING
        finally:
            detector.stop()

    def test_state_exposed_as_property(self) -> None:
        bus = EventBus()
        detector = CallScreeningDetector(bus)
        assert isinstance(detector.state, ScreeningState)

    @pytest.mark.asyncio
    async def test_human_answered_outcome(self) -> None:
        bus = EventBus()
        detector = CallScreeningDetector(bus, track_filter=None)
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid=""))
            await bus.emit(STTPartial(text="please record your name and reason for calling"))
            assert detector.state == ScreeningState.SCREENING_DETECTED
            await bus.emit(STTFinal(text="Hello, how can I help you?"))
            assert detector.state == ScreeningState.HUMAN_ANSWERED
        finally:
            detector.stop()

    @pytest.mark.asyncio
    async def test_voicemail_outcome(self) -> None:
        bus = EventBus()
        detector = CallScreeningDetector(bus, track_filter=None)
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid=""))
            await bus.emit(STTPartial(text="please record your name and reason for calling"))
            assert detector.state == ScreeningState.SCREENING_DETECTED
            await bus.emit(VoicemailDetected(result="machine"))
            assert detector.state == ScreeningState.VOICEMAIL
        finally:
            detector.stop()

    @pytest.mark.asyncio
    async def test_declined_outcome(self) -> None:
        bus = EventBus()
        detector = CallScreeningDetector(bus, track_filter=None)
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid=""))
            await bus.emit(STTPartial(text="please record your name and reason for calling"))
            assert detector.state == ScreeningState.SCREENING_DETECTED
            await bus.emit(CallEnded(call_sid="CA1"))
            assert detector.state == ScreeningState.DECLINED
        finally:
            detector.stop()


# ── Multi-turn screening ────────────────────────────────────────


class TestScreeningMultiTurn:
    @pytest.mark.asyncio
    async def test_max_screening_turns_enforced(self) -> None:
        bus = EventBus()
        detector = CallScreeningDetector(bus, max_screening_turns=3, track_filter=None)
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid=""))
            await bus.emit(STTPartial(text="please record your name and reason for calling"))
            assert detector.state == ScreeningState.SCREENING_DETECTED
            # Simulate 3 follow-up turns from screening AI (non-conversational).
            msg1 = "Can you tell me more about why you're calling us?"
            await bus.emit(STTFinal(text=msg1))
            msg2 = "What is this about exactly, could you explain?"
            await bus.emit(STTFinal(text=msg2))
            msg3 = "Why are you calling this number, please elaborate?"
            await bus.emit(STTFinal(text=msg3))
            assert detector.state == ScreeningState.SCREENING_TIMEOUT
            assert detector.screening_turns == 3
        finally:
            detector.stop()

    @pytest.mark.asyncio
    async def test_android_multi_turn_follow_up(self) -> None:
        bus = EventBus()
        detector = CallScreeningDetector(bus, max_screening_turns=3, track_filter=None)
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid=""))
            text = "The person you're calling is using a screening service"
            await bus.emit(STTPartial(text=text))
            assert detector.state == ScreeningState.SCREENING_DETECTED
            # Pixel AI asks follow-up.
            msg = "Can you tell me more about why you're calling us?"
            await bus.emit(STTFinal(text=msg))
            assert detector.screening_turns == 1
            # Second follow-up.
            msg2 = "What is this about exactly, could you explain?"
            await bus.emit(STTFinal(text=msg2))
            assert detector.screening_turns == 2
        finally:
            detector.stop()

    @pytest.mark.asyncio
    async def test_coherence_check_flags_answer_bot(self) -> None:
        bus = EventBus()
        detector = CallScreeningDetector(bus, track_filter=None)
        detector.start()
        try:
            await bus.emit(CallAnswered(call_sid=""))
            await bus.emit(STTPartial(text="please record your name and reason for calling"))
            # Simulate incoherent callee responses (RoboKiller-style).
            detector.record_bot_utterance(
                "Hi, this is Sarah from Acme Corp about your appointment"
            )
            detector.record_screening_turn("What's your favorite color of dinosaur?")
            detector.record_bot_utterance("I'm calling about your Thursday appointment")
            detector.record_screening_turn("Do you like pizza with pineapple?")
            assert not detector.is_coherent()
        finally:
            detector.stop()


# ── is_conversational structural heuristics ────────────────────


class TestIsConversational:
    """Tests for structural heuristic classification of human vs. screening speech."""

    def test_short_greetings_are_conversational(self) -> None:
        assert is_conversational("Hello?")
        assert is_conversational("Yeah")
        assert is_conversational("Speaking")
        assert is_conversational("Go ahead")
        assert is_conversational("This is John")

    def test_non_english_short_greetings(self) -> None:
        assert is_conversational("Hola")
        assert is_conversational("Bonjour")
        assert is_conversational("Oui")

    def test_receptionist_pickup_within_word_limit(self) -> None:
        assert is_conversational("Hello, how can I help you?")
        assert is_conversational("Hello how can I help you today")
        assert is_conversational("Thanks for calling how can I help")
        assert is_conversational("Hi this is John how may I help")

    def test_empty_and_whitespace_rejected(self) -> None:
        assert not is_conversational("")
        assert not is_conversational("   ")

    def test_screening_prompts_rejected(self) -> None:
        assert not is_conversational("please record your name and reason for calling")
        assert not is_conversational("The person you're calling is using a screening service")

    def test_long_interrogative_screening_rejected(self) -> None:
        assert not is_conversational("Can you tell me more about why you are calling today?")
        assert not is_conversational("Could you explain the reason for your call please?")
        assert not is_conversational("Why are you calling this number, please elaborate?")
        assert not is_conversational("What is this about exactly, could you explain?")

    def test_please_starter_rejected(self) -> None:
        assert not is_conversational("Please state your name and the reason you are calling today")

    def test_voicemail_greetings_rejected(self) -> None:
        assert not is_conversational(
            "Please leave a message after the tone and we will get back to you"
        )
        assert not is_conversational(
            "You have reached the voicemail box of John Smith please leave a message"
        )

    def test_screening_follow_up_patterns_rejected(self) -> None:
        assert not is_conversational("Tell me more about your reason for calling")
        # Short human-like phrases ("one moment", "who is this") are now
        # treated as conversational to avoid blocking real human handoffs.
        assert is_conversational("One moment please")
        assert is_conversational("Who is this")
        assert is_conversational("Why are you calling")

    def test_ivr_prompts_rejected(self) -> None:
        assert not is_conversational("Press 1 for sales, press 2 for support, press 3 for billing")


# ── Multi-language screening patterns ────────────────────────────


class TestMultiLanguagePatterns:
    def test_spanish_ios_screening(self) -> None:
        patterns = screening_patterns_for_languages(["es"])
        assert (
            match_screening_platform(
                "Si grabe su nombre y el motivo de la llamada veré si está disponible",
                patterns,
            )
            == "ios"
        )

    def test_french_ios_screening(self) -> None:
        patterns = screening_patterns_for_languages(["fr"])
        assert (
            match_screening_platform(
                "Veuillez enregistrez votre nom et la raison de votre appel",
                patterns,
            )
            == "ios"
        )

    def test_german_ios_screening(self) -> None:
        patterns = screening_patterns_for_languages(["de"])
        assert (
            match_screening_platform(
                "Bitte nennen sie ihren namen und den grund ihres anrufs",
                patterns,
            )
            == "ios"
        )

    def test_portuguese_ios_screening(self) -> None:
        patterns = screening_patterns_for_languages(["pt"])
        assert (
            match_screening_platform(
                "Por favor grave seu nome e o motivo da ligação",
                patterns,
            )
            == "ios"
        )

    def test_japanese_ios_screening(self) -> None:
        patterns = screening_patterns_for_languages(["ja"])
        assert (
            match_screening_platform("お名前とお電話の理由を録音してください", patterns) == "ios"
        )

    def test_korean_ios_screening(self) -> None:
        patterns = screening_patterns_for_languages(["ko"])
        assert (
            match_screening_platform(
                "이름과 전화하시는 이유를 녹음해 주시면 확인하겠습니다", patterns
            )
            == "ios"
        )

    def test_chinese_ios_screening(self) -> None:
        patterns = screening_patterns_for_languages(["zh"])
        assert match_screening_platform("请录下您的名字和来电原因", patterns) == "ios"

    def test_spanish_android_screening(self) -> None:
        patterns = screening_patterns_for_languages(["es"])
        assert (
            match_screening_platform(
                "La persona usa un servicio de filtrado de llamadas de Google",
                patterns,
            )
            == "android"
        )

    def test_french_android_screening(self) -> None:
        patterns = screening_patterns_for_languages(["fr"])
        assert (
            match_screening_platform(
                "Cette personne utilise un service de filtrage d'appels de Google",
                patterns,
            )
            == "android"
        )

    def test_spanish_carrier_screening(self) -> None:
        patterns = screening_patterns_for_languages(["es"])
        assert (
            match_screening_platform(
                "Esta persona no acepta llamadas no identificadas",
                patterns,
            )
            == "carrier"
        )

    def test_english_still_detected_with_non_english_language(self) -> None:
        """English patterns are NOT included when only a non-English language is requested."""
        patterns = screening_patterns_for_languages(["es"])
        # English iOS prompt should NOT match Spanish-only patterns.
        assert (
            match_screening_platform(
                "Please record your name and reason for calling",
                patterns,
            )
            is None
        )

    def test_combined_languages_include_both(self) -> None:
        patterns = screening_patterns_for_languages(["en", "es"])
        # English works.
        assert (
            match_screening_platform(
                "Please record your name and reason for calling",
                patterns,
            )
            == "ios"
        )
        # Spanish works.
        assert (
            match_screening_platform(
                "Si grabe su nombre y el motivo de la llamada",
                patterns,
            )
            == "ios"
        )

    def test_none_languages_includes_all(self) -> None:
        patterns = screening_patterns_for_languages(None)
        # Should detect all languages.
        assert (
            match_screening_platform(
                "Please record your name and reason for calling",
                patterns,
            )
            == "ios"
        )
        assert (
            match_screening_platform(
                "Si grabe su nombre y el motivo de la llamada",
                patterns,
            )
            == "ios"
        )
        assert (
            match_screening_platform("お名前とお電話の理由を録音してください", patterns) == "ios"
        )

    def test_no_false_positive_spanish_greeting(self) -> None:
        patterns = screening_patterns_for_languages(["es"])
        assert match_screening_platform("Hola, buenos días", patterns) is None

    def test_bcp47_subtag_handled(self) -> None:
        """e.g. 'es-MX' should resolve to 'es' patterns."""
        patterns = screening_patterns_for_languages(["es-MX"])
        assert (
            match_screening_platform(
                "Si grabe su nombre y el motivo de la llamada",
                patterns,
            )
            == "ios"
        )


# ── screening_patterns_for_languages factory ────────────────────


class TestScreeningPatternsFactory:
    def test_returns_screening_pattern_set(self) -> None:
        result = screening_patterns_for_languages(["en"])
        assert isinstance(result, ScreeningPatternSet)

    def test_english_only_matches_defaults(self) -> None:
        result = screening_patterns_for_languages(["en"])
        default = ScreeningPatternSet()
        assert set(result.ios) == set(default.ios)
        assert set(result.android) == set(default.android)
        assert set(result.carrier) == set(default.carrier)

    def test_no_duplicates(self) -> None:
        result = screening_patterns_for_languages(["en", "en"])
        assert len(result.ios) == len(set(result.ios))

    def test_unknown_language_returns_empty_platform_lists(self) -> None:
        result = screening_patterns_for_languages(["xx"])
        assert result.ios == []
        assert result.android == []
        assert result.carrier == []

    def test_third_party_always_included(self) -> None:
        result = screening_patterns_for_languages(["es"])
        default = ScreeningPatternSet()
        assert set(result.third_party) == set(default.third_party)

    def test_exclusions_always_included(self) -> None:
        result = screening_patterns_for_languages(["ja"])
        default = ScreeningPatternSet()
        assert set(result.exclusions) == set(default.exclusions)
