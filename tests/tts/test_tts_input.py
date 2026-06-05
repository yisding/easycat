"""Tests for TTSInput compatibility helpers."""

import pytest

from easycat.tts.input import (
    TTSInput,
    TTSInputPolicy,
    coerce_tts_input,
    resolve_tts_input_policy,
    strip_ssml_tags,
)


def test_coerce_tts_input_from_string() -> None:
    payload = coerce_tts_input("hello")
    assert payload == TTSInput(text="hello", format="plain")


def test_coerce_tts_input_passthrough() -> None:
    payload = TTSInput(text="<speak>hello</speak>", format="ssml")
    assert coerce_tts_input(payload) is payload


def test_strip_ssml_tags() -> None:
    assert strip_ssml_tags('<speak>Call <break time="100ms"/> now</speak>') == "Call now"


def test_tts_input_policy_defaults_to_plain_text() -> None:
    policy = TTSInputPolicy.plain_text()

    assert policy.accepted_formats == ("plain",)
    assert not policy.supports_ssml
    assert policy.accepts("plain")
    assert not policy.accepts("ssml")
    assert policy.to_dict() == {
        "accepted_formats": ["plain"],
        "supports_ssml": False,
        "unsupported_ssml": "strip",
        "streaming_boundary": "sentence",
        "pause_support": "none",
        "pronunciation_support": "none",
        "marker_support": "none",
    }


def test_tts_input_policy_native_ssml_describes_ssml_controls() -> None:
    policy = TTSInputPolicy.native_ssml(provider_options={"voice": "test"})

    assert policy.accepted_formats == ("plain", "ssml")
    assert policy.supports_ssml
    assert policy.accepts("ssml")
    assert policy.to_dict() == {
        "accepted_formats": ["plain", "ssml"],
        "supports_ssml": True,
        "unsupported_ssml": "strip",
        "streaming_boundary": "sentence",
        "pause_support": "ssml_break",
        "pronunciation_support": "ssml_phoneme",
        "marker_support": "none",
        "provider_options": {"voice": "test"},
    }


def test_tts_input_policy_normalizes_formats_and_freezes_options() -> None:
    provider_options = {"voice": "a"}
    policy = TTSInputPolicy(
        accepted_formats=("plain", "plain", "ssml"),
        provider_options=provider_options,
    )
    provider_options["voice"] = "b"

    assert policy.accepted_formats == ("plain", "ssml")
    assert dict(policy.provider_options) == {"voice": "a"}
    with pytest.raises(TypeError):
        policy.provider_options["voice"] = "c"  # type: ignore[index]


@pytest.mark.parametrize(
    "accepted_formats",
    [
        (),
        ("ssml",),
        ("plain", "markdown"),
    ],
)
def test_tts_input_policy_rejects_invalid_formats(accepted_formats: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        TTSInputPolicy(accepted_formats=accepted_formats)  # type: ignore[arg-type]


def test_resolve_tts_input_policy_prefers_typed_policy() -> None:
    class Provider:
        supports_ssml = False
        input_policy = TTSInputPolicy.native_ssml()

    assert resolve_tts_input_policy(Provider()).supports_ssml


@pytest.mark.parametrize(("supports_ssml", "expected"), [(False, False), (True, True)])
def test_resolve_tts_input_policy_uses_legacy_flag(
    supports_ssml: bool,
    expected: bool,
) -> None:
    class Provider:
        pass

    provider = Provider()
    provider.supports_ssml = supports_ssml

    assert resolve_tts_input_policy(provider).supports_ssml is expected


def test_resolve_tts_input_policy_rejects_malformed_policy() -> None:
    class Provider:
        input_policy = {"accepted_formats": ["plain"]}

    with pytest.raises(TypeError, match="input_policy"):
        resolve_tts_input_policy(Provider())
