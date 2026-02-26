"""Tests for LLM output processor helpers."""

from easycat.llm_output_processing import (
    PhoneNumberSSMLProcessor,
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
    assert isinstance(processors[1], PhoneNumberSSMLProcessor)
