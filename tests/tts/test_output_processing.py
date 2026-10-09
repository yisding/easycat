"""Tests for LLM output processor helpers."""

import pytest

from easycat.llm_output_processing import (
    MAX_SSML_BREAK_MS,
    PauseProcessor,
    PhoneticReplacementProcessor,
    default_pronunciation_processors,
)
from easycat.tts.input import TTSInput


def test_phonetic_replacement_processor_replaces_whole_terms_case_insensitive() -> None:
    processor = PhoneticReplacementProcessor({"Siobhan": "shi-vawn", "Nguyen": "win"})
    payload = processor.process(
        TTSInput("Ask SIOBHAN and Nguyen, but not Nguyenston."),
        is_final=True,
        is_streaming=False,
    )
    assert payload.text == "Ask shi-vawn and win, but not Nguyenston."
    assert payload.format == "plain"


def test_phonetic_replacement_processor_does_not_rewrite_its_own_output() -> None:
    processor = PhoneticReplacementProcessor({"AI": "A I", "I": "eye"})
    payload = processor.process(
        TTSInput("AI and I"),
        is_final=True,
        is_streaming=False,
    )
    assert payload.text == "A I and eye"


def test_phonetic_replacement_does_not_rewrite_already_spoken_terms() -> None:
    """Each source term must be replaced once, against the original text.

    ``"Dr"`` is spoken as ``"Doctor"``, and ``"Doctor"`` has its own entry. The
    output of the first rule must not be fed back through the second rule.
    """
    processor = PhoneticReplacementProcessor({"Dr": "Doctor", "Doctor": "Dok-tur"})
    payload = processor.process(
        TTSInput("Dr Smith asked the Doctor."),
        is_final=True,
        is_streaming=False,
    )
    assert payload.text == "Doctor Smith asked the Dok-tur."


def test_default_pronunciation_processors_order() -> None:
    processors = default_pronunciation_processors(
        name_pronunciations={"Siobhan": "shi-vawn"},
        phone_pause_ms=150,
    )
    assert isinstance(processors[0], PhoneticReplacementProcessor)
    assert isinstance(processors[1], PauseProcessor)
    assert processors[1].style == "ellipsis"
    assert processors[1].pause_ms == 150


def test_regex_pause_processor_inserts_breaks_for_user_pattern() -> None:
    processor = PauseProcessor(
        pattern=r"ticket\s+#?\d+",
        pause_ms=180,
        unit_pattern=r"\d",
        minimum_units=2,
        flags=0,
        style="ssml",
    )
    payload = processor.process(
        TTSInput("Please reference ticket #48291 before the call."),
        is_final=True,
        is_streaming=False,
    )
    assert payload.format == "ssml"
    assert '<break time="180ms"/>' in payload.text
    assert "4 <break" in payload.text


def test_default_pronunciation_helper_phone_regex_behavior() -> None:
    processors = default_pronunciation_processors(phone_pause_ms=130)
    payload = processors[-1].process(
        TTSInput("Call me at (415) 555-2671."),
        is_final=True,
        is_streaming=False,
    )
    assert payload.format == "plain"
    assert "4 ... 1 ... 5 ... 5 ... 5 ... 5 ... 2 ... 6 ... 7 ... 1" in payload.text
    assert "<break" not in payload.text


@pytest.mark.parametrize(
    "text",
    [
        "Released on 2024-01-15.",
        "Released on 2024/01/15.",
        "Meeting 2024-01-15 at 10.30",
        "10:30 2024-01-15",
        "Revenue grew in 2023 (12.5%) overall.",
        "Pi is roughly 3.14159265.",
        "The ratio was 123.4567890.",
        "Scores: 12 34 56 78.",
        "Upgrade to 1.2.3.4.5.6.7 today.",
        "Room 555\n1234 is free.",
        "Call 555\n123\n4567 later.",
        "Part 555-123-4567-89 shipped.",
        "ID 12-555-1234 closed.",
        "2024-01-15",
        "12.5%",
        "100-2000",
        "Read pages 100-2000 first.",
        "pages 555-1234",
        "Valid range 555-1234 to 555-9999.",
        "Serial 12345678901 shipped.",
        "Path a/555-2671 moved.",
        "At 10:555-2671 it stopped.",
        "Token x)555-2671 expired.",
        "Extension 415-555-2671/22 is busy.",
        r"File C:\Users\4155552671 saved.",
        r"File D:\2024\415-555-2671 saved.",
        "Call +1 2345 6789 0123 4567 8901 now.",
        "Call +123 4567 8901 2345 6789.",
        "He was recalled 555-0100 times.",
    ],
)
def test_default_phone_pauses_leave_non_phone_numbers_unchanged(text: str) -> None:
    """Dates, decimals, ranges, and runs of short numbers are not phone numbers.

    A bare dash-joined 3+4 digit pair is paced only after a phone cue such as
    "call" or "tel:", and the tail of a longer token (after ``)``, ``/``, or
    ``:``) is never paced on its own.

    The default pattern used to match any run of seven or more digits joined by
    spaces, dots, dashes, or parentheses (including across newlines), so an ISO
    date became ``"2 ... 0 ... 2 ... 4 ..."`` and ``"2023 (12.5%)"`` lost its
    decimal point and opening parenthesis.
    """
    processors = default_pronunciation_processors()
    payload = TTSInput(text)

    result = processors[-1].process(payload, is_final=True, is_streaming=False)

    assert result is payload


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Call (415) 555-2671.", "Call 4 ... 1 ... 5 ... 5 ... 5 ... 5 ... 2 ... 6 ... 7 ... 1."),
        ("Call 415-555-2671.", "Call 4 ... 1 ... 5 ... 5 ... 5 ... 5 ... 2 ... 6 ... 7 ... 1."),
        ("Call 415.555.2671.", "Call 4 ... 1 ... 5 ... 5 ... 5 ... 5 ... 2 ... 6 ... 7 ... 1."),
        ("Call 415 555 0142.", "Call 4 ... 1 ... 5 ... 5 ... 5 ... 5 ... 0 ... 1 ... 4 ... 2."),
        ("Call 555-0100.", "Call 5 ... 5 ... 5 ... 0 ... 1 ... 0 ... 0."),
        ("call me at 555-0100", "call me at 5 ... 5 ... 5 ... 0 ... 1 ... 0 ... 0"),
        ("Phone number: 555-0100", "Phone number: 5 ... 5 ... 5 ... 0 ... 1 ... 0 ... 0"),
        ("Text me at 555-0100", "Text me at 5 ... 5 ... 5 ... 0 ... 1 ... 0 ... 0"),
        ("Text us at 555-0100", "Text us at 5 ... 5 ... 5 ... 0 ... 1 ... 0 ... 0"),
        ("Fax number: 555-0100", "Fax number: 5 ... 5 ... 5 ... 0 ... 1 ... 0 ... 0"),
        ("Cell number is 555-0100", "Cell number is 5 ... 5 ... 5 ... 0 ... 1 ... 0 ... 0"),
        ("Contact: 555-0100", "Contact: 5 ... 5 ... 5 ... 0 ... 1 ... 0 ... 0"),
        ("Tel: 555-0100", "Tel: 5 ... 5 ... 5 ... 0 ... 1 ... 0 ... 0"),
        ("(415) 555-2671", "4 ... 1 ... 5 ... 5 ... 5 ... 5 ... 2 ... 6 ... 7 ... 1"),
        ("1(415)555-2671", "1 ... 4 ... 1 ... 5 ... 5 ... 5 ... 5 ... 2 ... 6 ... 7 ... 1"),
        (
            "Call 1(415)555-2671 now.",
            "Call 1 ... 4 ... 1 ... 5 ... 5 ... 5 ... 5 ... 2 ... 6 ... 7 ... 1 now.",
        ),
        ("+1 415 555 2671", "1 ... 4 ... 1 ... 5 ... 5 ... 5 ... 5 ... 2 ... 6 ... 7 ... 1"),
        (
            "+44 20 7946 0958",
            "4 ... 4 ... 2 ... 0 ... 7 ... 9 ... 4 ... 6 ... 0 ... 9 ... 5 ... 8",
        ),
        (
            "Call +1 (555) 123-4567.",
            "Call 1 ... 5 ... 5 ... 5 ... 1 ... 2 ... 3 ... 4 ... 5 ... 6 ... 7.",
        ),
        (
            "Call +1 415 555 0100.",
            "Call 1 ... 4 ... 1 ... 5 ... 5 ... 5 ... 5 ... 0 ... 1 ... 0 ... 0.",
        ),
        (
            "Call +44 20 7946 0958.",
            "Call 4 ... 4 ... 2 ... 0 ... 7 ... 9 ... 4 ... 6 ... 0 ... 9 ... 5 ... 8.",
        ),
        (
            "Call 2024-01-15 at (415) 555-2671.",
            "Call 2024-01-15 at 4 ... 1 ... 5 ... 5 ... 5 ... 5 ... 2 ... 6 ... 7 ... 1.",
        ),
    ],
)
def test_default_phone_pauses_pace_phone_number_shapes(text: str, expected: str) -> None:
    """Phone-number shapes are still paced digit by digit, whole and balanced."""
    processors = default_pronunciation_processors()

    result = processors[-1].process(TTSInput(text), is_final=True, is_streaming=False)

    assert result.text == expected
    assert result.format == "plain"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Call 415-555-2671x22.",
            "Call 4 ... 1 ... 5 ... 5 ... 5 ... 5 ... 2 ... 6 ... 7 ... 1x22.",
        ),
        (
            "Call (415) 555-2671x22.",
            "Call 4 ... 1 ... 5 ... 5 ... 5 ... 5 ... 2 ... 6 ... 7 ... 1x22.",
        ),
        (
            "Call 415-555-2671X22.",
            "Call 4 ... 1 ... 5 ... 5 ... 5 ... 5 ... 2 ... 6 ... 7 ... 1X22.",
        ),
        (
            "Call 415-555-2671ext22.",
            "Call 4 ... 1 ... 5 ... 5 ... 5 ... 5 ... 2 ... 6 ... 7 ... 1ext22.",
        ),
        (
            "Call 415-555-2671ext.22.",
            "Call 4 ... 1 ... 5 ... 5 ... 5 ... 5 ... 2 ... 6 ... 7 ... 1ext.22.",
        ),
    ],
)
def test_default_phone_pauses_pace_base_number_before_attached_extension(
    text: str, expected: str
) -> None:
    """An extension glued to the number is not a word continuation.

    Only the base number is paced; the extension stays literal text.
    """
    processors = default_pronunciation_processors()

    result = processors[-1].process(TTSInput(text), is_final=True, is_streaming=False)

    assert result.text == expected


@pytest.mark.parametrize(
    "text",
    [
        "Call 415-555-2671abc.",
        "Call 415-555-2671xyz22.",
        "Call 415-555-2671x.",
        "Call 415-555-2671ext.",
    ],
)
def test_default_phone_pauses_reject_other_word_continuations(text: str) -> None:
    """Only ``x``/``ext`` followed by digits may touch the number."""
    processors = default_pronunciation_processors()
    payload = TTSInput(text)

    result = processors[-1].process(payload, is_final=True, is_streaming=False)

    assert result is payload


def test_default_pronunciation_helper_can_opt_into_exact_ssml_breaks() -> None:
    processors = default_pronunciation_processors(
        phone_pause_style="ssml",
        phone_pause_ms=130,
    )
    payload = processors[-1].process(
        TTSInput("Call 415-555-2671."),
        is_final=True,
        is_streaming=False,
    )

    assert payload.format == "ssml"
    assert '<break time="130ms"/>' in payload.text


def test_pause_processor_does_not_promote_literal_break_tags_from_source_text() -> None:
    processor = PauseProcessor(
        pattern=r"\+?\d[\d\s().-]{5,}\d",
        pause_ms=120,
        unit_pattern=r"\d",
        minimum_units=7,
        style="ssml",
    )
    payload = processor.process(
        TTSInput('Say <break time="999999ms"/> and then call 415-555-2671.'),
        is_final=True,
        is_streaming=False,
    )

    assert payload.format == "ssml"
    assert '<break time="120ms"/>' in payload.text
    assert '<break time="999999ms"/>' not in payload.text
    assert "&lt;break time=&quot;999999ms&quot;/&gt;" in payload.text


def test_pause_processor_plain_text_styles() -> None:
    base = TTSInput("ticket #48291")

    ellipsis = PauseProcessor(
        pattern=r"ticket\s+#?\d+",
        unit_pattern=r"\d",
        minimum_units=2,
        style="ellipsis",
        ellipsis_count=1,
    ).process(base, is_final=True, is_streaming=False)
    assert ellipsis.format == "plain"
    assert "..." in ellipsis.text
    assert "... ..." not in ellipsis.text

    double_ellipsis = PauseProcessor(
        pattern=r"ticket\s+#?\d+",
        unit_pattern=r"\d",
        minimum_units=2,
        style="ellipsis",
        ellipsis_count=2,
    ).process(base, is_final=True, is_streaming=False)
    assert double_ellipsis.format == "plain"
    assert "... ..." in double_ellipsis.text

    emdash = PauseProcessor(
        pattern=r"ticket\s+#?\d+",
        unit_pattern=r"\d",
        minimum_units=2,
        style="emdash",
    ).process(base, is_final=True, is_streaming=False)
    assert emdash.format == "plain"
    assert "—" in emdash.text


def test_pause_processor_escapes_literal_break_markup_from_model_text() -> None:
    processor = PauseProcessor(
        pattern=r"ticket\s+#?\d+",
        pause_ms=180,
        unit_pattern=r"\d",
        minimum_units=2,
        style="ssml",
    )
    payload = processor.process(
        TTSInput('Literal <break time="999999ms"/> text before ticket #48291.'),
        is_final=True,
        is_streaming=False,
    )

    assert payload.format == "ssml"
    assert "&lt;break time=&quot;999999ms&quot;/&gt;" in payload.text
    assert '<break time="999999ms"/>' not in payload.text
    assert '<break time="180ms"/>' in payload.text


def test_pause_processor_clamps_ssml_break_duration() -> None:
    processor = PauseProcessor(
        pattern=r"ticket\s+#?\d+",
        pause_ms=99_999,
        unit_pattern=r"\d",
        minimum_units=2,
        style="ssml",
    )
    payload = processor.process(
        TTSInput("Please reference ticket #48291 before the call."),
        is_final=True,
        is_streaming=False,
    )

    assert payload.format == "ssml"
    assert f'<break time="{MAX_SSML_BREAK_MS}ms"/>' in payload.text
    assert '<break time="99999ms"/>' not in payload.text


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"style": "comma"}, "pause style"),
        ({"minimum_units": 0}, "minimum_units"),
        ({"minimum_units": True}, "minimum_units"),
        ({"style": "ellipsis", "ellipsis_count": 0}, "ellipsis_count"),
        ({"style": "ellipsis", "ellipsis_count": True}, "ellipsis_count"),
        ({"pattern": "["}, "invalid pattern"),
        ({"unit_pattern": "["}, "invalid unit_pattern"),
    ],
)
def test_pause_processor_rejects_invalid_policy(kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        PauseProcessor(**({"pattern": r"\d+"} | kwargs))


def test_pause_processor_no_match_preserves_original_payload() -> None:
    payload = TTSInput('<speak>Keep <break time="50ms"/> this.</speak>', format="ssml")
    processor = PauseProcessor(pattern=r"ticket\s+#?\d+", unit_pattern=r"\d")

    result = processor.process(payload, is_final=True, is_streaming=False)

    assert result is payload


def test_pause_processor_unit_capture_groups_select_full_match() -> None:
    processor = PauseProcessor(
        pattern=r"ticket\s+#?\d+",
        unit_pattern=r"(\d)",
        minimum_units=2,
        style="ellipsis",
    )

    result = processor.process(
        TTSInput("ticket #482"),
        is_final=True,
        is_streaming=False,
    )

    assert result.text == "4 ... 8 ... 2"
