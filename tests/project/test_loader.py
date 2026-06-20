"""Manifest discovery, parsing, validation, and the secret contract (M6a).

These exercise real loader behavior: discovery order, ``tomllib`` parsing,
unknown-key strictness, transport-shortcut validation, and the testable
``bearer-env:NAME`` secret contract (a literal secret RAISES the coded error;
the redacted dump shows no resolved token).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from easycat.errors import EasyCatError
from easycat.project import (
    DEFAULT_MANIFEST_NAME,
    ENV_MANIFEST_VAR,
    discover_manifest_path,
    load_manifest,
    parse_manifest,
)


def _write(tmp_path: Path, body: str, name: str = DEFAULT_MANIFEST_NAME) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


_MINIMAL = """
[project]
name = "support-voice-agent"

[server]
host = "0.0.0.0"
port = 8080
max_sessions = 64
auth = "bearer-env:EASYCAT_SERVE_TOKEN"

[voice.default]
transport = "webrtc"
agent = "python:app:create_agent"
stt = "openai/realtime"
tts = "openai"
vad = "silero"
debug = "light"

[voice.websocket]
transport = "websocket"
path = "/ws"
"""


# ── discovery ────────────────────────────────────────────────────────


def test_discover_explicit_path(tmp_path: Path) -> None:
    path = _write(tmp_path, _MINIMAL)
    resolved = discover_manifest_path(path)
    assert resolved == path.resolve()


def test_discover_env_var(tmp_path: Path) -> None:
    path = _write(tmp_path, _MINIMAL)
    resolved = discover_manifest_path(environ={ENV_MANIFEST_VAR: str(path)})
    assert resolved == path.resolve()


def test_discover_default_name_in_cwd(tmp_path: Path) -> None:
    path = _write(tmp_path, _MINIMAL)
    resolved = discover_manifest_path(cwd=tmp_path, environ={})
    assert resolved == path.resolve()


def test_discover_missing_raises_e601(tmp_path: Path) -> None:
    with pytest.raises(EasyCatError) as exc_info:
        discover_manifest_path(cwd=tmp_path, environ={})
    assert exc_info.value.code == "EASYCAT_E601"


# ── parsing ────────────────────────────────────────────────────────────


def test_load_minimal_manifest(tmp_path: Path) -> None:
    path = _write(tmp_path, _MINIMAL)
    manifest = load_manifest(path)
    assert manifest.project.name == "support-voice-agent"
    assert manifest.server.host == "0.0.0.0"
    assert manifest.server.port == 8080
    assert manifest.server.max_sessions == 64
    assert set(manifest.profiles) == {"default", "websocket"}

    default = manifest.profiles["default"]
    assert default.transport == "webrtc"
    assert default.agent == "python:app:create_agent"
    assert default.stt == "openai/realtime"
    assert default.tts == "openai"
    assert default.vad == "silero"
    assert default.debug == "light"
    assert manifest.profiles["websocket"].path == "/ws"


def test_invalid_toml_raises_e602(tmp_path: Path) -> None:
    path = _write(tmp_path, "[server\nport = ")
    with pytest.raises(EasyCatError) as exc_info:
        load_manifest(path)
    assert exc_info.value.code == "EASYCAT_E602"


def test_no_voice_profiles_raises_e602(tmp_path: Path) -> None:
    path = _write(tmp_path, '[project]\nname = "x"\n')
    with pytest.raises(EasyCatError) as exc_info:
        load_manifest(path)
    assert exc_info.value.code == "EASYCAT_E602"


def test_unknown_server_key_raises_e602() -> None:
    with pytest.raises(EasyCatError) as exc_info:
        parse_manifest({"server": {"prot": 8080}, "voice": {"default": {"transport": "local"}}})
    assert exc_info.value.code == "EASYCAT_E602"
    assert "prot" in str(exc_info.value)


def test_unknown_voice_key_raises_e602() -> None:
    with pytest.raises(EasyCatError) as exc_info:
        parse_manifest({"voice": {"default": {"transport": "local", "agnt": "python:app:f"}}})
    assert exc_info.value.code == "EASYCAT_E602"
    assert "agnt" in str(exc_info.value)


def test_unknown_transport_raises_e602() -> None:
    with pytest.raises(EasyCatError) as exc_info:
        parse_manifest({"voice": {"default": {"transport": "carrier-pigeon"}}})
    assert exc_info.value.code == "EASYCAT_E602"


def test_missing_transport_raises_e602() -> None:
    with pytest.raises(EasyCatError) as exc_info:
        parse_manifest({"voice": {"default": {"agent": "python:app:f"}}})
    assert exc_info.value.code == "EASYCAT_E602"


def test_non_integer_port_raises_e602() -> None:
    with pytest.raises(EasyCatError) as exc_info:
        parse_manifest({"server": {"port": "8080"}, "voice": {"default": {"transport": "local"}}})
    assert exc_info.value.code == "EASYCAT_E602"


# ── secret contract (acceptance) ───────────────────────────────────────


@pytest.mark.parametrize(
    "literal",
    [
        "sk-abcdef1234567890ABCDEF",  # OpenAI-style literal key
        "hunter2supersecretvalue",  # a bare literal
        "Bearer sk-abcdef1234567890",  # a header-style literal
    ],
)
def test_literal_secret_in_server_auth_is_rejected(literal: str) -> None:
    # Acceptance (a): a literal secret in [server] auth is REJECTED with E603.
    with pytest.raises(EasyCatError) as exc_info:
        parse_manifest(
            {
                "server": {"auth": literal},
                "voice": {"default": {"transport": "local"}},
            }
        )
    assert exc_info.value.code == "EASYCAT_E603"


def test_literal_secret_in_voice_token_is_rejected() -> None:
    with pytest.raises(EasyCatError) as exc_info:
        parse_manifest(
            {
                "voice": {
                    "default": {
                        "transport": "twilio",
                        "token": "sk-abcdef1234567890",
                    }
                }
            }
        )
    assert exc_info.value.code == "EASYCAT_E603"


def test_malformed_env_reference_name_is_rejected() -> None:
    with pytest.raises(EasyCatError) as exc_info:
        parse_manifest(
            {
                "server": {"auth": "bearer-env:not a valid name"},
                "voice": {"default": {"transport": "local"}},
            }
        )
    assert exc_info.value.code == "EASYCAT_E603"


def test_bearer_env_reference_is_accepted_without_resolving() -> None:
    # The env-reference grammar is accepted at parse time WITHOUT reading the
    # env (no token resolved during validation).
    manifest = parse_manifest(
        {
            "server": {"auth": "bearer-env:MY_TOKEN"},
            "voice": {"default": {"transport": "local"}},
        }
    )
    assert manifest.server.auth is not None
    assert manifest.server.auth.reference == "bearer-env:MY_TOKEN"
    assert manifest.server.auth.env_var == "MY_TOKEN"
