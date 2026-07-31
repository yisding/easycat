"""Contracts for staged audio-session assembly and rollback."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from easycat import EasyConfig, create_session, create_text_session
from easycat.config import TextSessionConfig, _factory
from tests.config._helpers import _DummyAgent, _stub_audio_backends


class _RollbackSTT:
    async def start_stream(self) -> None:
        pass

    async def send_audio(self, _chunk: object) -> None:
        pass

    async def commit_segment(self) -> None:
        pass

    async def end_stream(self) -> None:
        pass

    async def events(self):
        if False:
            yield None


class _RollbackTTS:
    async def synthesize(self, _text: str):
        if False:
            yield None

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass


class _ClosableVAD:
    def __init__(self) -> None:
        self.close_calls = 0

    def configure(self, **_kwargs: object) -> None:
        pass

    async def process(self, _chunk: object):
        if False:
            yield None

    def close(self) -> None:
        self.close_calls += 1


class _ClosableNoiseReducer:
    def __init__(self) -> None:
        self.close_calls = 0

    async def process(self, chunk: object) -> object:
        return chunk

    def close(self) -> None:
        self.close_calls += 1


class _ClosableEchoCanceller:
    def __init__(self) -> None:
        self.close_calls = 0

    async def process(self, chunk: object) -> object:
        return chunk

    def feed_reference(self, _chunk: object) -> None:
        pass

    def close(self) -> None:
        self.close_calls += 1


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


def test_session_build_forwards_journal_configuration(
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
            journal_redaction="pii",
        )
    )

    assert session._journal is journal
    assert session._run_ctx.journal_detail == "full"
    assert captured["capacity"] == 42_000
    assert captured["redaction"] == "pii"


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


def test_audio_pipeline_failure_closes_sync_noise_and_echo_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Later construction failures cannot leak already-created DSP resources."""
    vad = _ClosableVAD()
    noise_reducer = _ClosableNoiseReducer()
    echo_canceller = _ClosableEchoCanceller()

    def fail_transport(_config: object, _event_bus: object) -> object:
        raise RuntimeError("transport build failed")

    monkeypatch.setattr(_factory, "_create_transport", fail_transport)

    with pytest.raises(RuntimeError, match="transport build failed"):
        _factory._resolve_audio_pipeline(
            EasyConfig(
                stt=_RollbackSTT(),
                tts=_RollbackTTS(),
                vad=vad,
                noise_reduction=noise_reducer,
                echo_cancellation=echo_canceller,
                agent=_DummyAgent(),
            ),
            _factory.EventBus(),
        )

    assert vad.close_calls == 1
    assert noise_reducer.close_calls == 1
    assert echo_canceller.close_calls == 1


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
