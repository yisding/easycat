"""Tests for TTSInput compatibility helpers."""

from easycat.tts.input import TTSInput, coerce_tts_input, strip_ssml_tags


def test_coerce_tts_input_from_string() -> None:
    payload = coerce_tts_input("hello")
    assert payload == TTSInput(text="hello", format="plain")


def test_coerce_tts_input_passthrough() -> None:
    payload = TTSInput(text="<speak>hello</speak>", format="ssml")
    assert coerce_tts_input(payload) is payload


def test_strip_ssml_tags() -> None:
    assert strip_ssml_tags('<speak>Call <break time="100ms"/> now</speak>') == "Call now"
