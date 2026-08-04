"""Contract tests for centralized lifecycle-budget defaults."""

from inspect import signature

import pytest

from easycat.server.config import VoiceServerConfig
from easycat.server.webrtc_routes import (
    run_webrtc_config_server,
    serve_webrtc_config_sessions,
)
from easycat.teardown_budgets import (
    AGENT_POST_DONE_STREAM_DRAIN_TIMEOUT_S,
    JOURNAL_LIBSQL_SYNC_THREAD_JOIN_TIMEOUT_S,
    JOURNAL_LITESTREAM_KILL_TIMEOUT_S,
    JOURNAL_LITESTREAM_STDERR_JOIN_TIMEOUT_S,
    JOURNAL_LITESTREAM_TERMINATE_TIMEOUT_S,
    LLAMA_POST_CANCEL_AWAIT_TIMEOUT_S,
    REMOTE_RESPONSES_COMPLETED_STREAM_DRAIN_TIMEOUT_S,
    SERVER_DRAIN_TIMEOUT_S,
    SERVER_FORCE_SHUTDOWN_TIMEOUT_S,
    SESSION_APPLICATION_PROMPT_CANCEL_DRAIN_TIMEOUT_S,
    SESSION_AUDIO_DRAIN_TIMEOUT_S,
    SESSION_AUDIO_PLAYOUT_MARGIN_S,
    SESSION_BARGE_IN_CUTOFF_TIMEOUT_S,
    SESSION_FORCE_START_LOCK_TIMEOUT_S,
    SESSION_INLINE_SEND_CANCEL_GRACE_TIMEOUT_S,
    SESSION_INLINE_SEND_TIMEOUT_S,
    SESSION_SUPERSEDED_STOP_TIMEOUT_S,
    STANDALONE_WEBRTC_FORCE_SHUTDOWN_TIMEOUT_S,
    WEBRTC_AUDIO_ACLOSE_TIMEOUT_S,
    WEBRTC_OFFER_CANCEL_DRAIN_TIMEOUT_S,
)
from easycat.telephony.server import TwilioVoiceServerConfig
from easycat.telephony.twilio_app import TwilioAppSettings, twilio_app_settings_from_env
from easycat.transports.websocket import (
    WebSocketSessionServerConfig,
    websocket_session_server_config_from_env,
)
from easycat.transports.webtransport import WebTransportTransportConfig


def test_agent_lifecycle_budget_values_are_preserved() -> None:
    assert AGENT_POST_DONE_STREAM_DRAIN_TIMEOUT_S == 0.01
    assert LLAMA_POST_CANCEL_AWAIT_TIMEOUT_S == 2.0
    assert REMOTE_RESPONSES_COMPLETED_STREAM_DRAIN_TIMEOUT_S == 0.05


def test_session_lifecycle_budget_values_are_preserved() -> None:
    assert SESSION_AUDIO_DRAIN_TIMEOUT_S == 2.0
    assert SESSION_AUDIO_PLAYOUT_MARGIN_S == 0.5
    assert SESSION_INLINE_SEND_TIMEOUT_S == 0.5
    assert SESSION_INLINE_SEND_CANCEL_GRACE_TIMEOUT_S == 0.1
    assert SESSION_FORCE_START_LOCK_TIMEOUT_S == 0.5
    assert SESSION_SUPERSEDED_STOP_TIMEOUT_S == 0.5
    assert SESSION_BARGE_IN_CUTOFF_TIMEOUT_S == 0.4
    assert SESSION_APPLICATION_PROMPT_CANCEL_DRAIN_TIMEOUT_S == 0.1


def test_runtime_and_transport_lifecycle_budget_values_are_preserved() -> None:
    assert JOURNAL_LITESTREAM_TERMINATE_TIMEOUT_S == 5.0
    assert JOURNAL_LITESTREAM_KILL_TIMEOUT_S == 2.0
    assert JOURNAL_LITESTREAM_STDERR_JOIN_TIMEOUT_S == 2.0
    assert JOURNAL_LIBSQL_SYNC_THREAD_JOIN_TIMEOUT_S == 5.0
    assert WEBRTC_AUDIO_ACLOSE_TIMEOUT_S == 5.0
    assert WEBRTC_OFFER_CANCEL_DRAIN_TIMEOUT_S == 0.5


def test_configurable_server_lifecycle_budget_values_are_preserved() -> None:
    assert SERVER_DRAIN_TIMEOUT_S == 30.0
    assert SERVER_FORCE_SHUTDOWN_TIMEOUT_S == 10.0
    assert STANDALONE_WEBRTC_FORCE_SHUTDOWN_TIMEOUT_S == 5.0


def test_server_configurations_draw_unchanged_lifecycle_defaults_from_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configurations = (
        VoiceServerConfig(),
        TwilioVoiceServerConfig(),
        TwilioAppSettings(stream_url="wss://example.invalid/media"),
        twilio_app_settings_from_env(environ={"TWILIO_STREAM_URL": "wss://example.invalid/media"}),
        WebSocketSessionServerConfig(),
    )
    for config in configurations:
        assert config.drain_timeout_s == SERVER_DRAIN_TIMEOUT_S
        assert config.force_shutdown_timeout_s == SERVER_FORCE_SHUTDOWN_TIMEOUT_S

    env_prefix = "EASYCAT_TEST_TEARDOWN_BUDGET"
    for suffix in ("DRAIN_TIMEOUT_S", "FORCE_SHUTDOWN_TIMEOUT_S"):
        monkeypatch.delenv(f"{env_prefix}_{suffix}", raising=False)
    websocket_env_config = websocket_session_server_config_from_env(prefix=env_prefix)
    assert websocket_env_config.drain_timeout_s == SERVER_DRAIN_TIMEOUT_S
    assert websocket_env_config.force_shutdown_timeout_s == SERVER_FORCE_SHUTDOWN_TIMEOUT_S

    assert (
        WebTransportTransportConfig().force_shutdown_timeout_s == SERVER_FORCE_SHUTDOWN_TIMEOUT_S
    )
    for helper in (serve_webrtc_config_sessions, run_webrtc_config_server):
        parameters = signature(helper).parameters
        assert parameters["drain_timeout_s"].default == SERVER_DRAIN_TIMEOUT_S
        assert (
            parameters["force_shutdown_timeout_s"].default
            == STANDALONE_WEBRTC_FORCE_SHUTDOWN_TIMEOUT_S
        )
