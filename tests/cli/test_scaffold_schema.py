from __future__ import annotations

import json
from dataclasses import fields

import pytest

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
    ],
)
@pytest.mark.parametrize("value", [42, None])
def test_parse_config_rejects_present_non_string_optional_fields(
    field_name: str,
    value: object,
) -> None:
    with pytest.raises(EasyCatError, match=rf"{field_name!r} must be a string"):
        parse_config(_config_json(**{field_name: value}))


@pytest.mark.parametrize("field_name", ["tools", "mcp_servers"])
@pytest.mark.parametrize("value", ["not-a-list", ["valid", 42]])
def test_parse_config_rejects_invalid_string_lists(
    field_name: str,
    value: object,
) -> None:
    with pytest.raises(EasyCatError, match=rf"{field_name!r} must be a list of strings"):
        parse_config(_config_json(**{field_name: value}))
