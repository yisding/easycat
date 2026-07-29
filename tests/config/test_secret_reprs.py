"""Credential-bearing config fields must never appear in dataclass reprs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields
from typing import Any

import pytest

from easycat.config import EasyConfig, TextSessionConfig
from easycat.runtime.safe_defaults import _is_secret_name
from easycat.server.auth import BearerTokenAuth
from easycat.stt.factory import _CATALOG as _STT_CATALOG
from easycat.stt.factory import STTProviderConfig
from easycat.telephony.server import TwilioVoiceServerConfig
from easycat.telephony.twilio_app import TwilioAppSettings
from easycat.transports._webrtc_config import ICEServer, WebRTCTransportConfig
from easycat.transports.websocket import WebSocketSessionServerConfig
from easycat.tts.factory import _CATALOG as _TTS_CATALOG
from easycat.tts.factory import TTSProviderConfig

_SENTINEL = "easycat-repr-secret-sentinel"
_PROVIDER_CONFIGS = tuple(
    (catalog.kind, provider_name, spec.config_cls)
    for catalog in (_STT_CATALOG, _TTS_CATALOG)
    for provider_name, spec in catalog.specs.items()
)


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
