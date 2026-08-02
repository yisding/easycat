from __future__ import annotations

import json
from dataclasses import fields

import pytest

from easycat.cli.scaffold import _schema
from easycat.cli.scaffold._schema import (
    SCHEMA_V1_KEYS,
    InitConfig,
    parse_config,
)
from easycat.errors import EasyCatError


def _config_json(**overrides: object) -> str:
    payload: dict[str, object] = {
        "schema_version": 1,
        "template": "text-chat",
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_schema_keys_derive_from_init_config_fields() -> None:
    assert SCHEMA_V1_KEYS == {
        "schema_version",
        *(item.name for item in fields(InitConfig)),
    }


def test_parse_config_dispatches_every_field_kind() -> None:
    config = parse_config(
        _config_json(
            stt="deepgram/flux",
            tts="elevenlabs/flash",
            llm="openai/gpt",
            transport="webrtc",
            agent_name="Support",
            agent_instructions="Help the caller",
            tools=["weather"],
            mcp_servers=["stdio:///bin/echo"],
            easycat_source="../easycat",
            easycat_git=None,
            easycat_git_rev=None,
        )
    )

    assert config == InitConfig(
        template="text-chat",
        stt="deepgram/flux",
        tts="elevenlabs/flash",
        llm="openai/gpt",
        transport="webrtc",
        agent_name="Support",
        agent_instructions="Help the caller",
        tools=["weather"],
        mcp_servers=["stdio:///bin/echo"],
        easycat_source="../easycat",
        easycat_git=None,
        easycat_git_rev=None,
    )


@pytest.mark.parametrize("schema_version", [True, 1.0, "1", 2])
def test_parse_config_requires_exact_integer_schema_version(schema_version: object) -> None:
    with pytest.raises(EasyCatError, match="unsupported schema_version"):
        parse_config(_config_json(schema_version=schema_version))


def test_parse_config_requires_closed_json_object() -> None:
    with pytest.raises(EasyCatError, match="top-level value must be a JSON object"):
        parse_config("[]")
    with pytest.raises(EasyCatError, match="unknown key 'extra'"):
        parse_config(_config_json(extra=True))


def test_parse_config_wraps_oversized_integer_decoder_error() -> None:
    raw = '{"schema_version":' + ("9" * 5000) + ',"template":"text-chat"}'

    with pytest.raises(EasyCatError) as exc_info:
        parse_config(raw)

    assert exc_info.value.code == "EASYCAT_E102"
    assert "not valid JSON" in exc_info.value.message


def test_parse_config_wraps_excessive_nesting_decoder_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_recursion_error(_raw: str) -> object:
        raise RecursionError("maximum recursion depth exceeded while decoding JSON")

    monkeypatch.setattr(_schema.json, "loads", raise_recursion_error)

    with pytest.raises(EasyCatError) as exc_info:
        parse_config('{"schema_version":1,"template":"text-chat"}')

    assert exc_info.value.code == "EASYCAT_E102"
    assert "maximum nesting depth exceeded" in exc_info.value.message


def test_parse_config_distinguishes_required_field_presence_and_type() -> None:
    with pytest.raises(EasyCatError, match="missing required key 'template'"):
        parse_config(json.dumps({"schema_version": 1}))
    with pytest.raises(EasyCatError, match="'template' must be a string"):
        parse_config(_config_json(template=42))


@pytest.mark.parametrize("template", ["", " ", "\t"])
def test_parse_config_rejects_blank_required_strings(template: str) -> None:
    with pytest.raises(EasyCatError, match="'template' must be a non-empty string"):
        parse_config(_config_json(template=template))


@pytest.mark.parametrize(
    "field_name",
    [
        "stt",
        "tts",
        "llm",
        "transport",
        "agent_name",
        "agent_instructions",
        "easycat_source",
        "easycat_git",
        "easycat_git_rev",
    ],
)
@pytest.mark.parametrize("value", [42, True, []])
def test_parse_config_rejects_present_non_string_optional_fields(
    field_name: str,
    value: object,
) -> None:
    with pytest.raises(EasyCatError, match=rf"{field_name!r} must be a string"):
        parse_config(_config_json(**{field_name: value}))


def test_parse_config_accepts_explicit_null_for_optional_strings() -> None:
    config = parse_config(
        _config_json(
            stt=None,
            tts=None,
            llm=None,
            transport=None,
            agent_name=None,
            agent_instructions=None,
            easycat_source=None,
            easycat_git=None,
            easycat_git_rev=None,
        )
    )

    assert config == InitConfig(template="text-chat")


@pytest.mark.parametrize("field_name", ["tools", "mcp_servers"])
@pytest.mark.parametrize("value", ["not-a-list", ["valid", 42]])
def test_parse_config_rejects_invalid_string_lists(
    field_name: str,
    value: object,
) -> None:
    with pytest.raises(EasyCatError, match=rf"{field_name!r} must be a list of strings"):
        parse_config(_config_json(**{field_name: value}))
