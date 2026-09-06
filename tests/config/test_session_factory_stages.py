"""Contracts for staged audio-session assembly and rollback."""

from __future__ import annotations

from dataclasses import fields
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from easycat import EasyConfig, create_session, create_text_session
from easycat.config import TextSessionConfig, _factory
from easycat.noise_reduction import NoiseReducerConfig
from easycat.runtime.artifacts import InMemoryArtifactStore
from easycat.stt.deepgram_provider import DeepgramSTTConfig
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTTConfig
from easycat.tts.openai_tts import OpenAITTSConfig
from tests.config._helpers import (
    _DummyAgent,
    _IdentitySinkTransport,
    _stub_audio_backends,
)


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


class _TrackedArtifactStore(InMemoryArtifactStore):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0
        self.close_error: BaseException | None = None

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error
        super().close()


def _tracked_journal(monkeypatch: pytest.MonkeyPatch) -> Mock:
    journal = Mock()
    monkeypatch.setattr(_factory, "create_journal", lambda *_args, **_kwargs: journal)
    return journal


def _tracked_artifact_store(monkeypatch: pytest.MonkeyPatch) -> _TrackedArtifactStore:
    artifact_store = _TrackedArtifactStore()
    monkeypatch.setattr(
        _factory,
        "_create_artifact_store",
        lambda *_args, **_kwargs: artifact_store,
    )
    return artifact_store


def test_successful_session_build_transfers_journal_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_audio_backends(monkeypatch)
    artifact_store = _tracked_artifact_store(monkeypatch)
    journal = Mock()
    captured: dict[str, object] = {}

    def create_tracked_journal(*_args: object, **kwargs: object) -> Mock:
        captured.update(kwargs)
        return journal

    monkeypatch.setattr(_factory, "create_journal", create_tracked_journal)

    session = create_session(
        EasyConfig(openai_api_key="test-key", agent=_DummyAgent(), debug="light")
    )

    assert session._journal is journal
    assert session._artifact_store is artifact_store
    assert captured["artifact_store"] is artifact_store
    assert session._run_ctx.journal_detail == "light"
    journal.close.assert_not_called()
    assert artifact_store.close_calls == 0


def test_journal_creation_failure_closes_artifact_store_before_reraising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_store = _tracked_artifact_store(monkeypatch)

    def fail_journal(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("journal build failed")

    monkeypatch.setattr(_factory, "create_journal", fail_journal)
    retained_error: RuntimeError | None = None

    try:
        create_session(EasyConfig(openai_api_key="test-key", agent=_DummyAgent(), debug="light"))
    except RuntimeError as exc:
        retained_error = exc

    assert retained_error is not None
    assert str(retained_error) == "journal build failed"
    assert artifact_store.close_calls == 1


def test_journal_creation_failure_preserves_error_when_artifact_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_store = _tracked_artifact_store(monkeypatch)
    artifact_store.close_error = RuntimeError("artifact close failed")
    factory_error = RuntimeError("journal build failed")

    def fail_journal(*_args: object, **_kwargs: object) -> object:
        raise factory_error

    monkeypatch.setattr(_factory, "create_journal", fail_journal)

    with pytest.raises(RuntimeError, match="journal build failed") as caught:
        create_session(EasyConfig(openai_api_key="test-key", agent=_DummyAgent(), debug="light"))

    assert caught.value is factory_error
    assert artifact_store.close_calls == 1


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
    artifact_store = _tracked_artifact_store(monkeypatch)

    def fail_snapshot(_config: EasyConfig) -> object:
        raise RuntimeError("snapshot failed")

    monkeypatch.setattr(_factory, "_safe_config_ns", fail_snapshot)

    with pytest.raises(RuntimeError, match="snapshot failed"):
        create_session(EasyConfig(openai_api_key="test-key", agent=_DummyAgent(), debug="light"))

    journal.close.assert_called_once_with()
    assert artifact_store.close_calls == 1


def test_build_failure_rolls_back_acquired_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _tracked_journal(monkeypatch)
    artifact_store = _tracked_artifact_store(monkeypatch)
    artifact_store.close_error = RuntimeError("artifact close failed")
    factory_error = RuntimeError("audio build failed")

    def fail_audio_pipeline(_config: EasyConfig, _event_bus: object) -> object:
        raise factory_error

    monkeypatch.setattr(_factory, "_resolve_audio_pipeline", fail_audio_pipeline)

    with pytest.raises(RuntimeError, match="audio build failed") as caught:
        create_session(EasyConfig(openai_api_key="test-key", agent=_DummyAgent(), debug="light"))

    assert caught.value is factory_error
    journal.close.assert_called_once_with()
    assert artifact_store.close_calls == 1


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
    artifact_store = _tracked_artifact_store(monkeypatch)
    artifact_store.close_error = RuntimeError("artifact close failed")
    factory_error = RuntimeError("text build failed")

    def fail_session(_config: object) -> object:
        raise factory_error

    monkeypatch.setattr(_factory, "Session", fail_session)

    with pytest.raises(RuntimeError, match="text build failed") as caught:
        create_text_session(TextSessionConfig(agent=_DummyAgent(), debug="light"))

    assert caught.value is factory_error
    journal.close.assert_called_once_with()
    assert artifact_store.close_calls == 1


# ── decide-then-construct split ────────────────────────────────────────────

_LEAF_CONSTRUCTORS = (
    "create_stt_provider",
    "create_stt_provider_from_config",
    "create_tts_provider",
    "create_tts_provider_from_config",
    "create_vad",
    "create_noise_reducer",
    "create_echo_canceller",
    "_create_transport",
)


def _forbid_leaf_constructors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every provider constructor `_factory` resolves explode when called."""

    def _explode(name: str):
        def _fail(*_args: object, **_kwargs: object) -> object:
            raise AssertionError(f"{name} must not run while deciding the pipeline")

        return _fail

    for name in _LEAF_CONSTRUCTORS:
        monkeypatch.setattr(_factory, name, _explode(name))


def _decision_config() -> EasyConfig:
    return EasyConfig(
        stt=OpenAIRealtimeSTTConfig(api_key="test-key"),
        tts=OpenAITTSConfig(api_key="test-key"),
        agent=_DummyAgent(),
        enable_noise_reduction=True,
    )


def _field_snapshot(config: EasyConfig) -> dict[str, tuple[int, object]]:
    return {f.name: (id(getattr(config, f.name)), getattr(config, f.name)) for f in fields(config)}


def test_decide_audio_pipeline_allocates_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deciding the pipeline must not reach a single provider constructor."""
    _forbid_leaf_constructors(monkeypatch)

    decisions = _factory._decide_audio_pipeline(_decision_config())

    assert isinstance(decisions, _factory._AudioDecisions)
    assert decisions.enable_vad is True
    assert decisions.enable_noise_reduction is True
    assert isinstance(decisions.noise_spec, NoiseReducerConfig)


@pytest.mark.parametrize(
    "case",
    ["vad_enabled", "vad_skipped", "defaulted_noise_reducer"],
    ids=["vad_enabled", "vad_skipped", "defaulted_noise_reducer"],
)
def test_decide_audio_pipeline_does_not_mutate_the_config(case: str) -> None:
    """The decision step reads the config; it never writes to it."""
    if case == "vad_skipped":
        config = EasyConfig(
            stt=DeepgramSTTConfig(api_key="test-key", model="flux-general-en"),
            tts=OpenAITTSConfig(api_key="test-key"),
            agent=_DummyAgent(),
        )
    elif case == "defaulted_noise_reducer":
        config = _decision_config()
        assert config.noise_reduction is None
    else:
        config = EasyConfig(
            stt=OpenAIRealtimeSTTConfig(api_key="test-key"),
            tts=OpenAITTSConfig(api_key="test-key"),
            agent=_DummyAgent(),
        )

    before = _field_snapshot(config)
    decisions = _factory._decide_audio_pipeline(config)
    after = _field_snapshot(config)

    assert after == before
    assert decisions.enable_vad is (case != "vad_skipped")
    if case == "vad_skipped":
        assert decisions.vad_spec is None


def test_decide_audio_pipeline_keeps_caller_object_identity() -> None:
    """Injected providers reach the decisions by reference, never copied."""
    stt = _RollbackSTT()
    tts = _RollbackTTS()
    vad = _ClosableVAD()
    noise_reducer = _ClosableNoiseReducer()
    echo_canceller = _ClosableEchoCanceller()
    transport = _IdentitySinkTransport()
    config = EasyConfig(
        stt=stt,
        tts=tts,
        vad=vad,
        noise_reduction=noise_reducer,
        echo_cancellation=echo_canceller,
        transport=transport,
        agent=_DummyAgent(),
    )

    decisions = _factory._decide_audio_pipeline(config)

    assert decisions.stt_spec is stt
    assert decisions.tts_spec is tts
    assert decisions.vad_spec is vad
    assert decisions.noise_spec is noise_reducer
    assert decisions.echo_spec is echo_canceller
    assert decisions.transport_spec is transport
    # D4, preserved: an injected canceller still reports the flag as False.
    assert decisions.enable_echo_cancellation is False


def test_decide_audio_pipeline_reads_the_monkeypatched_auto_turn_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The policy is reached through the module global tests already patch."""
    monkeypatch.setattr(_factory, "_should_auto_turn_from_stt_final", lambda _config: True)

    decisions = _factory._decide_audio_pipeline(_decision_config())

    assert decisions.auto_turn_from_stt_final is True
    assert decisions.enable_vad is False
    assert decisions.vad_spec is None


def test_construct_audio_pipeline_rolls_back_when_the_transport_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rollback still closes earlier DSP resources when construction is split."""
    vad = _ClosableVAD()
    noise_reducer = _ClosableNoiseReducer()
    echo_canceller = _ClosableEchoCanceller()

    def fail_transport(_config: object, _event_bus: object) -> object:
        raise RuntimeError("transport build failed")

    monkeypatch.setattr(_factory, "_create_transport", fail_transport)

    decisions = _factory._decide_audio_pipeline(
        EasyConfig(
            stt=_RollbackSTT(),
            tts=_RollbackTTS(),
            vad=vad,
            noise_reduction=noise_reducer,
            echo_cancellation=echo_canceller,
            agent=_DummyAgent(),
        )
    )

    with pytest.raises(RuntimeError, match="transport build failed"):
        _factory._construct_audio_pipeline(decisions, _factory.EventBus())

    assert vad.close_calls == 1
    assert noise_reducer.close_calls == 1
    assert echo_canceller.close_calls == 1


def test_session_keeps_no_reference_to_the_decision_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decision result stays on create_session's call stack."""
    _stub_audio_backends(monkeypatch)

    session = create_session(
        EasyConfig(openai_api_key="test-key", agent=_DummyAgent(), debug="off")
    )

    assert not any(isinstance(value, _factory._AudioDecisions) for value in vars(session).values())
    assert isinstance(session._easycat_config, SimpleNamespace)
