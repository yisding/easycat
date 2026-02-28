"""Tests for LLM output processor helpers."""

from easycat.llm_output_processing import (
    PhoneNumberSSMLProcessor,
    PhoneticReplacementProcessor,
    RegexPauseSSMLProcessor,
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
    assert isinstance(processors[1], PhoneNumberSSMLProcessor)


def test_regex_pause_processor_inserts_breaks_for_user_pattern() -> None:
    processor = RegexPauseSSMLProcessor(
        pattern=r"ticket\s+#?\d+",
        pause_ms=180,
        unit_pattern=r"\d",
        minimum_units=2,
        flags=0,
    )
    payload = processor.process(
        TTSInput("Please reference ticket #48291 before the call."),
        is_final=True,
        is_streaming=False,
    )
    assert payload.format == "ssml"
    assert '<break time="180ms"/>' in payload.text
    assert "4 <break" in payload.text


def test_phone_number_processor_uses_digit_pause_behavior() -> None:
    processor = PhoneNumberSSMLProcessor(pause_ms=130)
    payload = processor.process(
        TTSInput("Call me at (415) 555-2671."),
        is_final=True,
        is_streaming=False,
    )
    assert payload.format == "ssml"
    assert '<break time="130ms"/>' in payload.text
    assert "4 <break" in payload.text
