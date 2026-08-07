"""Factory-parametrized contract suites for EasyCat provider Protocols.

Subclass the suite for your surface, point ``provider_factory`` at a
zero-argument callable (usually the provider class itself), and pytest
collects the protocol-semantics tests against your implementation::

    from easycat.testing import STTProviderContractSuite

    class TestAcmeSTT(STTProviderContractSuite):
        provider_factory = AcmeSTT

Suites are **offline by default**: the factory should build a provider that
can complete one scripted exchange without the network (a replay/fake
backend is fine — EasyCat's own contract tests run both the real built-in
providers over scripted backends and small reference fakes). For an optional **live mode**,
set ``live = True`` and ``credential_env_var`` on the subclass; the
``provider`` fixture then skips when the credential is missing. In EasyCat's
own repo, live subclasses should additionally carry
``pytest.mark.integration_live``.

The suites require ``pytest-asyncio`` (``asyncio_mode = "auto"`` is the
configuration EasyCat itself uses). Every suite test is marked
``pytest.mark.contract`` via the base class.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from collections import Counter
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any, ClassVar, get_args

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.events import (
    STTEvent,
    STTEventType,
    TTSEvent,
    TTSEventType,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentEventKind,
    AgentTurnInput,
    CancellationMode,
    ExternalAgentBridge,
    FrameworkStateSnapshot,
)
from easycat.providers import (
    STTProvider,
    Transport,
    TTSProvider,
    VADProvider,
    VersionedProvider,
)
from easycat.testing.recorder import RecordingAgentRecorder
from easycat.tts.input import coerce_tts_input
from easycat.validation.redaction import contains_unredacted_sensitive_text, redact_text

__all__ = [
    "AgentBridgeContractSuite",
    "ContractSuite",
    "ProviderContractSuite",
    "STTProviderContractSuite",
    "TTSProviderContractSuite",
    "TransportContractSuite",
    "VADProviderContractSuite",
]

AGENT_BRIDGE_EVENT_KINDS: frozenset[str] = frozenset(get_args(AgentEventKind))
_REQUIRED_VERSION_INFO_KEYS = ("provider", "model", "api_version", "sdk_version")


class ContractSuite:
    """Shared plumbing for every contract suite: factory, live mode, timeouts.

    Class attributes subclasses may override:

    - ``provider_factory`` — zero-argument callable building the object under
      test. A provider class, a plain function, or a ``staticmethod`` all
      work.
    - ``live`` — when ``True``, the ``provider`` fixture skips unless
      ``credential_env_var`` is set in the environment.
    - ``credential_env_var`` — credential gate for live mode (e.g.
      ``"ACME_API_KEY"``).
    - ``event_timeout`` — seconds before a non-terminating provider stream
      fails the test instead of hanging it.
    """

    pytestmark: ClassVar[list[Any]] = [pytest.mark.contract]

    provider_factory: ClassVar[Callable[[], Any] | None] = None
    live: ClassVar[bool] = False
    credential_env_var: ClassVar[str] = ""
    event_timeout: ClassVar[float] = 5.0

    @pytest.fixture
    def provider(self) -> Any:
        """A fresh provider per test; skips live runs missing credentials."""
        self.require_live_credentials()
        return self.build_provider()

    @classmethod
    def build_provider(cls) -> Any:
        """Build one provider instance from ``provider_factory``."""
        factory = inspect.getattr_static(cls, "provider_factory", None)
        if factory is None:
            pytest.fail(
                f"{cls.__name__} must set `provider_factory` to a zero-argument callable "
                "(usually the provider class itself) that builds the provider under test",
                pytrace=False,
            )
        if isinstance(factory, staticmethod | classmethod):
            assert cls.provider_factory is not None
            return cls.provider_factory()
        return factory()

    @classmethod
    def require_live_credentials(cls) -> None:
        """Skip live-mode runs when the configured credential is absent."""
        if not cls.live:
            return
        if cls.credential_env_var and not os.environ.get(cls.credential_env_var):
            pytest.skip(f"live contract run requires {cls.credential_env_var} to be set")

    async def collect_events(self, stream: AsyncIterator[Any], *, source: str) -> list[Any]:
        """Drain an async event stream, failing loudly if it never terminates."""
        items: list[Any] = []
        try:
            async with asyncio.timeout(self.event_timeout):
                async for item in stream:
                    items.append(item)
        except TimeoutError:
            pytest.fail(
                f"{source} did not terminate within {self.event_timeout}s; "
                "contract streams must end once the provider is done",
                pytrace=False,
            )
        return items


def _version_info_key_label(key: str) -> str:
    """Return a safe key label for contract failure messages."""
    return redact_text(key)


def _fail_version_info_contract(message: str) -> None:
    """Fail without pytest assertion introspection rendering provider metadata."""
    pytest.fail(message, pytrace=False)


class ProviderContractSuite(ContractSuite):
    """Adds the cross-surface ``version_info()`` contract shared by all providers.

    Providers must report the stable four-field diagnostic shape documented by
    :class:`easycat.providers.VersionedProvider`; use ``"unknown"`` when a
    field does not apply rather than omitting it.
    """

    async def test_version_info_is_a_redacted_string_mapping(self, provider: Any) -> None:
        """``version_info()`` returns str→str metadata free of secrets."""
        assert isinstance(provider, VersionedProvider)
        info = provider.version_info()
        if not isinstance(info, dict):
            del info
            _fail_version_info_contract("version_info() must return a dict")

        has_provider_name = bool(info.get("provider"))
        if not has_provider_name:
            del info
            _fail_version_info_contract("version_info() must carry a stable 'provider' name")

        missing = [key for key in _REQUIRED_VERSION_INFO_KEYS if key not in info]
        if missing:
            del info
            _fail_version_info_contract(
                "version_info() is missing required keys: " + ", ".join(missing)
            )

        for key, value in info.items():
            if not isinstance(key, str):
                del info, key, value
                _fail_version_info_contract("version_info() contains a non-string key")
            key_label = _version_info_key_label(key)
            if not isinstance(value, str):
                del info, key, value
                _fail_version_info_contract(f"version_info()[{key_label!r}] is not a str")
            if key in _REQUIRED_VERSION_INFO_KEYS and not value.strip():
                del info, key, value
                _fail_version_info_contract(
                    f"version_info()[{key_label!r}] must be a non-empty str; use 'unknown' "
                    "when the field does not apply"
                )
            if contains_unredacted_sensitive_text(value):
                del info, key, value
                _fail_version_info_contract(
                    f"version_info()[{key_label!r}] leaks sensitive text; providers must never "
                    "report credentials, signed URLs, or request ids through version_info()"
                )


class STTProviderContractSuite(ProviderContractSuite):
    """Protocol-semantics contract for :class:`easycat.providers.STTProvider`.

    Override ``expects_final_transcript = False`` when the offline factory
    cannot produce a transcript for the sample audio (e.g. a passthrough
    stub), and ``sample_audio_chunks()`` to feed provider-specific audio.
    """

    sample_audio_format: ClassVar[AudioFormat] = PCM16_MONO_16K
    expects_final_transcript: ClassVar[bool] = True

    def sample_audio_chunks(self) -> Sequence[AudioChunk]:
        """Audio fed to the provider during the lifecycle test."""
        return (AudioChunk(data=b"\x00" * 320, format=self.sample_audio_format),)

    async def test_satisfies_stt_provider_protocol(self, provider: Any) -> None:
        assert isinstance(provider, STTProvider)

    async def test_stream_lifecycle_yields_normalized_events(self, provider: Any) -> None:
        """start → send → commit → end produces a terminating STTEvent stream."""
        await provider.start_stream()
        for chunk in self.sample_audio_chunks():
            await provider.send_audio(chunk)
        committed = await provider.commit_segment()
        assert isinstance(committed, bool), "commit_segment() must return a bool"
        await provider.end_stream()

        events = await self.collect_events(provider.events(), source="STTProvider.events()")
        for event in events:
            assert isinstance(event, STTEvent), f"events() yielded non-STTEvent {event!r}"
            assert isinstance(event.type, STTEventType)
            assert isinstance(event.text, str)
        finals = [event for event in events if event.type is STTEventType.FINAL]
        if self.expects_final_transcript:
            assert finals, "expected at least one FINAL transcript event"
            assert finals[-1].text.strip(), "FINAL transcript text must be non-empty"

    async def test_segment_commit_reports_boolean_acceptance(self, provider: Any) -> None:
        """Segment commit reports acceptance without predicting transcription."""
        await provider.start_stream()
        for chunk in self.sample_audio_chunks():
            await provider.send_audio(chunk)

        async def _collect() -> list[Any]:
            events: list[Any] = []
            async for event in provider.events():
                events.append(event)
            return events

        collector = asyncio.create_task(_collect())
        await asyncio.sleep(0)
        committed = await provider.commit_segment()
        assert isinstance(committed, bool), "commit_segment() must return a bool"
        await provider.end_stream()
        await asyncio.wait_for(collector, timeout=self.event_timeout)

    async def test_events_iterator_is_fresh_across_turns(self, provider: Any) -> None:
        """A second start/end cycle must use a fresh, productive iterator."""

        async def _cycle() -> tuple[AsyncIterator[Any], list[Any]]:
            await provider.start_stream()
            stream = provider.events()
            collector = asyncio.create_task(
                self.collect_events(stream, source="STTProvider.events()")
            )
            await asyncio.sleep(0)
            for chunk in self.sample_audio_chunks():
                await provider.send_audio(chunk)
            await provider.commit_segment()
            await provider.end_stream()
            return stream, await collector

        first_stream, first_events = await _cycle()
        second_stream, second_events = await _cycle()

        assert second_stream is not first_stream, "events() must return a fresh iterator per turn"
        if self.expects_final_transcript:
            assert any(
                isinstance(event, STTEvent) and event.type is STTEventType.FINAL
                for event in first_events
            )
            assert any(
                isinstance(event, STTEvent) and event.type is STTEventType.FINAL
                for event in second_events
            ), "the second stream cycle did not yield a FINAL"

    async def test_end_stream_is_idempotent(self, provider: Any) -> None:
        await provider.start_stream()
        await provider.end_stream()
        await provider.end_stream()


class TTSProviderContractSuite(ProviderContractSuite):
    """Protocol-semantics contract for :class:`easycat.providers.TTSProvider`."""

    sample_text: ClassVar[str] = "Hello from the EasyCat contract kit."
    expects_audio: ClassVar[bool] = True

    async def test_satisfies_tts_provider_protocol(self, provider: Any) -> None:
        assert isinstance(provider, TTSProvider)

    async def test_synthesize_streams_normalized_events(self, provider: Any) -> None:
        """synthesize(str) yields a terminating TTSEvent stream carrying audio."""
        events = await self.collect_events(
            provider.synthesize(self.sample_text), source="TTSProvider.synthesize()"
        )
        for event in events:
            assert isinstance(event, TTSEvent), f"synthesize() yielded non-TTSEvent {event!r}"
            assert isinstance(event.type, TTSEventType)
            if event.type is TTSEventType.AUDIO:
                assert isinstance(event.audio, AudioChunk), "AUDIO events must carry an AudioChunk"
                assert event.audio.data, "AUDIO events must carry non-empty audio bytes"
                assert isinstance(event.audio.format, AudioFormat)
            if event.type is TTSEventType.MARKERS:
                assert event.markers is not None, "MARKERS events must carry a markers payload"
        if self.expects_audio:
            assert any(event.type is TTSEventType.AUDIO for event in events), (
                "expected at least one AUDIO event"
            )

    async def test_synthesize_accepts_typed_tts_input(self, provider: Any) -> None:
        """synthesize() accepts a coerced :class:`TTSInput`, not just str."""
        events = await self.collect_events(
            provider.synthesize(coerce_tts_input(self.sample_text)),
            source="TTSProvider.synthesize()",
        )
        if self.expects_audio:
            assert any(event.type is TTSEventType.AUDIO for event in events)

    async def test_stop_and_cancel_are_idempotent(self, provider: Any) -> None:
        await provider.stop()
        await provider.stop()
        await provider.cancel()
        await provider.cancel()


class VADProviderContractSuite(ProviderContractSuite):
    """Protocol-semantics contract for :class:`easycat.providers.VADProvider`."""

    sample_audio_format: ClassVar[AudioFormat] = PCM16_MONO_16K

    def sample_audio_chunks(self) -> Sequence[AudioChunk]:
        """Audio fed through ``process()`` during the boundary test."""
        return (AudioChunk(data=b"\x00" * 320, format=self.sample_audio_format),)

    async def test_satisfies_vad_provider_protocol(self, provider: Any) -> None:
        assert isinstance(provider, VADProvider)

    async def test_configure_accepts_threshold_keywords(self, provider: Any) -> None:
        provider.configure(
            min_speech_duration_ms=200,
            min_silence_duration_ms=120,
            sensitivity=0.7,
        )

    async def test_process_yields_balanced_speech_boundaries(self, provider: Any) -> None:
        """``process()`` yields only start/stop events, never stop-before-start."""
        events: list[Any] = []
        for chunk in self.sample_audio_chunks():
            events.extend(
                await self.collect_events(provider.process(chunk), source="VADProvider.process()")
            )
        speaking = False
        for event in events:
            assert isinstance(event, VADStartSpeaking | VADStopSpeaking), (
                f"process() yielded non-boundary event {event!r}"
            )
            if isinstance(event, VADStartSpeaking):
                assert not speaking, "VADStartSpeaking while already speaking"
                speaking = True
            else:
                assert speaking, "VADStopSpeaking without a preceding VADStartSpeaking"
                speaking = False


class TransportContractSuite(ProviderContractSuite):
    """Protocol-semantics contract for :class:`easycat.providers.Transport`.

    Override ``expects_send_accepted_after_connect = False`` for transports
    whose offline factory connects without a peer (so ``send_audio`` legally
    drops chunks even after ``connect()``). The factory needs no lifecycle
    injection hooks: the portable rows observe only public connect,
    disconnect, receive, and send behavior.
    """

    sample_audio_format: ClassVar[AudioFormat] = PCM16_MONO_16K
    expects_send_accepted_after_connect: ClassVar[bool] = True

    def sample_audio_chunk(self) -> AudioChunk:
        return AudioChunk(data=b"\x00" * 320, format=self.sample_audio_format)

    async def test_satisfies_transport_protocol(self, provider: Any) -> None:
        assert isinstance(provider, Transport)

    async def test_send_audio_is_dropped_before_connect(self, provider: Any) -> None:
        """A disconnected transport reports the drop instead of raising."""
        assert await provider.send_audio(self.sample_audio_chunk()) is False

    async def test_connect_is_idempotent(self, provider: Any) -> None:
        """Repeated connect calls keep the transport usable."""
        await provider.connect()
        await provider.connect()
        try:
            accepted = await provider.send_audio(self.sample_audio_chunk())
            assert isinstance(accepted, bool), "send_audio() must return a bool"
            if self.expects_send_accepted_after_connect:
                assert accepted is True, "send_audio() after connect() must accept the chunk"
        finally:
            await provider.disconnect()

    async def test_concurrent_connect_callers_complete(self, provider: Any) -> None:
        """Concurrent connect callers share one usable lifecycle outcome."""
        async with asyncio.timeout(self.event_timeout):
            await asyncio.gather(provider.connect(), provider.connect())
        try:
            accepted = await provider.send_audio(self.sample_audio_chunk())
            assert isinstance(accepted, bool), "send_audio() must return a bool"
            if self.expects_send_accepted_after_connect:
                assert accepted is True, "send_audio() after connect() must accept the chunk"
        finally:
            await provider.disconnect()

    async def test_connect_send_disconnect_lifecycle(self, provider: Any) -> None:
        """connect → send → disconnect; the inbound stream then terminates."""
        await provider.connect()
        accepted = await provider.send_audio(self.sample_audio_chunk())
        assert isinstance(accepted, bool), "send_audio() must return a bool"
        if self.expects_send_accepted_after_connect:
            assert accepted is True, "send_audio() after connect() must accept the chunk"
        await provider.disconnect()
        received = await self.collect_events(
            provider.receive_audio(), source="Transport.receive_audio()"
        )
        for chunk in received:
            assert isinstance(chunk, AudioChunk)

    async def test_disconnect_terminates_an_active_receiver(self, provider: Any) -> None:
        """Disconnect wakes an iterator that is already waiting for inbound audio."""
        await provider.connect()
        received: list[Any] = []

        async def _collect() -> None:
            received.extend(
                await self.collect_events(
                    provider.receive_audio(), source="active Transport.receive_audio()"
                )
            )

        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(_collect())
            await asyncio.sleep(0)
            await provider.disconnect()
        for chunk in received:
            assert isinstance(chunk, AudioChunk)

    async def test_disconnect_is_idempotent(self, provider: Any) -> None:
        """Repeated disconnect calls remain safe and leave sends rejected."""
        await provider.connect()
        await provider.disconnect()
        await provider.disconnect()
        assert await provider.send_audio(self.sample_audio_chunk()) is False

    async def test_clear_audio_is_idempotent(self, provider: Any) -> None:
        clear_audio = getattr(provider, "clear_audio", None)
        if clear_audio is None:
            pytest.skip("transport does not buffer outbound audio (no clear_audio)")
        await clear_audio()
        await clear_audio()


class AgentBridgeContractSuite(ContractSuite):
    """Event-grammar and journal contract for ``ExternalAgentBridge`` implementations.

    ``provider_factory`` builds the bridge under test. Override
    ``expects_interruption_journal = False`` for bridges whose
    ``apply_interruption`` is a documented no-op (nothing to truncate, so no
    commit protocol records are journaled).
    """

    sample_user_text: ClassVar[str] = "hello there"
    interrupted_text: ClassVar[str] = "hel"
    expects_interruption_journal: ClassVar[bool] = True
    rotating_reset_snapshot_fields: ClassVar[frozenset[str]] = frozenset()

    @pytest.fixture
    def recorder(self) -> RecordingAgentRecorder:
        return RecordingAgentRecorder()

    async def run_turn(
        self, bridge: Any, recorder: RecordingAgentRecorder
    ) -> list[AgentBridgeEvent]:
        """Drive one full turn through ``invoke()`` and return its events."""
        return await self.collect_events(
            bridge.invoke(AgentTurnInput.from_text(self.sample_user_text), recorder),
            source="ExternalAgentBridge.invoke()",
        )

    async def settle_interruption(self, provider: Any) -> None:
        """Wait for framework-specific deferred persistence, when required.

        Synchronous bridges need no override. A bridge whose public
        ``apply_interruption()`` queues an async state write can override this
        hook in its contract-suite subclass and wait for that write through a
        public lifecycle boundary such as ``aclose()``.
        """
        del provider

    async def test_satisfies_external_agent_bridge_protocol(self, provider: Any) -> None:
        assert isinstance(provider, ExternalAgentBridge)

    async def test_invoke_stream_follows_event_grammar(
        self, provider: Any, recorder: RecordingAgentRecorder
    ) -> None:
        """The stream carries only grammar kinds and ends with exactly one done."""
        events = await self.run_turn(provider, recorder)
        assert events, "invoke() must yield at least one AgentBridgeEvent"
        for event in events:
            assert isinstance(event, AgentBridgeEvent), f"invoke() yielded {event!r}"
            assert event.kind in AGENT_BRIDGE_EVENT_KINDS, (
                f"invoke() yielded unknown event kind {event.kind!r}; the stream grammar "
                f"allows only {sorted(AGENT_BRIDGE_EVENT_KINDS)}"
            )
            if event.kind == "text_replace":
                assert event.part_index is not None, (
                    "text_replace must identify the complete text part it replaces"
                )
        text_modes = {
            "indexed" if event.part_index is not None else "flat"
            for event in events
            if event.kind in {"text_delta", "text_replace"}
        }
        assert len(text_modes) <= 1, (
            "one invoke() stream cannot mix indexed and unindexed text events"
        )
        kinds = [event.kind for event in events]
        assert kinds.count("done") == 1, "invoke() must yield exactly one done event"
        assert kinds[-1] == "done", "the done event must terminate the stream"

    async def test_snapshot_state_is_json_safe(
        self, provider: Any, recorder: RecordingAgentRecorder
    ) -> None:
        await self.run_turn(provider, recorder)
        snapshot = provider.snapshot_state()
        assert isinstance(snapshot, FrameworkStateSnapshot)
        json.dumps(snapshot.fields)

    async def test_consumer_close_balances_recorded_units(
        self, provider: Any, recorder: RecordingAgentRecorder
    ) -> None:
        """Closing after the first event synchronously exits recorder cursors."""
        stream = provider.invoke(AgentTurnInput.from_text(self.sample_user_text), recorder)
        try:
            async with asyncio.timeout(self.event_timeout):
                first_event = await stream.__anext__()
        except StopAsyncIteration:
            pytest.fail("invoke() ended before yielding its first event", pytrace=False)
        assert isinstance(first_event, AgentBridgeEvent)

        aclose = getattr(stream, "aclose", None)
        assert callable(aclose), "invoke() must return a consumer-closeable async iterator"
        async with asyncio.timeout(self.event_timeout):
            await aclose()

        entered = Counter(
            record[1][0].unit_id for record in recorder.records if record[0] == "unit_entered"
        )
        exited = Counter(
            record[1][0].unit_id for record in recorder.records if record[0] == "unit_exited"
        )
        assert entered, "invoke() must journal at least one execution unit"
        assert exited == entered, "consumer close must exit every entered execution unit"
        json.dumps(provider.snapshot_state().fields)

    async def test_apply_interruption_journals_the_commit_protocol(
        self, provider: Any, recorder: RecordingAgentRecorder
    ) -> None:
        """With a recorder, an interruption journals commit + boundary records."""
        await self.run_turn(provider, recorder)
        provider.apply_interruption(
            self.interrupted_text,
            CancellationMode.IMMEDIATE_STOP,
            recorder=recorder,
            caused_by_signal_id="sig-contract-kit",
        )
        if not self.expects_interruption_journal:
            return
        await self.settle_interruption(provider)
        kinds = recorder.kinds()
        assert "state_committed" in kinds, (
            "apply_interruption() must journal a state_committed record before mutating"
        )
        assert "cancellation_boundary" in kinds, (
            "apply_interruption() must journal a cancellation_boundary record"
        )

    async def test_reset_returns_bridge_to_a_json_safe_state(
        self, provider: Any, recorder: RecordingAgentRecorder
    ) -> None:
        await self.run_turn(provider, recorder)
        provider.reset()
        json.dumps(provider.snapshot_state().fields)

    async def test_reset_restores_the_initial_snapshot(
        self, provider: Any, recorder: RecordingAgentRecorder
    ) -> None:
        initial = provider.snapshot_state()
        assert isinstance(initial, FrameworkStateSnapshot)
        json.dumps(initial.fields)

        await self.run_turn(provider, recorder)
        provider.reset()

        reset = provider.snapshot_state()
        assert isinstance(reset, FrameworkStateSnapshot)
        json.dumps(reset.fields)
        rotating = self.rotating_reset_snapshot_fields
        initial_fields = dict(initial.fields)
        reset_fields = dict(reset.fields)
        for field_name in rotating:
            assert field_name in initial_fields, f"missing initial rotating field {field_name!r}"
            assert field_name in reset_fields, f"missing reset rotating field {field_name!r}"
            assert reset_fields.pop(field_name) != initial_fields.pop(field_name), (
                f"reset() must rotate declared isolation identity {field_name!r}"
            )
        assert reset.kind == initial.kind
        assert reset.state_ref == initial.state_ref
        assert reset_fields == initial_fields, (
            "reset() must restore every stable field to its fresh-session value"
        )
