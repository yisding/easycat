"""Validator for the ``easycat init --config`` JSON payload.

Schema v1 is intentionally small.  Unknown top-level keys are rejected
with a fuzzy-match suggestion so coding agents (Claude Code, Cursor,
Codex) get immediate feedback on typos.  The ``schema_version`` field
is the guarded extension point — bump it on breaking changes and
document the older version under ``easycat explain init-schema``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from difflib import get_close_matches
from pathlib import Path
from typing import Any

from easycat.errors import EASYCAT_E102, EASYCAT_E103

# Keys accepted in the top-level ``--config`` JSON object.  Anything
# else fails loudly with EASYCAT_E102 + fuzzy suggestion.
SCHEMA_V1_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "template",
        "stt",
        "tts",
        "llm",
        "transport",
        "agent_name",
        "agent_instructions",
        "tools",
        "mcp_servers",
    }
)


@dataclass
class InitConfig:
    """Typed view of a validated ``--config`` payload."""

    template: str
    stt: str | None = None
    tts: str | None = None
    llm: str | None = None
    transport: str = "local"
    agent_name: str | None = None
    agent_instructions: str | None = None
    tools: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)


def available_templates() -> list[str]:
    """Return every template directory name, sorted."""
    root = Path(__file__).parent / "templates"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("_"))


def _reject(problem: str) -> None:
    raise EASYCAT_E102(problem=problem)


def _unknown_keys(payload: dict[str, Any]) -> list[str]:
    return [k for k in payload if k not in SCHEMA_V1_KEYS]


def _fuzzy_suggest(key: str) -> str:
    matches = get_close_matches(key, sorted(SCHEMA_V1_KEYS), n=1, cutoff=0.5)
    return matches[0] if matches else ""


def parse_config(raw: str) -> InitConfig:
    """Parse and validate a ``--config`` JSON string.

    Returns an :class:`InitConfig` on success.  Raises :class:`EasyCatError`
    with code ``EASYCAT_E102`` on malformed JSON or unknown keys, and
    ``EASYCAT_E103`` when the requested template is not in the catalog.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        _reject(f"not valid JSON ({exc.msg} at column {exc.colno})")

    if not isinstance(payload, dict):
        _reject("top-level value must be a JSON object")

    schema_version = payload.get("schema_version")
    if schema_version is None:
        _reject("missing required key 'schema_version'")
    if schema_version != 1:
        _reject(
            f"unsupported schema_version={schema_version!r} — "
            f"this version of easycat understands schema_version=1"
        )

    template = payload.get("template")
    if not template or not isinstance(template, str):
        _reject("missing required key 'template'")

    if unknown := _unknown_keys(payload):
        bad = unknown[0]
        suggestion = _fuzzy_suggest(bad)
        hint = f" Did you mean {suggestion!r}?" if suggestion else ""
        _reject(f"unknown key {bad!r}.{hint}")

    templates = available_templates()
    if template not in templates:
        raise EASYCAT_E103(template=template, available=", ".join(templates))

    # Coerce list-typed fields so downstream code can iterate without
    # re-checking types.
    def _as_str_list(key: str) -> list[str]:
        value = payload.get(key, [])
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            _reject(f"{key!r} must be a list of strings")
        return list(value)

    def _as_optional_str(key: str) -> str | None:
        value = payload.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            _reject(f"{key!r} must be a string")
        return value

    return InitConfig(
        template=template,
        stt=_as_optional_str("stt"),
        tts=_as_optional_str("tts"),
        llm=_as_optional_str("llm"),
        transport=_as_optional_str("transport") or "local",
        agent_name=_as_optional_str("agent_name"),
        agent_instructions=_as_optional_str("agent_instructions"),
        tools=_as_str_list("tools"),
        mcp_servers=_as_str_list("mcp_servers"),
    )
