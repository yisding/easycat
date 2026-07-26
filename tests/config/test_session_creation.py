from __future__ import annotations

import pytest

from easycat import (
    EasyConfig,
    create_session,
)
from easycat.session._types import CallIdentity
from easycat.stages.base import put_artifact_async
from easycat.stt.deepgram_provider import DeepgramSTTConfig
from easycat.transports.twilio_media import TwilioConnectionTransport
from easycat.tts.input import TTSInputPolicy
from easycat.tts.openai_tts import OpenAITTSConfig
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


def test_create_session_requires_real_agent_when_audio_pipeline_is_real():
    with pytest.raises(ValueError, match="agent"):
        create_session(
            EasyConfig(
                stt=_ProviderShapeSTT(),
                tts=_ProviderShapeTTS(),
                vad=_ProviderShapeVAD(),
                transport=_IdentitySinkTransport(),
                agent=None,
            )
        )


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
                },
            },
        }
    )

    assert session.call_identity is transport.call_identity
    assert session.call_identity is not None
    assert session.call_identity.caller_number == "+15551234567"
    assert session.call_identity.called_number == "+15557654321"
    assert session.call_identity.display_name == "Alice Example"


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
