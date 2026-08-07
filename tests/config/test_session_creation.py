from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import replace

import pytest

from easycat import (
    CallInitiated,
    EasyConfig,
    EventBus,
    Session,
    SessionConfig,
    STTProviderConfig,
    TTSProviderConfig,
    TurnMode,
    create_session,
)
from easycat.config import EasyConfigError, OutboundCallConfig, TelephonyConfig
from easycat.config import _factory as config_factory
from easycat.runtime.artifacts import ArtifactWriteReceipt
from easycat.session._types import CallIdentity
from easycat.stages.base import put_artifact_async
from easycat.stt.deepgram_provider import DeepgramSTTConfig
from easycat.stt.openai_provider import OpenAISTT
from easycat.telephony.retry import RetryStrategyConfig
from easycat.timeouts import TimeoutConfig
from easycat.transports.twilio_media import TwilioConnectionTransport
from easycat.tts.input import TTSInputPolicy
from easycat.tts.openai_tts import OpenAITTS, OpenAITTSConfig
from tests.config._helpers import (
    _DummyAgent,
    _DummyWebSocket,
    _IdentitySinkTransport,
)


class _ProviderShapeSTT:
    async def start_stream(self):
        pass

    async def send_audio(self, chunk):
        pass

    async def commit_segment(self) -> bool:
        return False

    async def end_stream(self):
        pass

    async def events(self):
        if False:
            yield None


class _ProviderShapeTTS:
    async def synthesize(self, payload):
        if False:
            yield None

    async def stop(self):
        pass

    async def cancel(self):
        pass


class _ProviderShapeVAD:
    def configure(self, **kwargs):
        pass

    async def process(self, chunk):
        if False:
            yield None


class _BlockingArtifactStore:
    writes_block = True

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.refs: set[str] = set()
        self._cleanup_token: str | None = None
        self._lock = threading.Lock()

    def put(self, payload: bytes, *, artifact_class: str = "debug_verbose") -> str:
        return self.put_with_cleanup_token(payload, artifact_class=artifact_class).ref

    def put_with_cleanup_token(
        self,
        payload: bytes,
        *,
        artifact_class: str = "debug_verbose",
    ) -> ArtifactWriteReceipt:
        del payload, artifact_class
        self.started.set()
        if not self.release.wait(timeout=5):
            raise AssertionError("timed out waiting to release artifact put")
        with self._lock:
            created = "blocked-ref" not in self.refs
            self.refs.add("blocked-ref")
            self._cleanup_token = uuid.uuid4().hex
            return ArtifactWriteReceipt(
                "blocked-ref",
                created=created,
                cleanup_token=self._cleanup_token,
            )

    def delete(self, ref: str) -> None:
        with self._lock:
            self.refs.discard(ref)
            self._cleanup_token = None

    def delete_if_cleanup_token(self, ref: str, cleanup_token: str) -> bool:
        with self._lock:
            if cleanup_token != self._cleanup_token:
                return False
            self.refs.discard(ref)
            self._cleanup_token = None
            return True


@pytest.mark.asyncio
async def test_outbound_identity_subscription_is_session_scoped_on_shared_bus() -> None:
    bus = EventBus(handler_error_policy="raise")
    victim = Session(
        SessionConfig(runtime_mode="text_session", event_bus=bus, session_id="victim-session")
    )
    other = Session(
        SessionConfig(runtime_mode="text_session", event_bus=bus, session_id="other-session")
    )
    config_factory._subscribe_outbound_identity(victim)
    config_factory._subscribe_outbound_identity(other)

    try:
        await bus.emit(
            CallInitiated(
                session_id=victim.session_id,
                call_sid="CA-victim",
                to="+15551112222",
                from_="+15559876543",
            )
        )

        assert victim.call_identity is not None
        assert victim.call_identity.call_sid == "CA-victim"
        assert other.call_identity is None
    finally:
        await victim.stop(force=True)
        await other.stop(force=True)


@pytest.mark.asyncio
async def test_session_stop_releases_outbound_identity_subscription_only() -> None:
    bus = EventBus(handler_error_policy="raise")
    observed: list[CallInitiated] = []
    external = bus.subscribe(CallInitiated, observed.append)
    session = Session(
        SessionConfig(runtime_mode="text_session", event_bus=bus, session_id="stopped-session")
    )
    config_factory._subscribe_outbound_identity(session)

    await session.stop(force=True)

    assert external.active is True
    assert bus.subscribers(CallInitiated) == [observed.append]
    event = CallInitiated(
        session_id="another-session",
        call_sid="CA-other",
        to="+15552223333",
        from_="+15559876543",
    )
    await bus.emit(event)
    assert observed == [event]
    assert session.call_identity is None


def test_create_session_copies_mutable_config_for_each_runtime() -> None:
    config = EasyConfig(
        stt=_ProviderShapeSTT(),
        tts=_ProviderShapeTTS(),
        vad=_ProviderShapeVAD(),
        transport=_IdentitySinkTransport(),
        agent=_DummyAgent(),
        debug="off",
    )
    config.turn_taking.mode = "push_to_talk"  # type: ignore[assignment]

    first = create_session(config)
    second = create_session(config)

    assert config.turn_taking.mode == "push_to_talk"
    assert first._turn_manager.mode is TurnMode.PUSH_TO_TALK
    assert second._turn_manager.mode is TurnMode.PUSH_TO_TALK
    assert first._turn_manager is not second._turn_manager
    assert first._turn_manager._config is not config.turn_taking
    assert second._turn_manager._config is not first._turn_manager._config


@pytest.mark.asyncio
async def test_session_audio_capture_policy_and_runtime_override():
    consent = {"enabled": False}
    session = create_session(
        EasyConfig(
            stt=_ProviderShapeSTT(),
            tts=_ProviderShapeTTS(),
            vad=_ProviderShapeVAD(),
            transport=_IdentitySinkTransport(),
            agent=_DummyAgent(),
            debug="light",
            capture_audio=lambda: consent["enabled"],
        )
    )

    assert await put_artifact_async(session._run_ctx, b"before-consent") is None
    consent["enabled"] = True
    captured_ref = await put_artifact_async(session._run_ctx, b"after-consent")
    assert captured_ref is not None
    assert session._artifact_store.get(captured_ref) == b"after-consent"

    session.set_audio_capture_enabled(False)
    assert await put_artifact_async(session._run_ctx, b"paused") is None
    session.set_audio_capture_enabled(True)
    assert await put_artifact_async(session._run_ctx, b"resumed") is not None
    consent["enabled"] = False
    assert await put_artifact_async(session._run_ctx, b"revoked") is None
    session.set_audio_capture_enabled(None)
    consent["enabled"] = True
    assert await put_artifact_async(session._run_ctx, b"policy-restored") is not None


def test_session_audio_capture_toggle_requires_bool():
    session = create_session(
        EasyConfig(
            stt=_ProviderShapeSTT(),
            tts=_ProviderShapeTTS(),
            vad=_ProviderShapeVAD(),
            transport=_IdentitySinkTransport(),
            agent=_DummyAgent(),
        )
    )

    with pytest.raises(TypeError, match="bool"):
        session.set_audio_capture_enabled(1)  # type: ignore[arg-type]


def test_session_audio_capture_predicate_fails_closed():
    session = create_session(
        EasyConfig(
            stt=_ProviderShapeSTT(),
            tts=_ProviderShapeTTS(),
            vad=_ProviderShapeVAD(),
            transport=_IdentitySinkTransport(),
            agent=_DummyAgent(),
            capture_audio=lambda: "yes",  # type: ignore[arg-type,return-value]
        )
    )

    assert session._is_audio_capture_enabled() is False
    assert session._is_audio_capture_enabled() is False


def test_enabling_capture_discards_pre_consent_turn_buffers():
    from easycat.audio_format import PCM16_MONO_16K, AudioChunk

    consent = {"enabled": False}
    session = create_session(
        EasyConfig(
            stt=_ProviderShapeSTT(),
            tts=_ProviderShapeTTS(),
            vad=_ProviderShapeVAD(),
            transport=_IdentitySinkTransport(),
            agent=_DummyAgent(),
            capture_audio=lambda: consent["enabled"],
        )
    )
    assert session._is_audio_capture_enabled() is False
    session._turn_manager.on_audio_frame(AudioChunk(data=b"\x00\x00" * 160, format=PCM16_MONO_16K))
    assert session._turn_manager._pre_roll_buffer

    consent["enabled"] = True
    assert session._is_audio_capture_enabled() is True
    assert not session._turn_manager._pre_roll_buffer
    assert session._turn_manager.turn_audio == []


@pytest.mark.asyncio
async def test_capture_revocation_fences_in_flight_blocking_write():
    store = _BlockingArtifactStore()
    session = create_session(
        EasyConfig(
            stt=_ProviderShapeSTT(),
            tts=_ProviderShapeTTS(),
            vad=_ProviderShapeVAD(),
            transport=_IdentitySinkTransport(),
            agent=_DummyAgent(),
            capture_audio=True,
        )
    )
    ctx = replace(session._run_ctx, artifact_store=store)
    write_task = asyncio.create_task(put_artifact_async(ctx, b"sensitive"))
    assert await asyncio.wait_for(asyncio.to_thread(store.started.wait), timeout=1)

    session.set_audio_capture_enabled(False)
    store.release.set()

    assert await write_task is None
    assert store.refs == set()


def test_create_session_closes_vad_when_later_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vad = _ProviderShapeVAD()
    vad.closed = False
    vad.close = lambda: setattr(vad, "closed", True)
    monkeypatch.setattr(config_factory, "_create_vad", lambda config: vad)
    monkeypatch.setattr(
        config_factory,
        "_resolve_agent",
        lambda config, mcp_servers: (_ for _ in ()).throw(RuntimeError("agent failed")),
    )

    with pytest.raises(RuntimeError, match="agent failed"):
        create_session(
            EasyConfig(
                stt=_ProviderShapeSTT(),
                tts=_ProviderShapeTTS(),
                vad=_ProviderShapeVAD(),
                transport=_IdentitySinkTransport(),
                agent=_DummyAgent(),
            )
        )

    assert vad.closed is True


@pytest.mark.asyncio
async def test_create_session_is_safe_to_construct_off_loop(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("EASYCAT_DATA_DIR", str(tmp_path))
    config = EasyConfig(
        stt=_ProviderShapeSTT(),
        tts=_ProviderShapeTTS(),
        vad=_ProviderShapeVAD(),
        transport=_IdentitySinkTransport(),
        agent=_DummyAgent(),
        debug="full",
    )

    session = await asyncio.to_thread(create_session, config)
    try:
        assert session.session_id
        assert session._journal is not None
    finally:
        await session.stop(force=True)


def test_create_session_binds_custom_identity_sink_capability():
    transport = _IdentitySinkTransport()
    session = create_session(
        EasyConfig(
            stt=DeepgramSTTConfig(api_key="test-key", model="flux-general-en"),
            tts=OpenAITTSConfig(api_key="test-key"),
            transport=transport,
            agent=_DummyAgent(),
        )
    )
    identity = CallIdentity(caller_number="+15551234567")

    assert transport.identity_sink is not None
    transport.identity_sink(identity)

    assert session.call_identity is identity


def test_create_session_accepts_root_exported_named_provider_configs() -> None:
    session = create_session(
        EasyConfig(
            openai_api_key="test-key",
            stt=STTProviderConfig(provider="openai"),
            tts=TTSProviderConfig(provider="openai"),
            vad=_ProviderShapeVAD(),
            transport=_IdentitySinkTransport(),
            agent=_DummyAgent(),
            debug="off",
        )
    )

    assert isinstance(session.stt, OpenAISTT)
    assert isinstance(session.tts, OpenAITTS)
    assert session.stt._config.event_bus is session.event_bus
    assert session.tts._config.event_bus is session.event_bus


@pytest.mark.parametrize(
    ("field_name", "provider_config", "kind"),
    [
        (
            "stt",
            STTProviderConfig(
                provider="deepgram",
                api_key="test-key",
                params={"encoding": "mp3"},
            ),
            "STT",
        ),
        (
            "tts",
            TTSProviderConfig(
                provider="deepgram",
                api_key="test-key",
                params={"encoding": "mp3"},
            ),
            "TTS",
        ),
    ],
)
def test_named_provider_configs_wrap_value_validation_errors(
    field_name: str,
    provider_config: STTProviderConfig | TTSProviderConfig,
    kind: str,
) -> None:
    with pytest.raises(
        EasyConfigError,
        match=rf"Invalid params for 'deepgram' {kind} provider",
    ) as exc_info:
        EasyConfig(**{field_name: provider_config})

    assert isinstance(exc_info.value.__cause__, ValueError)
    assert f"Unsupported Deepgram {kind} encoding" in str(exc_info.value)


def test_named_provider_configs_preserve_easycat_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from easycat.stt.factory import _CATALOG

    _CATALOG.discover()
    provider_cls, _config_cls = _CATALOG.providers["deepgram"]
    expected = EasyConfigError("provider-specific validation failed")

    class _FailingConfig:
        def __init__(self, **_kwargs: object) -> None:
            raise expected

    monkeypatch.setitem(_CATALOG.providers, "deepgram", (provider_cls, _FailingConfig))

    with pytest.raises(EasyConfigError) as exc_info:
        EasyConfig(
            stt=STTProviderConfig(provider="deepgram", api_key="test-key"),
        )

    assert exc_info.value is expected


def test_create_session_accepts_custom_provider_instances_without_sessionconfig(  # noqa: C901
    monkeypatch: pytest.MonkeyPatch,
):
    class _CustomSTT:
        async def start_stream(self):
            pass

        async def send_audio(self, chunk):
            pass

        async def commit_segment(self) -> bool:
            return False

        async def end_stream(self):
            pass

        async def events(self):
            if False:
                yield None

        def version_info(self) -> dict[str, str]:
            return {"provider": "custom-stt"}

    class _CustomTTS:
        async def synthesize(self, payload):
            if False:
                yield None

        async def stop(self):
            pass

        async def cancel(self):
            pass

        def version_info(self) -> dict[str, str]:
            return {"provider": "custom-tts"}

    class _CustomVAD:
        def configure(self, **kwargs):
            pass

        async def process(self, chunk):
            if False:
                yield None

        def version_info(self) -> dict[str, str]:
            return {"provider": "custom-vad"}

    class _CustomNoiseReducer:
        async def process(self, chunk):
            return chunk

        def version_info(self) -> dict[str, str]:
            return {"provider": "custom-noise-reducer"}

    class _CustomEchoCanceller:
        async def process(self, chunk):
            return chunk

        def feed_reference(self, chunk):
            pass

        def version_info(self) -> dict[str, str]:
            return {"provider": "custom-echo-canceller"}

    def _fail_factory(*_args, **_kwargs):
        raise RuntimeError("provider config factory should not be called")

    stt = _CustomSTT()
    tts = _CustomTTS()
    vad = _CustomVAD()
    noise_reducer = _CustomNoiseReducer()
    echo_canceller = _CustomEchoCanceller()

    monkeypatch.setattr("easycat.config._factory.create_stt_provider_from_config", _fail_factory)
    monkeypatch.setattr("easycat.config._factory.create_tts_provider_from_config", _fail_factory)
    monkeypatch.setattr("easycat.config._factory.create_vad", _fail_factory)
    monkeypatch.setattr("easycat.config._factory.create_noise_reducer", _fail_factory)
    monkeypatch.setattr("easycat.config._factory.create_echo_canceller", _fail_factory)

    session = create_session(
        EasyConfig(
            stt=stt,
            tts=tts,
            vad=vad,
            noise_reduction=noise_reducer,
            echo_cancellation=echo_canceller,
            agent=_DummyAgent(),
        )
    )

    assert session._config.stt is stt
    assert session._config.tts is tts
    assert session._config.vad is vad
    assert session._config.noise_reducer is noise_reducer
    assert session._config.echo_canceller is echo_canceller
    assert session._enable_noise_reduction is True
    assert session._enable_aec is True


def test_create_session_requires_agent_before_allocating_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config_factory,
        "_create_debug_resources",
        lambda *_args, **_kwargs: pytest.fail("resources allocated before agent validation"),
    )

    with pytest.raises(EasyConfigError, match="agent is required"):
        create_session(
            EasyConfig(
                stt=_ProviderShapeSTT(),
                tts=_ProviderShapeTTS(),
                vad=_ProviderShapeVAD(),
                transport=_IdentitySinkTransport(),
                agent=None,
            )
        )


def test_create_session_revalidates_mutated_privacy_policy_before_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = EasyConfig(
        stt=_ProviderShapeSTT(),
        tts=_ProviderShapeTTS(),
        vad=_ProviderShapeVAD(),
        transport=_IdentitySinkTransport(),
        agent=_DummyAgent(),
    )
    config.caller_id_exposure = "offf"  # type: ignore[assignment]
    monkeypatch.setattr(
        config_factory,
        "_create_debug_resources",
        lambda *_args, **_kwargs: pytest.fail("resources allocated before policy validation"),
    )

    with pytest.raises(EasyConfigError, match="caller_id_exposure"):
        create_session(config)


def test_create_session_revalidates_mutated_timeout_policy_before_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = EasyConfig(
        stt=_ProviderShapeSTT(),
        tts=_ProviderShapeTTS(),
        vad=_ProviderShapeVAD(),
        transport=_IdentitySinkTransport(),
        agent=_DummyAgent(),
    )
    config.timeouts.agent_timeout = 0.0
    monkeypatch.setattr(
        config_factory,
        "_create_debug_resources",
        lambda *_args, **_kwargs: pytest.fail("resources allocated before timeout validation"),
    )

    with pytest.raises(ValueError, match="agent_timeout must be a finite positive number"):
        create_session(config)


def test_create_session_revalidates_mutated_outbound_policy_before_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outbound = OutboundCallConfig(from_number="+15551234567")
    config = EasyConfig(
        stt=_ProviderShapeSTT(),
        tts=_ProviderShapeTTS(),
        vad=_ProviderShapeVAD(),
        transport=_IdentitySinkTransport(),
        agent=_DummyAgent(),
        telephony=TelephonyConfig(outbound=outbound),
    )
    outbound.max_call_duration_s = 0
    monkeypatch.setattr(
        config_factory,
        "_create_debug_resources",
        lambda *_args, **_kwargs: pytest.fail("resources allocated before outbound validation"),
    )

    with pytest.raises(ValueError, match="max_call_duration_s must be positive"):
        create_session(config)


def test_create_session_revalidates_mutated_nested_voicemail_policy_before_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outbound = OutboundCallConfig(from_number="+15551234567")
    config = EasyConfig(
        stt=_ProviderShapeSTT(),
        tts=_ProviderShapeTTS(),
        vad=_ProviderShapeVAD(),
        transport=_IdentitySinkTransport(),
        agent=_DummyAgent(),
        telephony=TelephonyConfig(outbound=outbound),
    )
    outbound.voicemail_detection.mode = "detect_end"  # type: ignore[assignment]
    monkeypatch.setattr(
        config_factory,
        "_create_debug_resources",
        lambda *_args, **_kwargs: pytest.fail("resources allocated before voicemail validation"),
    )

    with pytest.raises(EasyConfigError, match="voicemail_detection.mode"):
        create_session(config)


def test_create_session_rejects_replaced_outbound_before_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RaisingCopyOutbound:
        copy_called = False

        def __copy__(self) -> object:
            type(self).copy_called = True
            raise AssertionError("invalid outbound was copied before validation")

    config = EasyConfig(
        stt=_ProviderShapeSTT(),
        tts=_ProviderShapeTTS(),
        vad=_ProviderShapeVAD(),
        transport=_IdentitySinkTransport(),
        agent=_DummyAgent(),
        telephony=TelephonyConfig(outbound=OutboundCallConfig()),
    )
    config.telephony.outbound = _RaisingCopyOutbound()  # type: ignore[assignment]
    monkeypatch.setattr(
        config_factory,
        "_create_debug_resources",
        lambda *_args, **_kwargs: pytest.fail("resources allocated before outbound type check"),
    )

    with pytest.raises(EasyConfigError, match="telephony.outbound must be an OutboundCallConfig"):
        create_session(config)
    assert _RaisingCopyOutbound.copy_called is False


def test_create_session_revalidates_mutated_outbound_boolean_before_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outbound = OutboundCallConfig()
    config = EasyConfig(
        stt=_ProviderShapeSTT(),
        tts=_ProviderShapeTTS(),
        vad=_ProviderShapeVAD(),
        transport=_IdentitySinkTransport(),
        agent=_DummyAgent(),
        telephony=TelephonyConfig(outbound=outbound),
    )
    outbound.classification_gate = "false"  # type: ignore[assignment]
    monkeypatch.setattr(
        config_factory,
        "_create_debug_resources",
        lambda *_args, **_kwargs: pytest.fail("resources allocated before boolean validation"),
    )

    with pytest.raises(EasyConfigError, match="classification_gate must be a boolean"):
        create_session(config)


def test_create_session_revalidates_mutated_telephony_boolean_before_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telephony = TelephonyConfig()
    config = EasyConfig(
        stt=_ProviderShapeSTT(),
        tts=_ProviderShapeTTS(),
        vad=_ProviderShapeVAD(),
        transport=_IdentitySinkTransport(),
        agent=_DummyAgent(),
        telephony=telephony,
    )
    telephony.enable_outbound_call_manager = "false"  # type: ignore[assignment]
    monkeypatch.setattr(
        config_factory,
        "_create_debug_resources",
        lambda *_args, **_kwargs: pytest.fail(
            "resources allocated before telephony boolean validation"
        ),
    )

    with pytest.raises(EasyConfigError, match="enable_outbound_call_manager must be a boolean"):
        create_session(config)


def test_create_session_rejects_replaced_telephony_before_copying_nested_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RaisingCopyTelephony:
        copy_called = False

        def __copy__(self) -> object:
            type(self).copy_called = True
            raise AssertionError("invalid telephony config was copied before validation")

    config = EasyConfig(
        stt=_ProviderShapeSTT(),
        tts=_ProviderShapeTTS(),
        vad=_ProviderShapeVAD(),
        transport=_IdentitySinkTransport(),
        agent=_DummyAgent(),
        telephony=TelephonyConfig(),
    )
    config.telephony = _RaisingCopyTelephony()  # type: ignore[assignment]
    monkeypatch.setattr(
        config_factory,
        "_create_debug_resources",
        lambda *_args, **_kwargs: pytest.fail("resources allocated before telephony type check"),
    )

    with pytest.raises(EasyConfigError, match="telephony must be a TelephonyConfig"):
        create_session(config)
    assert _RaisingCopyTelephony.copy_called is False


def test_create_session_copies_timeout_policy_for_runtime() -> None:
    timeouts = TimeoutConfig(agent_timeout=12.0)
    config = EasyConfig(
        stt=_ProviderShapeSTT(),
        tts=_ProviderShapeTTS(),
        vad=_ProviderShapeVAD(),
        transport=_IdentitySinkTransport(),
        agent=_DummyAgent(),
        debug="off",
        timeouts=timeouts,
    )

    session = create_session(config)
    timeouts.agent_timeout = 1.0

    assert session._timeout_config is not timeouts
    assert session._timeout_config.agent_timeout == 12.0


def test_audio_runtime_config_copies_nested_retry_policy() -> None:
    retry = RetryStrategyConfig(max_retries=4)
    outbound = OutboundCallConfig(from_number="+15551234567", retry_strategy=retry)
    config = EasyConfig(
        stt=_ProviderShapeSTT(),
        tts=_ProviderShapeTTS(),
        vad=_ProviderShapeVAD(),
        transport=_IdentitySinkTransport(),
        agent=_DummyAgent(),
        telephony=TelephonyConfig(outbound=outbound),
    )

    runtime = config_factory._audio_runtime_config(config)
    runtime_retry = runtime.telephony.outbound.retry_strategy  # type: ignore[union-attr]
    retry.max_retries = 1

    assert runtime_retry is not retry
    assert runtime_retry is not None
    assert runtime_retry.max_retries == 4


def test_create_session_revalidates_mutated_retry_policy_before_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retry = RetryStrategyConfig()
    config = EasyConfig(
        stt=_ProviderShapeSTT(),
        tts=_ProviderShapeTTS(),
        vad=_ProviderShapeVAD(),
        transport=_IdentitySinkTransport(),
        agent=_DummyAgent(),
        telephony=TelephonyConfig(
            outbound=OutboundCallConfig(from_number="+15551234567", retry_strategy=retry)
        ),
    )
    retry.base_delay_s = float("nan")
    monkeypatch.setattr(
        config_factory,
        "_create_debug_resources",
        lambda *_args, **_kwargs: pytest.fail("resources allocated before retry validation"),
    )

    with pytest.raises(EasyConfigError, match="retry_strategy.*base_delay_s"):
        create_session(config)


def test_create_session_accepts_policy_only_custom_tts(
    monkeypatch: pytest.MonkeyPatch,
):
    class _CustomTTS:
        input_policy = TTSInputPolicy.plain_text()

        async def synthesize(self, payload):
            if False:
                yield None

        async def stop(self):
            pass

        async def cancel(self):
            pass

        def version_info(self) -> dict[str, str]:
            return {"provider": "custom-tts"}

    def _fail_tts_factory(*_args, **_kwargs):
        raise RuntimeError("provider config factory should not be called")

    tts = _CustomTTS()
    monkeypatch.setattr(
        "easycat.config._factory.create_tts_provider_from_config", _fail_tts_factory
    )

    # Flux STT keeps SmartTurn (and the Silero VAD it pulls in) off by default
    # even on the local-mic preset, so this TTS-policy test stays free of the
    # numpy/onnx extras without pinning a transport.
    session = create_session(
        EasyConfig(
            stt=DeepgramSTTConfig(api_key="test-key", model="flux-general-en"),
            tts=tts,
            agent=_DummyAgent(),
        )
    )

    assert session._config.tts is tts


def test_create_session_forwards_warmup_to_runtime_config():
    session = create_session(
        EasyConfig(
            stt=DeepgramSTTConfig(api_key="test-key", model="flux-general-en"),
            tts=OpenAITTSConfig(api_key="test-key"),
            transport=_IdentitySinkTransport(),
            agent=_DummyAgent(),
            warmup=False,
        )
    )

    assert session._config.warmup is False
    assert session._warmup.enabled is False


def test_create_session_preserves_data_dir_before_emergency_export(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    armed_with: list[object] = []
    monkeypatch.setattr(
        "easycat.config._factory.install_emergency_export",
        lambda session: armed_with.append(session),
    )

    session = create_session(
        EasyConfig(
            stt=DeepgramSTTConfig(api_key="test-key", model="flux-general-en"),
            tts=OpenAITTSConfig(api_key="test-key"),
            transport=_IdentitySinkTransport(),
            agent=_DummyAgent(),
            data_dir=tmp_path,
            emergency_export=True,
        )
    )

    assert session._data_dir == tmp_path
    assert armed_with == [session]


def test_create_session_configures_event_dispatch():
    session = create_session(
        EasyConfig(
            stt=DeepgramSTTConfig(api_key="test-key", model="flux-general-en"),
            tts=OpenAITTSConfig(api_key="test-key"),
            transport=_IdentitySinkTransport(),
            agent=_DummyAgent(),
            slow_handler_threshold_s=0.125,
            handler_error_policy="raise",
        )
    )

    assert session.event_bus.slow_handler_threshold_s == 0.125
    assert session.event_bus.handler_error_policy == "raise"


@pytest.mark.asyncio
async def test_create_session_binds_twilio_connection_identity_sink():
    transport = TwilioConnectionTransport(_DummyWebSocket())
    session = create_session(
        EasyConfig(
            stt=DeepgramSTTConfig(api_key="test-key", model="flux-general-en"),
            tts=OpenAITTSConfig(api_key="test-key"),
            transport=transport,
            agent=_DummyAgent(),
        )
    )

    await transport._handle_start(
        {
            "streamSid": "MZ1",
            "start": {
                "streamSid": "MZ1",
                "callSid": "CA1",
                "customParameters": {
                    "From": "+15551234567",
                    "To": "+15557654321",
                    "CallerName": "Alice Example",
                    "FromCity": "SAN FRANCISCO",
                    "FromState": "CA",
                    "FromZip": "94105",
                    "FromCountry": "US",
                    "caller_name": "Alias Name",
                    "from_city": "ALIAS CITY",
                    "from_state": "ZZ",
                    "from_zip": "00000",
                    "from_country": "ZZ",
                },
            },
        }
    )

    assert session.call_identity is transport.call_identity
    assert session.call_identity is not None
    assert session.call_identity.caller_number == "+15551234567"
    assert session.call_identity.called_number == "+15557654321"
    assert session.call_identity.display_name == "Alice Example"
    assert session.call_identity.city == "SAN FRANCISCO"
    assert session.call_identity.state == "CA"
    assert session.call_identity.zip_code == "94105"
    assert session.call_identity.country == "US"
    assert session.call_identity.custom_fields == {}


@pytest.mark.asyncio
async def test_create_session_caller_id_off_keeps_twilio_identity_private():
    transport = TwilioConnectionTransport(_DummyWebSocket())
    session = create_session(
        EasyConfig(
            stt=DeepgramSTTConfig(api_key="test-key", model="flux-general-en"),
            tts=OpenAITTSConfig(api_key="test-key"),
            transport=transport,
            agent=_DummyAgent(),
            caller_id_exposure="off",
        )
    )

    await transport._handle_start(
        {
            "streamSid": "MZ1",
            "start": {
                "streamSid": "MZ1",
                "callSid": "CA1",
                "customParameters": {
                    "From": "+15551234567",
                    "To": "+15557654321",
                },
            },
        }
    )

    assert transport.call_identity is not None
    assert session.call_identity is None
    assert session._caller_id.private_identity is not None
    assert session._caller_id.private_identity.caller_number == "+15551234567"


@pytest.mark.asyncio
async def test_create_session_twilio_identity_sink_merges_with_outbound_identity():
    transport = TwilioConnectionTransport(_DummyWebSocket())
    session = create_session(
        EasyConfig(
            stt=DeepgramSTTConfig(api_key="test-key", model="flux-general-en"),
            tts=OpenAITTSConfig(api_key="test-key"),
            transport=transport,
            agent=_DummyAgent(),
        )
    )
    session.call_identity = CallIdentity(
        caller_number="+15551112222",
        called_number="+15559876543",
        direction="outbound",
    )

    await transport._handle_start(
        {
            "streamSid": "MZ1",
            "start": {
                "streamSid": "MZ1",
                "callSid": "CA1",
                "customParameters": {
                    "Direction": "outbound-api",
                    "From": "+15559876543",
                    "To": "+15551112222",
                    "crm_account_id": "ACC-42",
                },
            },
        }
    )

    assert transport.call_identity is not None
    assert transport.call_identity.direction == "outbound"
    assert transport.call_identity.caller_number == "+15551112222"
    assert transport.call_identity.called_number == "+15559876543"
    assert session.call_identity is not transport.call_identity
    assert session.call_identity is not None
    assert session.call_identity.direction == "outbound"
    assert session.call_identity.caller_number == "+15551112222"
    assert session.call_identity.called_number == "+15559876543"
    assert session.call_identity.call_sid == "CA1"
    assert session.call_identity.custom_fields == {"crm_account_id": "ACC-42"}
