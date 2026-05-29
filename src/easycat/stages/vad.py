"""VADStage — wraps a VADProvider with journal recording and input capture."""

from __future__ import annotations

import dataclasses
import logging
import time
from typing import Any

from easycat import _observability as observability
from easycat._audio_utils import to_mono_chunk
from easycat._turn_context import TurnContext
from easycat.audio_format import AudioChunk
from easycat.runtime.context import RunContext
from easycat.runtime.replay import ReplayCassette, ReplayFidelity, ReplaySpec
from easycat.stages.base import (
    ControlSignal,
    StageStateSnapshot,
    audio_format_fields,
    journal_append_control_signal,
    journal_append_event,
    put_artifact,
)

logger = logging.getLogger(__name__)


def _serialize_event(event: Any) -> dict[str, Any]:
    """Serialize a VAD event into a replay-reconstructable descriptor.

    Records the event's class name under ``type`` and, when the event is
    a dataclass, its fields (timestamps, correlation ids, speech/silence
    boundaries, probabilities) so ARTIFACT replay can rebuild the event
    rather than recovering only its class name.
    """
    descriptor: dict[str, Any] = {"type": type(event).__name__}
    if dataclasses.is_dataclass(event) and not isinstance(event, type):
        descriptor["fields"] = dataclasses.asdict(event)
    return descriptor


class VADStage:
    """Stage wrapper around a :class:`VADProvider`.

    ``execute`` runs the provider on a single chunk, captures the raw
    input bytes as an ``input_ref`` artifact for LIVE-fidelity replay,
    and returns the list of events emitted.  ``stage_complete`` carries
    a serialized descriptor per event in ``data["events"]`` — each a
    dict of the event's ``type`` plus its dataclass fields (timestamps,
    correlation ids, etc.) — so ARTIFACT replay can reconstruct the VAD
    output without re-running the model.
    """

    name = "vad"

    _MAX_EVENTS_PER_CHUNK: int = 64

    def __init__(self, provider: Any, *, journal: Any = None) -> None:
        self._provider = provider
        # Fallback recording sink used only when ``ctx.journal`` is None
        # (see ``_journal_ctx``); recording normally flows through ctx.
        self._journal = journal
        # Most recent VAD decision (last emitted event type), surfaced via
        # ``snapshot_state`` so ``replay_decision`` returns something real.
        self._last_decision: Any = None

    def _journal_ctx(self, ctx: RunContext) -> RunContext:
        """Return *ctx*, substituting the constructor journal as a fallback."""
        if ctx.journal is None and self._journal is not None:
            return dataclasses.replace(ctx, journal=self._journal)
        return ctx

    async def execute(self, input: Any, ctx: RunContext, turn: TurnContext) -> Any:
        ctx = self._journal_ctx(ctx)
        started = time.perf_counter()
        result_attr = "pass"
        state_before = self.snapshot_state()
        data_bytes = getattr(input, "data", None) if not isinstance(input, bytes) else input
        input_ref = put_artifact(ctx, data_bytes)
        # VAD backends decode the raw byte stream as flat int16 mono (frame
        # boundaries are computed as samples*2). Interleaved multi-channel
        # input would be misread as garbage, so downmix to mono before
        # handing the chunk to the provider. We do this after capturing the
        # input_ref artifact so replay still reflects the true raw input.
        if isinstance(input, AudioChunk) and input.format.channels > 1:
            input = to_mono_chunk(input)
        start_extra = {
            "audio_bytes": len(data_bytes) if isinstance(data_bytes, (bytes, bytearray)) else 0,
        }
        start_extra.update(audio_format_fields(input))
        journal_append_event(
            ctx,
            stage=self.name,
            name="stage_start",
            turn_id=turn.id,
            state_before=state_before,
            input_ref=input_ref,
            data_extra=start_extra,
        )
        try:
            with observability.span(
                "easycat.vad.detect",
                {"easycat.stage": self.name, "easycat.surface": "stt"},
            ):
                events: list[Any] = []
                async for event in self._provider.process(input):
                    events.append(event)
                    if len(events) >= self._MAX_EVENTS_PER_CHUNK:
                        logger.warning(
                            "VAD provider yielded too many events for a single chunk; truncating"
                        )
                        break
                result = events
        except Exception as exc:
            result_attr = "fail"
            observability.increment_counter(
                "easycat.provider.errors.total",
                attributes={
                    "easycat.surface": "stt",
                    "easycat.provider": type(self._provider).__name__.lower(),
                    "easycat.error_type": type(exc).__name__,
                },
            )
            journal_append_event(
                ctx,
                stage=self.name,
                name="stage_error",
                turn_id=turn.id,
                state_before=state_before,
                error=str(exc),
            )
            raise
        finally:
            observability.record_histogram(
                "easycat.stage.latency",
                time.perf_counter() - started,
                {"easycat.stage": self.name, "easycat.result": result_attr},
            )
        if events:
            self._last_decision = type(events[-1]).__name__
        state_after = self.snapshot_state()
        journal_append_event(
            ctx,
            stage=self.name,
            name="stage_complete",
            turn_id=turn.id,
            state_before=state_before,
            state_after=state_after,
            data_extra={"events": [_serialize_event(e) for e in events]},
        )
        return result

    def snapshot_state(self) -> StageStateSnapshot:
        fields: dict[str, Any] = {"provider": type(self._provider).__name__}
        if self._last_decision is not None:
            fields["decision"] = self._last_decision
        return StageStateSnapshot(stage_name=self.name, fields=fields)

    def replay(
        self,
        spec: ReplaySpec,
        cassette: ReplayCassette | None = None,
    ) -> Any:
        """Replay VAD stage.

        ``ARTIFACT`` returns the captured frame events from the
        cassette (or ``spec.overrides["events"]``).  ``LIVE`` returns
        the captured input audio for re-execution against a fresh VAD
        backend.
        """
        overrides = spec.overrides
        if spec.fidelity is ReplayFidelity.LIVE:
            if "input" in overrides:
                return overrides["input"]
            if cassette is not None:
                record = cassette.last_record("stage_start") or cassette.last_record()
                if record is not None:
                    blob = cassette.blob(record.get("input_ref"))
                    if blob is not None:
                        return blob
            return None

        if "events" in overrides or "result" in overrides:
            return overrides.get("events", overrides.get("result", []))
        if cassette is not None:
            record = cassette.last_record("stage_complete") or cassette.last_record()
            if record is not None:
                data = record.get("data") or {}
                if isinstance(data, dict):
                    for key in ("events", "result"):
                        if key in data:
                            return data[key]
        return []

    def replay_decision(self, snapshot: StageStateSnapshot) -> Any:
        """Replay the last VAD decision recorded in a snapshot.

        ``snapshot_state`` carries the most recent emitted event type under
        ``fields["decision"]`` once the stage has run, so this returns that
        verdict (or ``None`` for a snapshot taken before any events).
        """
        return snapshot.fields.get("decision", None)

    async def handle_upstream(
        self,
        signal: ControlSignal,
        ctx: RunContext | None = None,
    ) -> None:
        """Observe and journal an upstream control signal (no cancel here).

        Cancellation is owned by the ``CancelOrchestrator`` / turn runner;
        this method only records that the stage saw the signal.
        """
        logger.debug("VADStage received upstream signal: %s", signal)
        if ctx is not None:
            journal_append_control_signal(self._journal_ctx(ctx), stage=self.name, signal=signal)
