"""Environment parsing for :mod:`easycat.telephony.telnyx_app`."""

from __future__ import annotations

import pytest

from easycat.teardown_budgets import SERVER_DRAIN_TIMEOUT_S, SERVER_FORCE_SHUTDOWN_TIMEOUT_S
from easycat.telephony.telnyx_app import TelnyxAppSettings, telnyx_app_settings_from_env


@pytest.fixture(autouse=True)
def _clear_telnyx_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "TELNYX_STREAM_URL",
        "TELNYX_API_KEY",
        "TELNYX_PUBLIC_KEY",
        "TELNYX_CONNECTION_ID",
        "TELNYX_STREAM_TOKEN_SECRET",
        "TELNYX_WS_PORT",
        "TELNYX_MAX_SESSIONS",
        "TELNYX_START_TIMEOUT_S",
        "TELNYX_DRAIN_TIMEOUT_S",
        "TELNYX_FORCE_SHUTDOWN_TIMEOUT_S",
    ):
        monkeypatch.delenv(name, raising=False)


BASE_ENV = {
    "TELNYX_API_KEY": "api-key-value",
    "TELNYX_STREAM_URL": "wss://voice.example.net/media",
    "TELNYX_PUBLIC_KEY": "public-key-value",
    "TELNYX_CONNECTION_ID": "conn-1",
}


def test_full_env_parses_every_setting() -> None:
    env = {
        **BASE_ENV,
        "TELNYX_WS_PORT": "9001",
        "TELNYX_STREAM_TOKEN_SECRET": "token-secret-value",
        "TELNYX_MAX_SESSIONS": "8",
        "TELNYX_START_TIMEOUT_S": "5",
        "TELNYX_DRAIN_TIMEOUT_S": "20",
        "TELNYX_FORCE_SHUTDOWN_TIMEOUT_S": "7",
    }

    settings = telnyx_app_settings_from_env(environ=env)

    assert isinstance(settings, TelnyxAppSettings)
    assert settings.stream_url == "wss://voice.example.net/media"
    assert settings.api_key == "api-key-value"
    assert settings.public_key == "public-key-value"
    assert settings.connection_id == "conn-1"
    assert settings.ws_port == 9001
    assert settings.stream_token_secret == "token-secret-value"
    assert settings.max_sessions == 8
    assert settings.start_timeout_s == 5.0
    assert settings.drain_timeout_s == 20.0
    assert settings.force_shutdown_timeout_s == 7.0


def test_defaults_apply_without_optional_env() -> None:
    settings = telnyx_app_settings_from_env(
        stream_url="wss://example/media",
        environ={"TELNYX_STREAM_URL": "wss://ignored", "TELNYX_API_KEY": "   "},
    )

    assert settings.stream_url == "wss://example/media"
    assert settings.api_key == ""
    assert settings.public_key == ""
    assert settings.connection_id == ""
    assert settings.ws_port == 8766
    assert settings.max_sessions == 64
    assert settings.start_timeout_s == 10.0
    assert settings.drain_timeout_s == SERVER_DRAIN_TIMEOUT_S
    assert settings.force_shutdown_timeout_s == SERVER_FORCE_SHUTDOWN_TIMEOUT_S


def test_stream_url_kwarg_overrides_env() -> None:
    settings = telnyx_app_settings_from_env(
        stream_url="wss://override/media", environ={**BASE_ENV, "TELNYX_STREAM_URL": "wss://env/x"}
    )

    assert settings.stream_url == "wss://override/media"


def test_missing_stream_url_raises() -> None:
    with pytest.raises(RuntimeError) as exc:
        telnyx_app_settings_from_env(environ=BASE_ENV | {"TELNYX_STREAM_URL": ""})
    assert "TELNYX_STREAM_URL" in str(exc.value)


def test_blank_and_whitespace_values_normalize_to_empty() -> None:
    env = {
        "TELNYX_STREAM_URL": "   wss://example/media  ",
        "TELNYX_API_KEY": "   ",
        "TELNYX_CONNECTION_ID": "",
    }

    settings = telnyx_app_settings_from_env(environ=env)

    assert settings.stream_url == "wss://example/media"
    assert settings.api_key == ""
    assert settings.connection_id == ""


@pytest.mark.parametrize("bad_url", ["https://example/media", "ws://example/media", "example"])
def test_non_wss_stream_url_is_rejected(bad_url: str) -> None:
    with pytest.raises(RuntimeError) as exc:
        telnyx_app_settings_from_env(environ={"TELNYX_STREAM_URL": bad_url})
    assert "must use wss://" in str(exc.value)


def test_uppercase_wss_prefix_is_accepted() -> None:
    settings = telnyx_app_settings_from_env(environ={"TELNYX_STREAM_URL": "WSS://Example/Media"})

    assert settings.stream_url == "WSS://Example/Media"


@pytest.mark.parametrize("raw", ["0", "-3", "abc", "2.5"])
def test_invalid_max_sessions_raises(raw: str) -> None:
    with pytest.raises(RuntimeError, match="TELNYX_MAX_SESSIONS"):
        telnyx_app_settings_from_env(
            environ={**BASE_ENV, "TELNYX_MAX_SESSIONS": raw},
        )


@pytest.mark.parametrize("raw", ["0", "-1", "abc"])
def test_invalid_ws_port_raises(raw: str) -> None:
    with pytest.raises(RuntimeError, match="TELNYX_WS_PORT"):
        telnyx_app_settings_from_env(environ={**BASE_ENV, "TELNYX_WS_PORT": raw})


def test_port_boundary_values_are_accepted() -> None:
    low = telnyx_app_settings_from_env(environ={**BASE_ENV, "TELNYX_WS_PORT": "1"})
    high = telnyx_app_settings_from_env(environ={**BASE_ENV, "TELNYX_WS_PORT": "65535"})

    assert (low.ws_port, high.ws_port) == (1, 65535)


@pytest.mark.parametrize("raw", ["nan", "inf", "-1", "abc"])
def test_invalid_start_timeout_raises(raw: str) -> None:
    with pytest.raises(RuntimeError, match="TELNYX_START_TIMEOUT_S"):
        telnyx_app_settings_from_env(environ={**BASE_ENV, "TELNYX_START_TIMEOUT_S": raw})


@pytest.mark.parametrize("name", ["TELNYX_DRAIN_TIMEOUT_S", "TELNYX_FORCE_SHUTDOWN_TIMEOUT_S"])
@pytest.mark.parametrize("raw", ["nan", "inf", "-0.5", "abc"])
def test_invalid_drain_and_force_timeouts_raise(name: str, raw: str) -> None:
    with pytest.raises(RuntimeError, match=name):
        telnyx_app_settings_from_env(environ={**BASE_ENV, name: raw})


def test_actions_enabled_only_with_api_key() -> None:
    configured = TelnyxAppSettings(stream_url="wss://s", api_key="k")
    unconfigured = TelnyxAppSettings(stream_url="wss://s")

    assert configured.telnyx_session_actions() is not None
    assert configured.telnyx_actions_enabled is True
    assert unconfigured.telnyx_session_actions() is None


def test_session_actions_carry_api_key_and_connection() -> None:
    from easycat.telephony.session_actions import TelnyxSessionActionConfig

    actions = TelnyxAppSettings(
        stream_url="wss://s", api_key="k", connection_id="conn-9"
    ).telnyx_session_actions()

    assert isinstance(actions, TelnyxSessionActionConfig)
    assert actions.api_key == "k"
    assert actions.connection_id == "conn-9"


def test_repr_omits_secrets_but_keeps_public_fields() -> None:
    settings = TelnyxAppSettings(
        stream_url="wss://s",
        api_key="secret-api-key",
        public_key="portal-public-key",
        stream_token_secret="secret-token-key",
    )
    rendered = repr(settings)

    assert "secret-api-key" not in rendered
    assert "secret-token-key" not in rendered
    # The Ed25519 public key is not a credential; it stays visible.
    assert "portal-public-key" in rendered
