"""Tests for easycat.helpers convenience functions."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from easycat.event_logging import EventLoggingConfig
from easycat.events import AgentFinal, BotStoppedSpeaking, Interruption, STTFinal, TurnStarted
from easycat.helpers import (
    attach_runtime_feedback,
    default_event_logging,
    require_env,
)

# ── require_env ─────────────────────────────────────────────────


def test_require_env_returns_value(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEST_KEY_ABC", "hello")
    assert require_env("TEST_KEY_ABC") == "hello"


def test_require_env_raises_on_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("NONEXISTENT_KEY_XYZ", raising=False)
    with pytest.raises(SystemExit, match="NONEXISTENT_KEY_XYZ is required"):
        require_env("NONEXISTENT_KEY_XYZ")


# ── default_event_logging ───────────────────────────────────────


def test_default_event_logging_returns_config():
    cfg = default_event_logging()
    assert isinstance(cfg, EventLoggingConfig)
    assert cfg.enabled is True
    assert cfg.include_partials is False


# ── attach_runtime_feedback ─────────────────────────────────────


def test_attach_runtime_feedback_subscribes_events():
    session = MagicMock()
    attach_runtime_feedback(session)

    assert session.subscribe_event.call_count == 5

    subscribed_types = {c.args[0] for c in session.subscribe_event.call_args_list}
    assert subscribed_types == {
        TurnStarted,
        STTFinal,
        AgentFinal,
        BotStoppedSpeaking,
        Interruption,
    }
