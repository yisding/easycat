"""Contracts for staged audio-session assembly and rollback."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from easycat import EasyConfig, create_session, create_text_session
from easycat.config import TextSessionConfig, _factory
from tests.config._helpers import _DummyAgent, _stub_audio_backends


def _tracked_journal(monkeypatch: pytest.MonkeyPatch) -> Mock:
    journal = Mock()
    monkeypatch.setattr(_factory, "create_journal", lambda *_args, **_kwargs: journal)
    return journal


def test_successful_session_build_transfers_journal_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_audio_backends(monkeypatch)
    journal = _tracked_journal(monkeypatch)

    session = create_session(
        EasyConfig(openai_api_key="test-key", agent=_DummyAgent(), debug="light")
    )

    assert session._journal is journal
    assert session._run_ctx.journal_detail == "light"
    journal.close.assert_not_called()


def test_session_build_forwards_journal_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_audio_backends(monkeypatch)
    journal = Mock()
    captured: dict[str, object] = {}

    def create_tracked_journal(*_args: object, **kwargs: object) -> Mock:
        captured.update(kwargs)
        return journal

    monkeypatch.setattr(_factory, "create_journal", create_tracked_journal)

    session = create_session(
        EasyConfig(
            openai_api_key="test-key",
            agent=_DummyAgent(),
            debug="full",
            journal_capacity=42_000,
        )
    )

    assert session._journal is journal
    assert session._run_ctx.journal_detail == "full"
    assert captured["capacity"] == 42_000


def test_post_build_failure_rolls_back_acquired_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_audio_backends(monkeypatch)
    journal = _tracked_journal(monkeypatch)

    def fail_snapshot(_config: EasyConfig) -> object:
        raise RuntimeError("snapshot failed")

    monkeypatch.setattr(_factory, "_safe_config_ns", fail_snapshot)

    with pytest.raises(RuntimeError, match="snapshot failed"):
        create_session(EasyConfig(openai_api_key="test-key", agent=_DummyAgent(), debug="light"))

    journal.close.assert_called_once_with()


def test_build_failure_rolls_back_acquired_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _tracked_journal(monkeypatch)

    def fail_audio_pipeline(_config: EasyConfig, _event_bus: object) -> object:
        raise RuntimeError("audio build failed")

    monkeypatch.setattr(_factory, "_resolve_audio_pipeline", fail_audio_pipeline)

    with pytest.raises(RuntimeError, match="audio build failed"):
        create_session(EasyConfig(openai_api_key="test-key", agent=_DummyAgent(), debug="light"))

    journal.close.assert_called_once_with()


def test_text_build_failure_rolls_back_acquired_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _tracked_journal(monkeypatch)

    def fail_session(_config: object) -> object:
        raise RuntimeError("text build failed")

    monkeypatch.setattr(_factory, "Session", fail_session)

    with pytest.raises(RuntimeError, match="text build failed"):
        create_text_session(TextSessionConfig(agent=_DummyAgent(), debug="light"))

    journal.close.assert_called_once_with()
