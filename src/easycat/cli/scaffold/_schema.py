"""Validator for the ``easycat init --config`` JSON payload.

Schema v1 is intentionally small.  Unknown top-level keys are rejected
with a fuzzy-match suggestion so coding agents (Claude Code, Cursor,
Codex) get immediate feedback on typos.  The ``schema_version`` field
is the guarded extension point — bump it on breaking changes and
document the older version under ``easycat explain init-schema``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from difflib import get_close_matches
from enum import StrEnum
from pathlib import Path
from typing import Any, Never, cast

from easycat.errors import EASYCAT_E102, EASYCAT_E103

# Directory names that can appear as local tooling, cache, build, or secret-bearing
# artifacts next to bundled templates, but must never be treated as scaffold
# templates or copied into generated projects.
TEMPLATE_ARTIFACT_DIRECTORY_NAMES: frozenset[str] = frozenset(
    {
        "__pycache__",
        ".agents",
        ".claude",
        ".codex",
        ".easycat",
        ".git",
        ".github",
        ".hypothesis",
        ".mypy_cache",
        ".mutmut-cache",
        ".pipecat-bench",
        ".pytest_cache",
        ".ruff_cache",
        ".uv-cache",
        ".venv",
        "build",
        "dist",
        "htmlcov",
        "mutants",
        "runs",
        "site",
    }
)


class _InitFieldKind(StrEnum):
    REQUIRED_STRING = "required_string"
    OPTIONAL_STRING = "optional_string"
    STRING_LIST = "string_list"


_FIELD_KIND_METADATA_KEY = "easycat.init_field_kind"
_REQUIRED_STRING_METADATA = {_FIELD_KIND_METADATA_KEY: _InitFieldKind.REQUIRED_STRING}
_OPTIONAL_STRING_METADATA = {_FIELD_KIND_METADATA_KEY: _InitFieldKind.OPTIONAL_STRING}
_STRING_LIST_METADATA = {_FIELD_KIND_METADATA_KEY: _InitFieldKind.STRING_LIST}


@dataclass
class InitConfig:
    """Typed view of a validated ``--config`` payload."""

    template: str = field(metadata=_REQUIRED_STRING_METADATA)
    stt: str | None = field(default=None, metadata=_OPTIONAL_STRING_METADATA)
    tts: str | None = field(default=None, metadata=_OPTIONAL_STRING_METADATA)
    llm: str | None = field(default=None, metadata=_OPTIONAL_STRING_METADATA)
    transport: str | None = field(default=None, metadata=_OPTIONAL_STRING_METADATA)
    agent_name: str | None = field(default=None, metadata=_OPTIONAL_STRING_METADATA)
    agent_instructions: str | None = field(default=None, metadata=_OPTIONAL_STRING_METADATA)
    tools: list[str] = field(default_factory=list, metadata=_STRING_LIST_METADATA)
    mcp_servers: list[str] = field(default_factory=list, metadata=_STRING_LIST_METADATA)
    easycat_source: str | None = field(default=None, metadata=_OPTIONAL_STRING_METADATA)
    easycat_git: str | None = field(default=None, metadata=_OPTIONAL_STRING_METADATA)
    easycat_git_rev: str | None = field(default=None, metadata=_OPTIONAL_STRING_METADATA)


_INIT_CONFIG_FIELDS = fields(InitConfig)

# Treat the JSON object as a closed schema: model fields are accepted and
# every other key fails loudly with EASYCAT_E102 + a fuzzy suggestion.
SCHEMA_V1_KEYS: frozenset[str] = frozenset(
    {"schema_version", *(item.name for item in _INIT_CONFIG_FIELDS)}
)


def available_templates() -> list[str]:
    """Return every template directory name, sorted."""
    root = Path(__file__).parent / "templates"
    if not root.is_dir():
        return []
    return sorted(
        p.name
        for p in root.iterdir()
        if p.is_dir()
        and not p.name.startswith("_")
        and p.name not in TEMPLATE_ARTIFACT_DIRECTORY_NAMES
    )


def _reject(problem: str) -> Never:
    raise EASYCAT_E102(problem=problem)


def _unknown_keys(payload: dict[str, Any]) -> list[str]:
    return [k for k in payload if k not in SCHEMA_V1_KEYS]


def _fuzzy_suggest(key: str) -> str:
    matches = get_close_matches(key, sorted(SCHEMA_V1_KEYS), n=1, cutoff=0.5)
    return matches[0] if matches else ""


def _parse_json_object(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        _reject(f"not valid JSON ({exc.msg} at column {exc.colno})")
    except RecursionError:
        _reject("not valid JSON (maximum nesting depth exceeded)")
    except ValueError as exc:
        # CPython raises a plain ValueError, rather than JSONDecodeError,
        # when a JSON integer exceeds its configured digit limit.
        _reject(f"not valid JSON ({exc})")

    if not isinstance(payload, dict):
        _reject("top-level value must be a JSON object")
    return cast(dict[str, Any], payload)


def _validate_schema_version(payload: Mapping[str, Any]) -> None:
    schema_version = payload.get("schema_version")
    if schema_version is None:
        _reject("missing required key 'schema_version'")
    if type(schema_version) is not int or schema_version != 1:
        _reject(
            f"unsupported schema_version={schema_version!r} — "
            f"this version of easycat understands schema_version=1"
        )


def _reject_unknown_keys(payload: dict[str, Any]) -> None:
    if not (unknown := _unknown_keys(payload)):
        return
    bad = unknown[0]
    suggestion = _fuzzy_suggest(bad)
    hint = f" Did you mean {suggestion!r}?" if suggestion else ""
    _reject(f"unknown key {bad!r}.{hint}")


def _as_required_string(payload: Mapping[str, Any], key: str) -> str:
    if key not in payload:
        _reject(f"missing required key {key!r}")
    value = payload[key]
    if not isinstance(value, str):
        _reject(f"{key!r} must be a string")
    if not value.strip():
        _reject(f"{key!r} must be a non-empty string")
    return value


def _as_optional_string(payload: Mapping[str, Any], key: str) -> str | None:
    if key not in payload:
        return None
    value = payload[key]
    # Schema v1 has always treated an explicit JSON null like an omitted
    # optional field. Preserve that machine-generated payload contract.
    if value is None:
        return None
    if not isinstance(value, str):
        _reject(f"{key!r} must be a string")
    return value


def _as_string_list(payload: Mapping[str, Any], key: str) -> list[str]:
    value = payload.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _reject(f"{key!r} must be a list of strings")
    return list(value)


_FieldParser = Callable[[Mapping[str, Any], str], Any]
_FIELD_PARSERS: Mapping[_InitFieldKind, _FieldParser] = {
    _InitFieldKind.REQUIRED_STRING: _as_required_string,
    _InitFieldKind.OPTIONAL_STRING: _as_optional_string,
    _InitFieldKind.STRING_LIST: _as_string_list,
}


def _parse_config_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for item in _INIT_CONFIG_FIELDS:
        kind = item.metadata.get(_FIELD_KIND_METADATA_KEY)
        if not isinstance(kind, _InitFieldKind):
            raise RuntimeError(f"InitConfig field {item.name!r} has no parser contract")
        parsed[item.name] = _FIELD_PARSERS[kind](payload, item.name)
    return parsed


def parse_config(raw: str) -> InitConfig:
    """Parse and validate a ``--config`` JSON string.

    Returns an :class:`InitConfig` on success.  Raises :class:`EasyCatError`
    with code ``EASYCAT_E102`` on malformed JSON or unknown keys, and
    ``EASYCAT_E103`` when the requested template is not in the catalog.
    """
    payload = _parse_json_object(raw)
    _validate_schema_version(payload)
    _reject_unknown_keys(payload)
    config = InitConfig(**_parse_config_fields(payload))

    templates = available_templates()
    if config.template not in templates:
        raise EASYCAT_E103(template=config.template, available=", ".join(templates))
    return config
