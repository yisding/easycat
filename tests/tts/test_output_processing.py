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
