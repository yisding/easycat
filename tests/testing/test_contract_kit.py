"""Tests for the installable contract kit in ``easycat.testing``.

These cover the kit's own machinery (factory resolution, live-mode gating,
hung-stream timeouts) and prove each suite rejects representative protocol
violations. The suites' green path against conformant fakes runs in
``tests/contracts/`` where the in-tree contract files subclass the kit.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from easycat.audio_format import PCM16_MONO_24K, AudioChunk
from easycat.events import (
    Event,
    STTEvent,
    STTEventType,
    TTSEvent,
    TTSEventType,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
    CancellationMode,
    CommitRule,
    FrameworkStateSnapshot,
    UnitKind,
)
from easycat.testing import (
    AGENT_BRIDGE_EVENT_KINDS,
    AgentBridgeContractSuite,
    RecordingAgentRecorder,
    STTProviderContractSuite,
    TransportContractSuite,
    TTSProviderContractSuite,
    VADProviderContractSuite,
)

pytestmark = [pytest.mark.contract]


# ── Minimal conformant fakes ─────────────────────────────────────


class _KitSTT:
    async def start_stream(self) -> None:
        self._queue: asyncio.Queue[STTEvent | None] = asyncio.Queue()

    async def send_audio(self, chunk: AudioChunk) -> None:
        del chunk

    async def commit_segment(self) -> bool:
        await self._queue.put(STTEvent(type=STTEventType.PARTIAL, text="kit"))
        await self._queue.put(STTEvent(type=STTEventType.FINAL, text="kit hello"))
        return True

    async def end_stream(self) -> None:
        queue = getattr(self, "_queue", None)
        if queue is None:
            self._queue = queue = asyncio.Queue()
        await queue.put(None)

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                break
            yield event

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "kit-stt",
            "model": "offline",
            "api_version": "v1",
            "sdk_version": "none",
        }


class _KitTTS:
    async def synthesize(self, payload: object) -> AsyncIterator[TTSEvent]:
        del payload
        yield TTSEvent(
            type=TTSEventType.AUDIO,
            audio=AudioChunk(data=b"\0" * 320, format=PCM16_MONO_24K),
        )

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "kit-tts",
            "model": "offline",
            "api_version": "v1",
            "sdk_version": "none",
        }


class _KitVAD:
    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        del chunk
        yield VADStartSpeaking()
        yield VADStopSpeaking()

    def configure(
        self,
        *,
        min_speech_duration_ms: int = 250,
        min_silence_duration_ms: int = 150,
        sensitivity: float = 0.5,
    ) -> None:
        pass

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "kit-vad",
            "model": "offline",
            "api_version": "v1",
            "sdk_version": "none",
        }


class _KitTransport:
    def __init__(self) -> None:
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        return
        yield  # pragma: no cover - makes this an async generator

    async def send_audio(self, chunk: AudioChunk) -> bool:
        del chunk
        return self.connected

    async def clear_audio(self) -> None:
        pass

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "kit-transport",
            "model": "unknown",
            "api_version": "v1",
            "sdk_version": "none",
        }


class _KitBridge:
    COMMITTABLE_BOUNDARIES = {UnitKind.AGENT: CommitRule.BETWEEN_TURNS}  # noqa: RUF012 test fake uses shared class fixture

    def __init__(self) -> None:
        self.history: list[str] = []

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token=None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        del recorder, cancel_token
        self.history.append(turn_input.text)
        yield AgentBridgeEvent(kind="text_delta", text="kit")
        yield AgentBridgeEvent(kind="done", text="kit")

    def snapshot_state(self) -> FrameworkStateSnapshot:
        return FrameworkStateSnapshot(fields={"history": list(self.history)}, kind="kit")

    def apply_interruption(
        self,
        delivered_text: str,
        mode: CancellationMode,
        recorder: AgentRecorder | None = None,
        caused_by_signal_id: str | None = None,
    ) -> None:
        del delivered_text, mode
        if recorder is not None:
            recorder.record_state_committed("interrupt_truncate")
            recorder.record_cancellation_boundary(
                CancellationMode.IMMEDIATE_STOP,
                caused_by_signal_id=caused_by_signal_id,
            )

    def replace_last_assistant_text(self, text: str) -> None:
        if self.history:
            self.history[-1] = text

    def append_interruption_note(self, note: str) -> None:
        self.history.append(note)

    def reset(self) -> None:
        self.history.clear()


class _KitSTTSuite(STTProviderContractSuite):
    provider_factory = _KitSTT


# Collected end-to-end: proves pytest picks up subclassed kit suites the way
# an external provider author would use them.
class TestKitSTTExample(STTProviderContractSuite):
    provider_factory = _KitSTT


# ── Factory resolution ───────────────────────────────────────────


def test_provider_factory_accepts_class_function_and_staticmethod() -> None:
    def make() -> _KitSTT:
        return _KitSTT()

    class _ClassStyle(STTProviderContractSuite):
        provider_factory = _KitSTT

    class _FunctionStyle(STTProviderContractSuite):
        provider_factory = make

    class _StaticStyle(STTProviderContractSuite):
        provider_factory = staticmethod(make)

    assert isinstance(_ClassStyle.build_provider(), _KitSTT)
    assert isinstance(_FunctionStyle.build_provider(), _KitSTT)
    assert isinstance(_StaticStyle.build_provider(), _KitSTT)


def test_build_provider_fails_without_factory() -> None:
    class _Missing(STTProviderContractSuite):
        pass

    with pytest.raises(pytest.fail.Exception, match="provider_factory"):
        _Missing.build_provider()


# ── Live-mode gating ─────────────────────────────────────────────


class _LiveSuite(STTProviderContractSuite):
    provider_factory = _KitSTT
    live = True
    credential_env_var = "EASYCAT_CONTRACT_KIT_TEST_KEY"


def test_live_mode_skips_without_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EASYCAT_CONTRACT_KIT_TEST_KEY", raising=False)
    with pytest.raises(pytest.skip.Exception, match="EASYCAT_CONTRACT_KIT_TEST_KEY"):
        _LiveSuite.require_live_credentials()


def test_live_mode_proceeds_with_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EASYCAT_CONTRACT_KIT_TEST_KEY", "kit-test-value")
    _LiveSuite.require_live_credentials()


def test_offline_mode_never_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EASYCAT_CONTRACT_KIT_TEST_KEY", raising=False)
    _KitSTTSuite.require_live_credentials()


# ── Hung-stream timeout ──────────────────────────────────────────


async def test_collect_events_fails_on_hung_stream() -> None:
    class _HungSTT(_KitSTT):
        async def events(self) -> AsyncIterator[STTEvent]:
            while True:
                await asyncio.sleep(3600)
                yield STTEvent(type=STTEventType.PARTIAL, text="never")

    class _FastTimeoutSuite(STTProviderContractSuite):
        provider_factory = _HungSTT
        event_timeout = 0.05

    suite = _FastTimeoutSuite()
    with pytest.raises(pytest.fail.Exception, match="did not terminate"):
        await suite.test_stream_lifecycle_yields_normalized_events(suite.build_provider())


# ── Violation detection per surface ──────────────────────────────


async def test_stt_suite_rejects_stream_without_final_transcript() -> None:
    class _SilentSTT(_KitSTT):
        async def commit_segment(self) -> bool:
            return False

        async def end_stream(self) -> None:
            queue = getattr(self, "_queue", None)
            if queue is None:
                self._queue = queue = asyncio.Queue()
            await queue.put(None)

    suite = _KitSTTSuite()
    with pytest.raises(AssertionError, match="FINAL"):
        await suite.test_stream_lifecycle_yields_normalized_events(_SilentSTT())


async def test_stt_suite_allows_accepted_empty_commit_without_final() -> None:
    class _MissingCommitFinalSTT(_KitSTT):
        async def commit_segment(self) -> bool:
            return True

    class _FastCommitSuite(STTProviderContractSuite):
        provider_factory = _MissingCommitFinalSTT
        event_timeout = 0.02

    suite = _FastCommitSuite()
    await suite.test_segment_commit_reports_boolean_acceptance(suite.build_provider())


async def test_stt_suite_rejects_cached_events_iterator() -> None:
    class _CachedIteratorSTT(_KitSTT):
        def __init__(self) -> None:
            self._cached_events: AsyncIterator[STTEvent] | None = None

        def events(self) -> AsyncIterator[STTEvent]:
            if self._cached_events is None:
                self._cached_events = super().events()
            return self._cached_events

    suite = _KitSTTSuite()
    with pytest.raises(AssertionError, match="fresh iterator"):
        await suite.test_events_iterator_is_fresh_across_turns(_CachedIteratorSTT())


async def test_stt_suite_checks_freshness_while_each_stream_is_active() -> None:
    class _LiveConsumerSTT(_KitSTT):
        async def start_stream(self) -> None:
            await super().start_stream()
            self._consumer_started = False

        async def end_stream(self) -> None:
            assert self._consumer_started, "events() was not consumed during the active stream"
            await super().end_stream()

        async def events(self) -> AsyncIterator[STTEvent]:
            self._consumer_started = True
            async for event in super().events():
                yield event

    suite = _KitSTTSuite()
    await suite.test_events_iterator_is_fresh_across_turns(_LiveConsumerSTT())


async def test_version_info_test_rejects_leaked_secret_values() -> None:
    leaked_secret = "sk-" + "a" * 20

    class _LeakySTT(_KitSTT):
        def version_info(self) -> dict[str, str]:
            return {
                **super().version_info(),
                "session": leaked_secret,
            }

    suite = _KitSTTSuite()
    with pytest.raises(pytest.fail.Exception, match="sensitive") as exc_info:
        await suite.test_version_info_is_a_redacted_string_mapping(_LeakySTT())

    assert leaked_secret not in str(exc_info.value)


async def test_version_info_test_rejects_missing_provider_without_echoing_mapping() -> None:
    leaked_secret = "sk-" + "b" * 20

    class _MissingProviderSTT(_KitSTT):
        def version_info(self) -> dict[str, str]:
            return {"api_key": leaked_secret}

    suite = _KitSTTSuite()
    with pytest.raises(pytest.fail.Exception, match="stable 'provider' name") as exc_info:
        await suite.test_version_info_is_a_redacted_string_mapping(_MissingProviderSTT())

    assert leaked_secret not in str(exc_info.value)
    assert "api_key" not in str(exc_info.value)


async def test_version_info_test_rejects_missing_diagnostic_fields() -> None:
    class _IncompleteSTT(_KitSTT):
        def version_info(self) -> dict[str, str]:
            return {"provider": "kit-stt", "model": "offline"}

    suite = _KitSTTSuite()
    with pytest.raises(pytest.fail.Exception, match="api_version, sdk_version"):
        await suite.test_version_info_is_a_redacted_string_mapping(_IncompleteSTT())


async def test_version_info_test_rejects_empty_required_field() -> None:
    class _EmptyModelSTT(_KitSTT):
        def version_info(self) -> dict[str, str]:
            return {**super().version_info(), "model": ""}

    suite = _KitSTTSuite()
    with pytest.raises(pytest.fail.Exception, match="must be a non-empty str"):
        await suite.test_version_info_is_a_redacted_string_mapping(_EmptyModelSTT())


async def test_tts_suite_rejects_stream_without_audio() -> None:
    class _SilentTTS(_KitTTS):
        async def synthesize(self, payload: object) -> AsyncIterator[TTSEvent]:
            del payload
            yield TTSEvent(type=TTSEventType.MARKERS, markers=[])

    class _TTSSuite(TTSProviderContractSuite):
        provider_factory = _SilentTTS

    suite = _TTSSuite()
    with pytest.raises(AssertionError, match="AUDIO"):
        await suite.test_synthesize_streams_normalized_events(suite.build_provider())


async def test_vad_suite_rejects_stop_before_start() -> None:
    class _UnbalancedVAD(_KitVAD):
        async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
            del chunk
            yield VADStopSpeaking()
            yield VADStartSpeaking()

    class _VADSuite(VADProviderContractSuite):
        provider_factory = _UnbalancedVAD

    suite = _VADSuite()
    with pytest.raises(AssertionError, match="VADStopSpeaking without"):
        await suite.test_process_yields_balanced_speech_boundaries(suite.build_provider())


async def test_transport_suite_rejects_send_accepted_before_connect() -> None:
    class _EagerTransport(_KitTransport):
        async def send_audio(self, chunk: AudioChunk) -> bool:
            del chunk
            return True

    class _TransportSuite(TransportContractSuite):
        provider_factory = _EagerTransport

    suite = _TransportSuite()
    with pytest.raises(AssertionError):
        await suite.test_send_audio_is_dropped_before_connect(suite.build_provider())


async def test_agent_bridge_suite_rejects_stream_missing_done() -> None:
    class _NoDoneBridge(_KitBridge):
        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token=None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            del recorder, cancel_token
            self.history.append(turn_input.text)
            yield AgentBridgeEvent(kind="text_delta", text="kit")

    class _BridgeSuite(AgentBridgeContractSuite):
        provider_factory = _NoDoneBridge

    suite = _BridgeSuite()
    with pytest.raises(AssertionError, match="done"):
        await suite.test_invoke_stream_follows_event_grammar(
            suite.build_provider(), RecordingAgentRecorder()
        )


async def test_agent_bridge_suite_accepts_minimal_conformant_bridge() -> None:
    class _BridgeSuite(AgentBridgeContractSuite):
        provider_factory = _KitBridge

    suite = _BridgeSuite()
    await suite.test_satisfies_external_agent_bridge_protocol(suite.build_provider())
    await suite.test_invoke_stream_follows_event_grammar(
        suite.build_provider(), RecordingAgentRecorder()
    )
    await suite.test_snapshot_state_is_json_safe(suite.build_provider(), RecordingAgentRecorder())
    await suite.test_apply_interruption_journals_the_commit_protocol(
        suite.build_provider(), RecordingAgentRecorder()
    )
    await suite.test_reset_returns_bridge_to_a_json_safe_state(
        suite.build_provider(), RecordingAgentRecorder()
    )


def test_agent_bridge_event_kinds_match_the_bridge_grammar() -> None:
    assert AGENT_BRIDGE_EVENT_KINDS == {
        "text_delta",
        "text_replace",
        "tool_started",
        "tool_delta",
        "tool_result",
        "done",
    }
