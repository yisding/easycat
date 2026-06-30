"""Streaming segmentation and interruption-estimation helper tests."""

from __future__ import annotations

from easycat._turn_context import TURN_AUDIO_LOG_MAXLEN, TurnContext
from easycat.cancel import CancelToken
from easycat.session.interruption import (
    TtsChunk,
    _all_tts_audio_delivered,
    _audio_bytes_acknowledged,
    _audio_bytes_likely_heard,
    _audio_bytes_likely_heard_hybrid,
    _estimate_text_spoken,
    estimate_and_notify_interruption,
)
from easycat.session.text import (
    _cleanup_estimation_text,
    _text_for_estimation_timeline,
    split_at_sentence_boundaries,
    split_first_clause,
)
from easycat.tts.input import TTSInput


def test_streaming_trigger_chars_superset_of_segmenter_terminators():
    """Every sentence-segmenter terminal punctuation char must be a trigger.

    If the segmenter would close a sentence on a character the streaming
    throttle does not treat as a trigger, that sentence stalls until the
    final flush.  Keep ``_STREAMING_SENTENCE_TRIGGER_CHARS`` a strict
    superset of the segmenter's terminal punctuation (it additionally
    carries ``\\n``/``\\r``)."""
    from easycat.session._streaming import _STREAMING_SENTENCE_TRIGGER_CHARS
    from easycat.session.text import _SENTENCE_SEGMENTER

    # The attribute name below is defined by the sentencesplit library.
    terminators = set(_SENTENCE_SEGMENTER.language_module.Punctuations)  # codespell:ignore
    assert terminators  # guard against an empty/renamed segmenter attribute
    missing = terminators - _STREAMING_SENTENCE_TRIGGER_CHARS
    assert not missing, f"trigger set missing segmenter terminators: {sorted(missing)}"
    # The fullwidth full stop U+FF0E is one of those terminators specifically.
    assert "．" in _STREAMING_SENTENCE_TRIGGER_CHARS


def test_split_no_boundary():
    ready, remaining = split_at_sentence_boundaries("hello world")
    assert ready == ""
    assert remaining == "hello world"


def test_split_single_sentence():
    # Lookahead sees the trailing period as a stable boundary, so the single
    # complete sentence is emitted rather than buffered until the LLM
    # finishes.
    ready, remaining = split_at_sentence_boundaries("Hello world. ")
    assert ready == "Hello world. "
    assert remaining == ""


def test_split_multiple_sentences():
    text = "Hello. How are you? Fine"
    ready, remaining = split_at_sentence_boundaries(text)
    # Should split at last boundary (after "you? ")
    assert "Hello" in ready
    assert "How are you" in ready
    assert remaining == "Fine"


def test_split_incomplete_sentence():
    ready, remaining = split_at_sentence_boundaries("Hello world. How are")
    assert "Hello world" in ready
    assert remaining == "How are"


def test_split_abbreviation_sentence():
    text = "Dr. Smith went home. Next"
    ready, remaining = split_at_sentence_boundaries(text)
    assert ready.strip() == "Dr. Smith went home."
    assert remaining == "Next"


def test_split_trailing_abbreviation():
    ready, remaining = split_at_sentence_boundaries("Nice to meet you Mr. ")
    assert ready == ""
    assert remaining == "Nice to meet you Mr. "


def test_split_newline_sentence():
    text = "Hello world!\nHow are you"
    ready, remaining = split_at_sentence_boundaries(text)
    assert ready.strip() == "Hello world!"
    assert remaining == "How are you"


def test_split_spanish_sentence():
    text = "Hola mundo. ¿Cómo estás? Bien"
    ready, remaining = split_at_sentence_boundaries(text)
    assert "Hola mundo." in ready
    assert "¿Cómo estás?" in ready
    assert remaining == "Bien"


def test_split_chinese_sentence():
    text = "你好。今天天气不错。继续"
    ready, remaining = split_at_sentence_boundaries(text)
    assert ready == "你好。今天天气不错。"
    assert remaining == "继续"


def test_split_first_clause_empty_inputs():
    assert split_first_clause("") == ("", "")
    assert split_first_clause("   ") == ("", "   ")


def test_split_first_clause_emits_clause_at_comma():
    # A comma after a sufficiently long clause ships that clause early —
    # before the full sentence terminator arrives.
    ready, remaining = split_first_clause(
        "Let me look into that for you, and I will report back soon."
    )
    assert ready == "Let me look into that for you, "
    assert remaining == "and I will report back soon."


def test_split_first_clause_skips_decimal_point() -> None:
    ready, remaining = split_first_clause("The estimate is 3.5 seconds, then continue.")
    assert ready == "The estimate is 3.5 seconds, "
    assert remaining == "then continue."


def test_split_first_clause_skips_url_scheme_colon() -> None:
    ready, remaining = split_first_clause("Visit https://example.com now, then continue.")
    assert ready == "Visit https://example.com now, "
    assert remaining == "then continue."


def test_split_first_clause_holds_url_scheme_colon_for_lookahead() -> None:
    ready, remaining = split_first_clause("Visit https:")
    assert ready == ""
    assert remaining == "Visit https:"


def test_split_first_clause_splits_after_complete_url_at_comma() -> None:
    ready, remaining = split_first_clause("Visit https://example.com, then continue.")
    assert ready == "Visit https://example.com, "
    assert remaining == "then continue."


def test_split_first_clause_skips_wrapped_url_scheme_colon() -> None:
    ready, remaining = split_first_clause("Visit (https://example.com) now, then continue.")
    assert ready == "Visit (https://example.com) now, "
    assert remaining == "then continue."


def test_split_first_clause_holds_wrapped_url_scheme_colon_for_lookahead() -> None:
    ready, remaining = split_first_clause("Visit [https:")
    assert ready == ""
    assert remaining == "Visit [https:"


def test_split_first_clause_holds_trailing_numeric_period_for_lookahead() -> None:
    ready, remaining = split_first_clause("The estimate is 3.")
    assert ready == ""
    assert remaining == "The estimate is 3."


def test_split_first_clause_skips_short_opener_fragment():
    # "Sure," is too short to ship on its own; the split falls through to the
    # sentence terminator instead of emitting a clipped fragment.
    ready, remaining = split_first_clause("Sure, let me check that for you.")
    assert ready == "Sure, let me check that for you."
    assert remaining == ""


def test_split_first_clause_short_opener_alone_waits():
    # A short opener with no later boundary is not shipped at all; the caller
    # keeps buffering until a real boundary (or the final flush) arrives.
    ready, remaining = split_first_clause("Sure, ")
    assert ready == ""
    assert remaining == "Sure, "


def test_split_first_clause_handles_semicolon_and_colon():
    ready, remaining = split_first_clause("Here is the plan; we start now.")
    assert ready == "Here is the plan; "
    assert remaining == "we start now."

    ready, remaining = split_first_clause("Two options are available: A and B.")
    assert ready == "Two options are available: "
    assert remaining == "A and B."


def test_text_for_estimation_timeline_encodes_ssml_breaks() -> None:
    payload = TTSInput(
        text='<speak>Hello<break time="500ms"/>world</speak>',
        format="ssml",
    )
    timeline = _text_for_estimation_timeline(payload)
    assert "Hello" in timeline and "world" in timeline
    assert timeline != "Hello world"


def test_text_for_estimation_timeline_supports_single_quoted_breaks() -> None:
    payload = TTSInput(
        text="<speak>Hello<break time='500ms'/>world</speak>",
        format="ssml",
    )

    timeline = _text_for_estimation_timeline(payload)
    assert "Hello" in timeline and "world" in timeline
    assert timeline != "Hello world"
    assert timeline.count("\ue000") == 7


def test_text_for_estimation_timeline_supports_second_breaks() -> None:
    payload = TTSInput(
        text='<speak>Hello<break time="0.5s"/>world</speak>',
        format="ssml",
    )

    timeline = _text_for_estimation_timeline(payload)
    assert "Hello" in timeline and "world" in timeline
    assert timeline != "Hello world"
    assert timeline.count("\ue000") == 7


def test_cleanup_estimation_text_removes_pause_markers() -> None:
    payload = TTSInput(
        text='<speak>A<break time="500ms"/>B</speak>',
        format="ssml",
    )
    cleaned = _cleanup_estimation_text(_text_for_estimation_timeline(payload))
    assert cleaned == "AB"


def test_estimate_text_spoken_with_pause_markers_advances_less_text() -> None:
    # First chunk contains synthetic pause markers, so half-byte progress should
    # include less visible text than the same bytes on plain text.
    with_pause = _estimate_text_spoken([("AB" + "\ue000" * 10 + "CD", 1000, True)], 500)
    plain = _estimate_text_spoken([("ABCD", 1000, True)], 500)
    assert len(with_pause.replace("\ue000", "")) <= len(plain)


def test_estimate_text_spoken_empty():
    assert _estimate_text_spoken([], 0) == ""
    assert _estimate_text_spoken([], 100) == ""
    assert _estimate_text_spoken([("Hello.", 320, True)], 0) == ""


def test_estimate_text_spoken_full_chunks():
    """When all audio was sent, the full text is returned."""
    chunks = [("Hello. ", 320, True), ("How are you?", 640, True)]
    assert _estimate_text_spoken(chunks, 960) == "Hello. How are you?"
    # More bytes sent than produced — still returns full text
    assert _estimate_text_spoken(chunks, 9999) == "Hello. How are you?"


def test_estimate_text_spoken_partial_first_chunk():
    """When only part of the first chunk's audio was sent, estimate proportionally."""
    chunks = [("Hello world.", 1000, True)]
    # Half the audio sent → approximately half the text
    assert _estimate_text_spoken(chunks, 500) == "Hello "


def test_estimate_text_spoken_one_and_a_half_chunks():
    """First chunk fully sent, second chunk partially sent."""
    chunks = [("First sentence. ", 400, True), ("Second sentence.", 400, True)]
    # All of first chunk (400) + half of second (200) = 600
    spoken = _estimate_text_spoken(chunks, 600)
    assert spoken.startswith("First sentence. ")
    # Second chunk has 16 chars, half → 8 chars
    assert spoken == "First sentence. Second "


def test_estimate_text_spoken_partial_chunk_trims_mid_word_boundary():
    chunks = [("Alpha bravo charlie", 1000, True)]
    # 12 chars would land in the middle of "charlie"; should trim to word boundary.
    assert _estimate_text_spoken(chunks, 700) == "Alpha bravo "


def test_estimate_text_spoken_partial_single_token_keeps_prefix():
    chunks = [("supercalifragilistic", 1000, True)]
    # No internal boundary exists; keep proportional prefix instead of dropping content.
    assert _estimate_text_spoken(chunks, 300) == "superc"


def test_estimate_text_spoken_skips_zero_audio_chunks():
    """Chunks with 0 audio bytes (cancelled before any output) are skipped."""
    chunks = [("First. ", 320, True), ("Never spoken.", 0, True), ("Third.", 320, True)]
    # 320 covers first chunk, 0-byte chunk is skipped, then 320 for third
    assert _estimate_text_spoken(chunks, 640) == "First. Third."


def test_all_tts_audio_delivered():
    chunks = [("Hello. ", 320, True), ("How are you?", 640, True)]

    assert not _all_tts_audio_delivered([], 960)
    assert not _all_tts_audio_delivered(chunks, 959)
    assert _all_tts_audio_delivered(chunks, 960)
    assert _all_tts_audio_delivered(chunks, 9999)


def test_all_tts_audio_delivered_ignores_non_positive_chunks():
    chunks = [("First.", 320, True), ("Silent", 0, True), ("Oops", -20, True)]
    assert not _all_tts_audio_delivered(chunks, 319)
    assert _all_tts_audio_delivered(chunks, 320)


def test_all_tts_audio_delivered_requires_completed_synthesis():
    chunks = [("Hello", 320, False)]
    assert not _all_tts_audio_delivered(chunks, 320)


def test_all_tts_audio_delivered_zero_audio_is_still_fully_delivered():
    chunks = [("", 0, True)]
    assert _all_tts_audio_delivered(chunks, 0)


def test_tts_chunk_named_fields_flow_through_consumers():
    # The producer (TurnRunner._process_tts) builds TtsChunk instances, so the
    # consumers must accept them by named field as well as positionally.
    chunks = [
        TtsChunk(text="Hello. ", audio_bytes=320, completed=True),
        TtsChunk(text="How are you?", audio_bytes=640, completed=True),
    ]
    assert chunks[0].text == "Hello. "
    assert chunks[0].audio_bytes == 320
    assert chunks[0].completed is True

    assert _estimate_text_spoken(chunks, 960) == "Hello. How are you?"
    assert _all_tts_audio_delivered(chunks, 960)
    assert not _all_tts_audio_delivered(chunks, 959)


def test_audio_send_log_eviction_preserves_cumulative_heard_bytes(monkeypatch):
    now = 100.0

    def monotonic() -> float:
        nonlocal now
        current = now
        now += 0.001
        return current

    monkeypatch.setattr("easycat._turn_context.time.monotonic", monotonic)
    turn = TurnContext("long-turn", CancelToken())
    for _ in range(TURN_AUDIO_LOG_MAXLEN + 1):
        turn.record_audio_sent(1, 1.0)

    assert len(turn.audio_send_log) == TURN_AUDIO_LOG_MAXLEN
    assert turn.audio_send_log_base_bytes == 1
    assert turn.audio_send_log_base_playout_start is not None
    assert turn.audio_bytes_sent == TURN_AUDIO_LOG_MAXLEN + 1

    heard = _audio_bytes_likely_heard_hybrid(
        list(turn.audio_send_log),
        [],
        None,
        ack_stale_ms=500,
        ack_tail_cap_ms=500,
        send_log_base_bytes=turn.audio_send_log_base_bytes,
        send_log_base_playout_start=turn.audio_send_log_base_playout_start,
        send_log_base_playout_cursor=turn.audio_send_log_base_playout_cursor,
    )

    assert heard == TURN_AUDIO_LOG_MAXLEN + 1


def test_audio_send_log_base_accounts_partial_evicted_playout_window():
    heard = _audio_bytes_likely_heard(
        [],
        1.5,
        send_log_base_bytes=100,
        send_log_base_playout_start=1.0,
        send_log_base_playout_cursor=2.0,
    )

    assert heard == 50


def test_audio_send_log_base_cutoff_inside_window_does_not_count_retained_chunks():
    heard = _audio_bytes_likely_heard(
        [(1.1, 100, 1000.0)],
        1.5,
        send_log_base_bytes=100,
        send_log_base_playout_start=1.0,
        send_log_base_playout_cursor=2.0,
    )

    assert heard == 50


def test_long_turn_evicted_send_log_does_not_notify_when_fully_delivered(monkeypatch):
    now = 200.0

    def monotonic() -> float:
        nonlocal now
        current = now
        now += 0.001
        return current

    class Agent:
        def __init__(self) -> None:
            self.interruptions: list[str] = []

        def apply_interruption(self, delivered_text, cancellation_mode, **kwargs):
            self.interruptions.append(delivered_text)

    monkeypatch.setattr("easycat._turn_context.time.monotonic", monotonic)
    turn = TurnContext("long-turn", CancelToken())
    for _ in range(TURN_AUDIO_LOG_MAXLEN + 1):
        turn.record_audio_sent(1, 1.0)

    agent = Agent()
    result = estimate_and_notify_interruption(
        agent,
        None,
        turn,
        [TtsChunk("complete response", TURN_AUDIO_LOG_MAXLEN + 1, True)],
        tts_playback_started=True,
        interrupted=True,
        interruption_mode="truncate",
        latency_compensation_ms=0,
        ack_stale_ms=500,
        ack_tail_cap_ms=500,
    )

    assert result is None
    assert agent.interruptions == []


def test_audio_bytes_likely_heard_without_cutoff_uses_all_bytes():
    send_log = [(1.0, 100, 10.0), (1.2, 150, 10.0), (1.4, 50, 10.0)]
    assert _audio_bytes_likely_heard(send_log, None) == 300


def test_audio_bytes_likely_heard_with_cutoff_filters_future_bytes():
    send_log = [(1.0, 100, 10.0), (1.2, 150, 10.0), (1.4, 50, 10.0)]
    assert _audio_bytes_likely_heard(send_log, 1.2) == 100


def test_audio_bytes_likely_heard_ignores_negative_sizes():
    send_log = [(1.0, 100, 10.0), (1.2, -10, 10.0), (1.3, 20, 10.0)]
    assert _audio_bytes_likely_heard(send_log, 2.0) == 120


def test_audio_bytes_likely_heard_partial_chunk_by_duration():
    send_log = [(1.0, 100, 20.0)]
    assert _audio_bytes_likely_heard(send_log, 1.005) == 24


def test_audio_bytes_likely_heard_uses_cumulative_playout_clock():
    send_log = [
        (1.0, 100, 1000.0),  # plays from 1.0 to 2.0
        (1.01, 100, 1000.0),  # queued immediately but should start at 2.0
    ]
    assert _audio_bytes_likely_heard(send_log, 1.5) == 50


def test_audio_bytes_acknowledged_filters_by_cutoff():
    playback_ack_log = [(1.0, 120), (1.2, 220), (1.4, 320)]
    assert _audio_bytes_acknowledged(playback_ack_log, 1.2) == 220
    assert _audio_bytes_acknowledged(playback_ack_log, None) == 320


def test_audio_bytes_likely_heard_hybrid_fresh_ack_caps_heuristic():
    send_log = [(1.0, 100, 1000.0)]  # 100 B/s
    playback_ack_log = [(1.7, 70)]  # fresh and close to heuristic at cutoff=1.8
    heard = _audio_bytes_likely_heard_hybrid(
        send_log,
        playback_ack_log,
        1.8,
        ack_stale_ms=500,
        ack_tail_cap_ms=500,
    )
    assert heard == 70


def test_audio_bytes_likely_heard_hybrid_stale_ack_allows_bounded_tail():
    send_log = [(1.0, 100, 1000.0)]  # 100 B/s
    playback_ack_log = [(1.0, 20)]  # stale at cutoff=1.8 (800ms old)
    heard = _audio_bytes_likely_heard_hybrid(
        send_log,
        playback_ack_log,
        1.8,
        ack_stale_ms=500,
        ack_tail_cap_ms=500,  # allow +50 bytes
    )
    assert heard == 70


def test_audio_bytes_likely_heard_hybrid_tail_cap_zero_keeps_ack_cap():
    send_log = [(1.0, 100, 1000.0)]  # 100 B/s
    playback_ack_log = [(1.0, 20)]
    heard = _audio_bytes_likely_heard_hybrid(
        send_log,
        playback_ack_log,
        1.8,
        ack_stale_ms=500,
        ack_tail_cap_ms=0,
    )
    assert heard == 20
