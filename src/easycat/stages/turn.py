"""TurnStage — wraps SmartTurn with journal recording and capture."""

from __future__ import annotations

import dataclasses
import logging
import time
from typing import Any

from easycat import _observability as observability
from easycat._turn_context import TurnContext
from easycat.runtime.context import RunContext
from easycat.runtime.replay import ReplayCassette, ReplayFidelity, ReplaySpec
from easycat.stages.base import (
    ControlSignal,
    StageStateSnapshot,
    audio_input_capture_allowed,
    journal_append_control_signal,
    journal_append_event,
    journal_ctx,
    live_replay_input,
    put_artifact_async,
    record_stage_failure,
)

logger = logging.getLogger(__name__)


class TurnStage:
    """Stage wrapper around :class:`SmartTurnProvider`.

    ``execute`` runs SmartTurn's endpoint classifier on an audio window.
    The raw input audio is captured as ``input_ref`` on ``stage_start``
    so a LIVE replay can re-run the same classifier against the same
    window.  The classifier's ``prediction`` + ``probability`` (the two
    fields of :class:`SmartTurnResult`) are recorded on ``stage_complete``
    so ARTIFACT replay returns the original verdict without loading the
    ONNX model.
    """

    name = "turn"

    def __init__(self, provider: Any, *, journal: Any = None) -> None:
        self._provider = provider
        self._journal = journal
        # Most recent endpoint decision (last classifier ``prediction``),
        # surfaced via ``snapshot_state`` so ``replay_decision`` is real.
        self._last_decision: Any = None

    async def execute(self, input: Any, ctx: RunContext, turn: TurnContext) -> Any:
        ctx = journal_ctx(ctx, self._journal)
        started = time.perf_counter()
        result_attr = "pass"
        state_before = self.snapshot_state()
        provider_input = _materialize_one_shot_iterable(input)
        audio_bytes = _concat_chunks(provider_input)
        input_ref = await put_artifact_async(
            ctx,
            audio_bytes,
            capture_allowed=audio_input_capture_allowed(ctx, provider_input),
        )
        start_sequence = journal_append_event(
            ctx,
            stage=self.name,
            name="stage_start",
            turn_id=turn.id,
            state_before=state_before,
            input_ref=input_ref,
            data_extra={
                "audio_bytes": len(audio_bytes) if audio_bytes else 0,
            },
        )
        try:
            result = await self._provider.detect(provider_input)
        except Exception as exc:
            result_attr = "fail"
            elapsed_ms = (time.perf_counter() - started) * 1000
            record_stage_failure(
                exc,
                ctx,
                stage=self.name,
                provider=type(self._provider).__name__.lower(),
                surface="stt",
                elapsed_ms=elapsed_ms,
                sequence=start_sequence,
                turn_id=turn.id,
                state_before=state_before,
            )
            raise
        finally:
            observability.record_histogram(
                "easycat.stage.latency",
                time.perf_counter() - started,
                {"easycat.stage": self.name, "easycat.result": result_attr},
            )
        complete_extra: dict[str, Any] = {}
        if isinstance(result, dict):
            source: dict[str, Any] = dict(result)
        elif dataclasses.is_dataclass(result) and not isinstance(result, type):
            source = dataclasses.asdict(result)
        else:
            source = {}
        for key in ("prediction", "probability"):
            if key in source:
                complete_extra[key] = source[key]
        if "prediction" in source:
            self._last_decision = source["prediction"]
        state_after = self.snapshot_state()
        complete_extra["elapsed_ms"] = (time.perf_counter() - started) * 1000
        journal_append_event(
            ctx,
            stage=self.name,
            name="stage_complete",
            turn_id=turn.id,
            state_before=state_before,
            state_after=state_after,
            data_extra=complete_extra,
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
        """Replay Turn (SmartTurn) stage.

        ``ARTIFACT`` returns the captured classification (the recorded
        ``prediction``).  ``LIVE`` returns the captured audio window so
        SmartTurn can be re-run against the same snapshot.
        """
        overrides = spec.overrides
        if spec.fidelity is ReplayFidelity.LIVE:
            return live_replay_input(spec, cassette)

        if "prediction" in overrides or "result" in overrides:
            return overrides.get("prediction", overrides.get("result"))
        if cassette is not None:
            record = cassette.last_record("stage_complete") or cassette.last_record()
            if record is not None:
                data = record.get("data") or {}
                if isinstance(data, dict):
                    for key in ("prediction", "result"):
                        if key in data:
                            return data[key]
        return None

    def replay_decision(self, snapshot: StageStateSnapshot) -> Any:
        """Replay the last endpoint decision recorded in a snapshot.

        ``snapshot_state`` carries the most recent classifier ``prediction``
        under ``fields["decision"]`` once the stage has run, so this returns
        that verdict (or ``None`` for a snapshot taken before any run).
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
        logger.debug("TurnStage received upstream signal: %s", signal)
        if ctx is not None:
            journal_append_control_signal(
                journal_ctx(ctx, self._journal), stage=self.name, signal=signal
            )


def _concat_chunks(input_: Any) -> bytes:
    """Flatten the smart-turn input into a single byte string.

    SmartTurn is typically fed an iterable of ``AudioChunk``s but some
    callers hand over a single chunk or raw ``bytes``; accept all three
    shapes without forcing the caller into a normaliser.
    """
    if input_ is None:
        return b""
    if isinstance(input_, (bytes, bytearray)):
        return bytes(input_)
    data = getattr(input_, "data", None)
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    try:
        pieces: list[bytes] = []
        for item in input_:
            chunk_data = getattr(item, "data", None)
            if isinstance(chunk_data, (bytes, bytearray)):
                pieces.append(bytes(chunk_data))
            elif isinstance(item, (bytes, bytearray)):
                pieces.append(bytes(item))
        return b"".join(pieces)
    except TypeError:
        return b""


def _materialize_one_shot_iterable(input_: Any) -> Any:
    """Snapshot iterator inputs once so capture and detection see identical audio."""
    try:
        iterator = iter(input_)
    except TypeError:
        return input_
    if iterator is input_:
        return list(iterator)
    return input_
