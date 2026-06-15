"""``ProjectManifest`` behavior: redacted dump, auth resolution, conversion.

These exercise the acceptance (b) contract — the dump shows NO resolved token —
plus profile -> EasyConfig conversion and the ``python:module:function`` agent
resolver.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from easycat.errors import EasyCatError
from easycat.project import load_manifest, parse_manifest
from easycat.server.auth import BearerTokenAuth
from easycat.validation.redaction import contains_unredacted_sensitive_text

_RESOLVED_TOKEN = "sk-supersecret-token-1234567890"


def _manifest_with_auth() -> object:
    return parse_manifest(
        {
            "project": {"name": "demo"},
            "server": {"auth": "bearer-env:EASYCAT_SERVE_TOKEN", "port": 8080},
            "voice": {"default": {"transport": "webrtc", "agent": "python:app:create_agent"}},
        }
    )


# ── auth resolution ────────────────────────────────────────────────────


def test_resolve_auth_reads_token_from_environment() -> None:
    manifest = _manifest_with_auth()
    auth = manifest.resolve_auth({"EASYCAT_SERVE_TOKEN": _RESOLVED_TOKEN})
    assert isinstance(auth, BearerTokenAuth)
    assert auth.token == _RESOLVED_TOKEN


def test_resolve_auth_none_when_no_auth_configured() -> None:
    manifest = parse_manifest({"voice": {"default": {"transport": "local"}}})
    assert manifest.resolve_auth({}) is None


def test_resolve_auth_unset_env_raises_e604() -> None:
    manifest = _manifest_with_auth()
    with pytest.raises(EasyCatError) as exc_info:
        manifest.resolve_auth({})  # env var absent
    assert exc_info.value.code == "EASYCAT_E604"


# ── redacted dump (acceptance b) ───────────────────────────────────────


def test_redacted_dump_shows_reference_never_resolved_token() -> None:
    manifest = _manifest_with_auth()
    # Resolve the token first to prove it still never appears in the dump.
    manifest.resolve_auth({"EASYCAT_SERVE_TOKEN": _RESOLVED_TOKEN})

    dump = manifest.to_redacted_dict()
    serialized = json.dumps(dump)

    # The reference is shown (NAME is not a secret); the resolved token is not.
    assert dump["server"]["auth_ref"] == "bearer-env:EASYCAT_SERVE_TOKEN"
    assert _RESOLVED_TOKEN not in serialized
    assert not contains_unredacted_sensitive_text(serialized)


def test_redacted_dump_token_field_only_reference() -> None:
    manifest = parse_manifest(
        {
            "voice": {
                "phone": {
                    "transport": "twilio",
                    "token": "bearer-env:TWILIO_STREAM_TOKEN_SECRET",
                    "stream_url": "wss://example.com/twilio/media",
                }
            }
        }
    )
    dump = manifest.to_redacted_dict()
    assert dump["profiles"]["phone"]["auth_ref"] == "bearer-env:TWILIO_STREAM_TOKEN_SECRET"


# ── EasyConfig conversion ──────────────────────────────────────────────


@pytest.fixture
def _agent_module() -> object:
    """Install a throwaway module exposing a zero-arg ``create_agent`` factory."""
    module = types.ModuleType("_easycat_test_agent_mod")
    sentinel = object()

    async def _run(text: str) -> str:  # a minimal valid agent shape
        return text

    def create_agent() -> object:
        # Return an object with the duck-typed ``async run`` agent shape.
        obj = types.SimpleNamespace(run=_run, marker=sentinel)
        return obj

    module.create_agent = create_agent  # type: ignore[attr-defined]
    module._sentinel = sentinel  # type: ignore[attr-defined]
    sys.modules["_easycat_test_agent_mod"] = module
    try:
        yield module
    finally:
        sys.modules.pop("_easycat_test_agent_mod", None)


def test_to_easyconfig_websocket_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    # A dummy key lets EasyConfig construct so the converter's verbatim
    # forwarding of the provider shortcut strings is exercised end-to-end.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    from easycat.transports.websocket import WebSocketTransportConfig

    manifest = parse_manifest(
        {
            "voice": {
                "default": {
                    "transport": "websocket",
                    "stt": "openai/realtime",
                    "tts": "openai",
                }
            }
        }
    )
    config = manifest.to_easyconfig("default", resolve_agent=False)
    assert isinstance(config.transport, WebSocketTransportConfig)
    # EasyConfig normalizes the provider shortcut strings into provider configs;
    # the converter forwarded them (they are no longer ``None``).
    assert config.stt is not None
    assert config.tts is not None


def test_to_easyconfig_browser_profile_uses_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    from easycat.transports.webrtc import WebRTCTransportConfig

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    manifest = parse_manifest(
        {"voice": {"default": {"transport": "webrtc", "tts": "openai", "stt": "openai/realtime"}}}
    )
    config = manifest.to_easyconfig("default", resolve_agent=False)
    assert isinstance(config.transport, WebRTCTransportConfig)
    assert config.tts is not None  # forwarded + normalized by EasyConfig


def test_to_easyconfig_coerces_vad_shortcut_to_vad_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # REGRESSION: EasyConfig stores a ``vad`` shortcut string verbatim (it never
    # coerces it like stt/tts), so forwarding it raw would make create_session ->
    # create_vad('silero') raise AttributeError("'str' object has no attribute
    # 'backend'"). The manifest converter coerces it into a VADConfig.
    from easycat.vad import VADConfig

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    manifest = parse_manifest({"voice": {"default": {"transport": "webrtc", "vad": "silero"}}})
    config = manifest.to_easyconfig("default", resolve_agent=False)
    assert isinstance(config.vad, VADConfig)
    assert config.vad.backend == "silero"


def test_to_easyconfig_coerced_vad_drives_create_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The coerced VADConfig must let create_session build without the str-backend
    # crash that the raw shortcut caused.
    from easycat.config import create_session

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")

    class _Agent:
        async def run(self, text: str) -> str:
            return "ok"

    manifest = parse_manifest({"voice": {"default": {"transport": "websocket", "vad": "silero"}}})
    config = manifest.to_easyconfig("default", resolve_agent=False)
    config.agent = _Agent()
    config.debug = "off"
    session = create_session(config)
    assert session is not None


def test_to_easyconfig_unknown_vad_backend_raises_e602(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from easycat.errors import EasyCatError

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    manifest = parse_manifest(
        {"voice": {"default": {"transport": "websocket", "vad": "not-a-backend"}}}
    )
    with pytest.raises(EasyCatError) as excinfo:
        manifest.to_easyconfig("default", resolve_agent=False)
    assert excinfo.value.code == "EASYCAT_E602"


def test_to_easyconfig_resolves_python_agent(
    _agent_module: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    manifest = parse_manifest(
        {
            "voice": {
                "default": {
                    "transport": "websocket",
                    "agent": "python:_easycat_test_agent_mod:create_agent",
                    "stt": "openai/realtime",
                    "tts": "openai",
                }
            }
        }
    )
    config = manifest.to_easyconfig("default")
    assert config.agent is not None
    assert config.agent.marker is _agent_module._sentinel  # type: ignore[attr-defined]


def test_to_easyconfig_per_call_builds_fresh_config(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each conversion builds a fresh EasyConfig (no shared grouped sub-configs
    # across connections — the per-connection safety contract).
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    manifest = parse_manifest(
        {
            "voice": {
                "default": {"transport": "websocket", "stt": "openai/realtime", "tts": "openai"}
            }
        }
    )
    first = manifest.to_easyconfig("default", resolve_agent=False)
    second = manifest.to_easyconfig("default", resolve_agent=False)
    assert first is not second
    assert first.audio_processing is not second.audio_processing


def test_unknown_profile_raises_e602() -> None:
    manifest = parse_manifest({"voice": {"default": {"transport": "local"}}})
    with pytest.raises(EasyCatError) as exc_info:
        manifest.to_easyconfig("nope", resolve_agent=False)
    assert exc_info.value.code == "EASYCAT_E602"


# ── python: agent resolver errors ──────────────────────────────────────


def test_resolve_agent_bad_module_raises_e605() -> None:
    manifest = parse_manifest(
        {"voice": {"default": {"transport": "local", "agent": "python:no_such_mod:f"}}}
    )
    with pytest.raises(EasyCatError) as exc_info:
        manifest.resolve_agent("default")
    assert exc_info.value.code == "EASYCAT_E605"


def test_resolve_agent_missing_attribute_raises_e605(_agent_module: object) -> None:
    manifest = parse_manifest(
        {
            "voice": {
                "default": {
                    "transport": "local",
                    "agent": "python:_easycat_test_agent_mod:missing",
                }
            }
        }
    )
    with pytest.raises(EasyCatError) as exc_info:
        manifest.resolve_agent("default")
    assert exc_info.value.code == "EASYCAT_E605"


def test_resolve_agent_bad_grammar_raises_e605() -> None:
    manifest = parse_manifest(
        {"voice": {"default": {"transport": "local", "agent": "python:onlymodule"}}}
    )
    with pytest.raises(EasyCatError) as exc_info:
        manifest.resolve_agent("default")
    assert exc_info.value.code == "EASYCAT_E605"


def test_resolve_agent_non_python_grammar_raises_e605() -> None:
    manifest = parse_manifest(
        {"voice": {"default": {"transport": "local", "agent": "support_agent"}}}
    )
    with pytest.raises(EasyCatError) as exc_info:
        manifest.resolve_agent("default")
    assert exc_info.value.code == "EASYCAT_E605"


# ── round-trip from a real file ────────────────────────────────────────


def test_load_from_file_round_trip(tmp_path: object) -> None:
    from pathlib import Path

    path = Path(str(tmp_path)) / "easycat.toml"
    path.write_text(
        '[project]\nname = "rt"\n\n[voice.default]\ntransport = "local"\n',
        encoding="utf-8",
    )
    manifest = load_manifest(path)
    assert manifest.source_path == path.resolve()
    assert manifest.project.name == "rt"
