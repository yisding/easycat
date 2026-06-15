"""Manifest discovery + ``tomllib`` parsing/validation (M6a).

:func:`load_manifest` is the entry point: it discovers an ``easycat.toml`` (from
``--manifest`` / ``EASYCAT_MANIFEST`` / the working directory), parses it with the
stdlib :mod:`tomllib`, validates each table with unknown-key strictness, enforces
the ``bearer-env:NAME`` secret contract, and returns a typed
:class:`~easycat.project.manifest.ProjectManifest`.

Validation imports no heavy provider/runtime SDK (the schema leaf is SDK-free and
the agent/EasyConfig conversion is deferred to ``ProjectManifest`` methods), so a
manifest can be validated — and its secret contract enforced — without importing
the audio pipeline. That keeps ``easycat serve --manifest`` fast to fail.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from easycat.errors import EASYCAT_E601, EASYCAT_E602
from easycat.project.manifest import ProjectManifest
from easycat.project.schema import (
    ProjectSection,
    ServerSection,
    VoiceProfile,
    parse_auth_reference,
    validate_transport,
)

# Discovery order: explicit path, then env override, then the conventional name.
ENV_MANIFEST_VAR = "EASYCAT_MANIFEST"
DEFAULT_MANIFEST_NAME = "easycat.toml"

# Allow-listed keys per table — unknown keys raise so typos fail loudly (matching
# the init-schema strictness). ``[voice.<name>]`` profile keys are listed here;
# anything else surfaces as an EASYCAT_E602.
_PROJECT_KEYS = frozenset({"name"})
_SERVER_KEYS = frozenset({"host", "port", "max_sessions", "auth"})
_VOICE_KEYS = frozenset(
    {"transport", "agent", "stt", "tts", "vad", "debug", "path", "stream_url", "token"}
)


def discover_manifest_path(
    path: str | Path | None = None,
    *,
    environ: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> Path:
    """Resolve the manifest path or RAISE :data:`EASYCAT_E601`.

    Discovery order: an explicit ``path``, then ``EASYCAT_MANIFEST``, then
    ``easycat.toml`` in ``cwd``. The returned path is absolute (resolved against
    ``cwd`` when relative) so relative profile paths inside the manifest resolve
    against a stable directory.
    """
    env = environ if environ is not None else dict(os.environ)
    base = cwd or Path.cwd()
    candidate: Path | None = None
    if path is not None:
        candidate = Path(path)
    elif env.get(ENV_MANIFEST_VAR):
        candidate = Path(env[ENV_MANIFEST_VAR])
    else:
        candidate = base / DEFAULT_MANIFEST_NAME

    resolved = candidate if candidate.is_absolute() else base / candidate
    if not resolved.is_file():
        raise EASYCAT_E601(path=str(resolved))
    return resolved.resolve()


def load_manifest(
    path: str | Path | None = None,
    *,
    environ: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> ProjectManifest:
    """Discover, parse, and validate an ``easycat.toml`` into a manifest."""
    resolved = discover_manifest_path(path, environ=environ, cwd=cwd)
    try:
        raw = tomllib.loads(resolved.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise EASYCAT_E602(path=str(resolved), problem=f"not valid TOML: {exc}")
    except OSError as exc:
        raise EASYCAT_E602(path=str(resolved), problem=f"could not read file: {exc}")
    return parse_manifest(raw, source_path=resolved)


def parse_manifest(raw: dict[str, Any], *, source_path: Path | None = None) -> ProjectManifest:
    """Validate a parsed TOML mapping into a :class:`ProjectManifest`.

    Split out from :func:`load_manifest` so tests can validate an in-memory
    mapping without writing a file. Enforces unknown-key strictness, the
    transport-shortcut allow-list, and the ``bearer-env:NAME`` secret contract.
    """
    where = str(source_path or DEFAULT_MANIFEST_NAME)

    project = _parse_project(raw.get("project", {}), where)
    server = _parse_server(raw.get("server", {}), where)
    profiles = _parse_voice(raw.get("voice", {}), where)
    if not profiles:
        raise EASYCAT_E602(
            path=where,
            problem="no [voice.<profile>] tables found; at least one is required",
        )

    return ProjectManifest(
        project=project,
        server=server,
        profiles=profiles,
        source_path=source_path,
    )


def _reject_unknown(
    table: Any, allowed: frozenset[str], *, path: str, where: str
) -> dict[str, Any]:
    if not isinstance(table, dict):
        raise EASYCAT_E602(path=where, problem=f"{path} must be a table")
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise EASYCAT_E602(
            path=where,
            problem=f"{path} has unknown key(s): {unknown}; allowed: {sorted(allowed)}",
        )
    return table


def _parse_project(table: Any, where: str) -> ProjectSection:
    table = _reject_unknown(table, _PROJECT_KEYS, path="[project]", where=where)
    name = table.get("name")
    if name is not None and not isinstance(name, str):
        raise EASYCAT_E602(path=where, problem="[project] name must be a string")
    return ProjectSection(name=name)


def _parse_server(table: Any, where: str) -> ServerSection:
    table = _reject_unknown(table, _SERVER_KEYS, path="[server]", where=where)
    host = table.get("host", "127.0.0.1")
    port = table.get("port", 8080)
    max_sessions = table.get("max_sessions", 64)
    if not isinstance(host, str):
        raise EASYCAT_E602(path=where, problem="[server] host must be a string")
    if not isinstance(port, int) or isinstance(port, bool):
        raise EASYCAT_E602(path=where, problem="[server] port must be an integer")
    if not isinstance(max_sessions, int) or isinstance(max_sessions, bool):
        raise EASYCAT_E602(path=where, problem="[server] max_sessions must be an integer")

    auth_raw = table.get("auth")
    auth = (
        parse_auth_reference(auth_raw, field_name="[server] auth")
        if auth_raw is not None
        else None
    )
    return ServerSection(host=host, port=port, max_sessions=max_sessions, auth=auth)


def _parse_voice(table: Any, where: str) -> dict[str, VoiceProfile]:
    if not isinstance(table, dict):
        raise EASYCAT_E602(path=where, problem="[voice] must be a table of profiles")
    profiles: dict[str, VoiceProfile] = {}
    for name, profile_table in table.items():
        profiles[name] = _parse_profile(name, profile_table, where)
    return profiles


def _parse_profile(name: str, table: Any, where: str) -> VoiceProfile:
    table = _reject_unknown(table, _VOICE_KEYS, path=f"[voice.{name}]", where=where)
    transport_raw = table.get("transport")
    if transport_raw is None:
        raise EASYCAT_E602(path=where, problem=f"[voice.{name}] is missing required 'transport'")
    transport = validate_transport(transport_raw, profile=name)

    def _str_field(key: str) -> str | None:
        value = table.get(key)
        if value is not None and not isinstance(value, str):
            raise EASYCAT_E602(path=where, problem=f"[voice.{name}] {key} must be a string")
        return value

    token_raw = table.get("token")
    token = (
        parse_auth_reference(token_raw, field_name=f"[voice.{name}] token")
        if token_raw is not None
        else None
    )

    return VoiceProfile(
        name=name,
        transport=transport,
        agent=_str_field("agent"),
        stt=_str_field("stt"),
        tts=_str_field("tts"),
        vad=_str_field("vad"),
        debug=_str_field("debug"),
        path=_str_field("path"),
        stream_url=_str_field("stream_url"),
        token=token,
    )


__all__ = [
    "DEFAULT_MANIFEST_NAME",
    "ENV_MANIFEST_VAR",
    "discover_manifest_path",
    "load_manifest",
    "parse_manifest",
]
