"""Credential-bearing config fields must never appear in dataclass reprs."""

from __future__ import annotations

import ast
import importlib
from collections.abc import Callable
from dataclasses import fields, is_dataclass
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from easycat._public_api import LAZY_EXPORTS, PUBLIC_CONFIG_EXPORTS
from easycat.config import (
    EasyConfig,
    OutboundCallConfig,
    TelephonyConfig,
    TextSessionConfig,
    VoicemailDetectionConfig,
)
from easycat.noise_reduction import NoiseReducerConfig
from easycat.runtime.safe_defaults import _is_secret_name
from easycat.server.auth import BearerTokenAuth
from easycat.session._types import SessionConfig
from easycat.smart_turn import SmartTurnConfig
from easycat.stt.factory import _CATALOG as _STT_CATALOG
from easycat.stt.factory import STTProviderConfig
from easycat.telephony.server import TwilioVoiceServerConfig
from easycat.telephony.session_actions import TelnyxSessionActionConfig, TwilioSessionActionConfig
from easycat.telephony.twilio_app import TwilioAppSettings
from easycat.transports._webrtc_config import ICEServer, WebRTCTransportConfig
from easycat.transports.local import LocalTransportConfig
from easycat.transports.websocket import WebSocketSessionServerConfig, WebSocketTransportConfig
from easycat.transports.webtransport import WebTransportTransportConfig
from easycat.tts.factory import _CATALOG as _TTS_CATALOG
from easycat.tts.factory import TTSProviderConfig
from easycat.turn_manager import TurnManagerConfig
from easycat.vad import VADConfig

_SENTINEL = "easycat-repr-secret-sentinel"
_SOURCE_ROOT = Path(__file__).parents[2] / "src" / "easycat"
_PROVIDER_CONFIGS = tuple(
    (catalog.kind, provider_name, spec.config_cls)
    for catalog in (_STT_CATALOG, _TTS_CATALOG)
    for provider_name, spec in catalog.specs.items()
)

# This is an exact source inventory, not a best-effort runtime import walk.
# Optional provider SDKs therefore cannot make the guard skip a config module.
_SECRET_FIELD_INVENTORY = frozenset(
    {
        "config/easy.py:EasyConfig.openai_api_key",
        "config/easy.py:OutboundCallConfig.telnyx_api_key",
        "config/easy.py:OutboundCallConfig.twilio_auth_token",
        "config/easy.py:_AgentSessionConfig.remote_agent_api_key",
        "server/auth.py:BearerTokenAuth.token",
        "stt/cartesia_provider.py:CartesiaSTTConfig.api_key",
        "stt/deepgram_provider.py:DeepgramSTTConfig.api_key",
        "stt/elevenlabs_provider.py:ElevenLabsSTTConfig.api_key",
        "stt/factory.py:STTProviderConfig.api_key",
        "stt/openai_provider.py:OpenAISTTConfig.api_key",
        "stt/openai_realtime_provider.py:OpenAIRealtimeSTTConfig.api_key",
        "telephony/server.py:TwilioVoiceServerConfig.stream_token_secret",
        "telephony/server.py:TwilioVoiceServerConfig.twilio_auth_token",
        "telephony/session_actions.py:TelnyxSessionActionConfig.api_key",
        "telephony/session_actions.py:TwilioSessionActionConfig.auth_token",
        "telephony/twilio_app.py:TwilioAppSettings.auth_token",
        "telephony/twilio_app.py:TwilioAppSettings.call_api_token",
        "telephony/twilio_app.py:TwilioAppSettings.stream_token_secret",
        "transports/_webrtc_config.py:ICEServer.credential",
        "transports/_webrtc_config.py:WebRTCTransportConfig.auth_token",
        "transports/websocket.py:WebSocketSessionServerConfig.auth_token",
        "transports/webtransport.py:WebTransportTransportConfig.auth_token",
        "tts/cartesia_tts.py:CartesiaTTSConfig.api_key",
        "tts/deepgram_tts.py:DeepgramTTSConfig.api_key",
        "tts/elevenlabs_tts.py:ElevenLabsTTSConfig.api_key",
        "tts/factory.py:TTSProviderConfig.api_key",
        "tts/openai_tts.py:OpenAITTSConfig.api_key",
    }
)


def _public_config_factories() -> dict[str, Callable[[], Any]]:
    """Runtime sentinels for the explicitly classified top-level config surface."""
    return {
        "EasyConfig": lambda: EasyConfig(
            openai_api_key=_SENTINEL,
            remote_agent_api_key=_SENTINEL,
        ),
        "ICEServer": lambda: ICEServer(
            urls="turn:example.test",
            credential=_SENTINEL,
        ),
        "LocalTransportConfig": LocalTransportConfig,
        "NoiseReducerConfig": NoiseReducerConfig,
        "OutboundCallConfig": lambda: OutboundCallConfig(
            twilio_auth_token=_SENTINEL,
        ),
        "STTProviderConfig": lambda: STTProviderConfig(
            provider="openai",
            api_key=_SENTINEL,
        ),
        "SessionConfig": lambda: SessionConfig(runtime_mode="text_session"),
        "SmartTurnConfig": SmartTurnConfig,
        "TTSProviderConfig": lambda: TTSProviderConfig(
            provider="openai",
            api_key=_SENTINEL,
        ),
        "TelephonyConfig": lambda: TelephonyConfig(
            outbound=OutboundCallConfig(
                twilio_auth_token=_SENTINEL,
            ),
            twilio_actions=TwilioSessionActionConfig(
                auth_token=_SENTINEL,
            ),
        ),
        "TurnManagerConfig": TurnManagerConfig,
        "TwilioSessionActionConfig": lambda: TwilioSessionActionConfig(
            auth_token=_SENTINEL,
        ),
        "VADConfig": VADConfig,
        "VoicemailDetectionConfig": VoicemailDetectionConfig,
        "WebRTCTransportConfig": lambda: WebRTCTransportConfig(
            auth_token=_SENTINEL,
            ice_servers=[
                ICEServer(urls="turn:example.test", credential=_SENTINEL),
            ],
        ),
        "WebSocketTransportConfig": WebSocketTransportConfig,
        "WebTransportTransportConfig": lambda: WebTransportTransportConfig(
            auth_token=_SENTINEL,
        ),
    }


def _is_dataclass_decorator(node: ast.expr) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    return (isinstance(target, ast.Name) and target.id == "dataclass") or (
        isinstance(target, ast.Attribute) and target.attr == "dataclass"
    )


def _is_config_dataclass(node: ast.ClassDef) -> bool:
    config_name = node.name.lstrip("_")
    return any(_is_dataclass_decorator(item) for item in node.decorator_list) and (
        config_name.endswith(("Config", "Settings", "Auth")) or config_name == "ICEServer"
    )


def _is_direct_secret_field_name(name: str) -> bool:
    """Identify raw credential values, excluding boolean policy/metadata fields."""
    if name.startswith(("allow_", "expose_", "unsafe_")):
        return False
    return name in {"api_key", "credential", "password", "secret", "token"} or name.endswith(
        ("_api_key", "_credential", "_password", "_secret", "_token")
    )


def _field_sets_repr_false(value: ast.expr | None) -> bool:
    if not isinstance(value, ast.Call):
        return False
    target = value.func
    if not (
        (isinstance(target, ast.Name) and target.id == "field")
        or (isinstance(target, ast.Attribute) and target.attr == "field")
    ):
        return False
    return any(
        keyword.arg == "repr"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is False
        for keyword in value.keywords
    )


@cache
def _source_dataclass_inventory() -> tuple[frozenset[str], dict[str, bool]]:
    config_class_names: set[str] = set()
    discovered: dict[str, bool] = {}
    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(_SOURCE_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or not _is_config_dataclass(node):
                continue
            config_class_names.add(node.name)
            for item in node.body:
                if not isinstance(item, ast.AnnAssign) or not isinstance(item.target, ast.Name):
                    continue
                if _is_direct_secret_field_name(item.target.id):
                    identity = f"{relative}:{node.name}.{item.target.id}"
                    discovered[identity] = _field_sets_repr_false(item.value)
    return frozenset(config_class_names), discovered


def _source_secret_fields() -> dict[str, bool]:
    return _source_dataclass_inventory()[1]


def test_public_config_registry_matches_top_level_exports() -> None:
    factories = _public_config_factories()
    source_config_names, _secret_fields = _source_dataclass_inventory()
    exported_config_dataclasses = set(LAZY_EXPORTS).intersection(source_config_names)

    assert set(PUBLIC_CONFIG_EXPORTS) == exported_config_dataclasses
    assert set(factories) == set(PUBLIC_CONFIG_EXPORTS)

    for name, factory in factories.items():
        module_name, attribute_name = LAZY_EXPORTS[name]
        config_cls = getattr(importlib.import_module(module_name), attribute_name)
        assert is_dataclass(config_cls), name
        assert type(factory()) is config_cls, name


@pytest.mark.parametrize(
    ("config_name", "factory"),
    _public_config_factories().items(),
)
def test_public_config_repr_omits_secret_sentinel(
    config_name: str,
    factory: Callable[[], Any],
) -> None:
    instance = factory()

    for dataclass_field in fields(instance):
        if _is_direct_secret_field_name(dataclass_field.name):
            assert dataclass_field.repr is False, f"{config_name}.{dataclass_field.name}"
    assert _SENTINEL not in repr(instance), config_name


def test_source_secret_field_inventory_is_complete_and_repr_safe() -> None:
    discovered = _source_secret_fields()

    assert set(discovered) == set(_SECRET_FIELD_INVENTORY)
    assert {name for name, repr_safe in discovered.items() if not repr_safe} == set()


@pytest.mark.parametrize(
    ("provider_kind", "provider_name", "config_cls"),
    _PROVIDER_CONFIGS,
)
def test_registered_provider_config_repr_omits_api_key(
    provider_kind: str,
    provider_name: str,
    config_cls: type,
) -> None:
    """Walk the catalogs so every present and future built-in gets this guard."""
    api_key_field = next(field for field in fields(config_cls) if field.name == "api_key")

    assert _is_secret_name(api_key_field.name), (
        f"{provider_kind} provider {provider_name!r} credential field escaped "
        "the shared secret-name policy"
    )
    assert api_key_field.repr is False
    assert _SENTINEL not in repr(config_cls(api_key=_SENTINEL))


@pytest.mark.parametrize(
    ("config_cls", "secret_field_names", "factory"),
    [
        (
            EasyConfig,
            ("openai_api_key", "remote_agent_api_key"),
            lambda: EasyConfig(
                openai_api_key=_SENTINEL,
                remote_agent_api_key=_SENTINEL,
            ),
        ),
        (
            TextSessionConfig,
            ("remote_agent_api_key",),
            lambda: TextSessionConfig(remote_agent_api_key=_SENTINEL),
        ),
        (
            STTProviderConfig,
            ("api_key",),
            lambda: STTProviderConfig(provider="openai", api_key=_SENTINEL),
        ),
        (
            TTSProviderConfig,
            ("api_key",),
            lambda: TTSProviderConfig(provider="openai", api_key=_SENTINEL),
        ),
        (
            BearerTokenAuth,
            ("token",),
            lambda: BearerTokenAuth(token=_SENTINEL),
        ),
        (
            ICEServer,
            ("credential",),
            lambda: ICEServer(urls="turn:example.test", credential=_SENTINEL),
        ),
        (
            WebRTCTransportConfig,
            ("auth_token",),
            lambda: WebRTCTransportConfig(auth_token=_SENTINEL),
        ),
        (
            WebSocketSessionServerConfig,
            ("auth_token",),
            lambda: WebSocketSessionServerConfig(auth_token=_SENTINEL),
        ),
        (
            OutboundCallConfig,
            ("telnyx_api_key", "twilio_auth_token"),
            lambda: OutboundCallConfig(
                telnyx_api_key=_SENTINEL,
                twilio_auth_token=_SENTINEL,
            ),
        ),
        (
            TelnyxSessionActionConfig,
            ("api_key",),
            lambda: TelnyxSessionActionConfig(api_key=_SENTINEL),
        ),
        (
            TwilioVoiceServerConfig,
            ("stream_token_secret", "twilio_auth_token"),
            lambda: TwilioVoiceServerConfig(
                stream_token_secret=_SENTINEL,
                twilio_auth_token=_SENTINEL,
            ),
        ),
        (
            TwilioAppSettings,
            ("auth_token", "call_api_token", "stream_token_secret"),
            lambda: TwilioAppSettings(
                stream_url="wss://example.test/media",
                auth_token=_SENTINEL,
                call_api_token=_SENTINEL,
                stream_token_secret=_SENTINEL,
            ),
        ),
    ],
)
def test_credential_config_repr_omits_secret_fields(
    config_cls: type,
    secret_field_names: tuple[str, ...],
    factory: Callable[[], Any],
) -> None:
    fields_by_name = {field.name: field for field in fields(config_cls)}

    for field_name in secret_field_names:
        assert _is_secret_name(field_name)
        assert fields_by_name[field_name].repr is False
    assert _SENTINEL not in repr(factory())


@pytest.mark.parametrize("config_cls", [STTProviderConfig, TTSProviderConfig])
def test_named_provider_config_repr_redacts_nested_param_credentials(config_cls: type) -> None:
    config = config_cls(
        provider="custom",
        params={
            "api_key": _SENTINEL,
            "nested": {"authorization": _SENTINEL},
            "model": "safe-model",
        },
    )

    rendered = repr(config)
    assert _SENTINEL not in rendered
    assert "safe-model" in rendered
    assert rendered.count("***") == 2
