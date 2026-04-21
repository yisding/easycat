"""TurnStage — wraps SmartTurn with journal recording and capture."""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from easycat.runtime.context import RunContext
from easycat.runtime.replay import ReplayCassette, ReplayFidelity, ReplaySpec
from easycat.session._turn_context import TurnContext
from easycat.stages.base import (
    ControlSignal,
    StageStateSnapshot,
    journal_append_control_signal,
    journal_append_event,
    put_artifact,
)

logger = logging.getLogger(__name__)


class TurnStage:
    """Stage wrapper around :class:`SmartTurnProvider`.

    ``execute`` runs SmartTurn's endpoint classifier on an audio window.
    The raw input audio is captured as ``input_ref`` on ``stage_start``
    so a LIVE replay can re-run the same classifier against the same
    window.  The classifier's decision + probability are recorded on
    ``stage_complete`` so ARTIFACT replay returns the original verdict
    without loading the ONNX model.
    """

    name = "turn"

    def __init__(self, provider: Any, *, journal: Any = None) -> None:
        self._provider = provider
        self._journal = journal
        self._last_snapshot = StageStateSnapshot(stage_name=self.name)

    async def execute(self, input: Any, ctx: RunContext, turn: TurnContext) -> Any:
        state_before = self.snapshot_state()
        audio_bytes = _concat_chunks(input)
        input_ref = put_artifact(ctx, audio_bytes)
        journal_append_event(
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
            result = await self._provider.detect(input)
        except Exception as exc:
            journal_append_event(
                ctx,
                stage=self.name,
                name="stage_error",
                turn_id=turn.id,
                state_before=state_before,
                error=str(exc),
            )
            raise
        state_after = self.snapshot_state()
        complete_extra: dict[str, Any] = {}
        if isinstance(result, dict):
            source: dict[str, Any] = dict(result)
        elif dataclasses.is_dataclass(result):
            source = dataclasses.asdict(result)
        else:
            source = {}
        for key in ("prediction", "probability", "decision"):
            if key in source:
                complete_extra[key] = source[key]
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
        return StageStateSnapshot(
            stage_name=self.name,
            fields={"provider": type(self._provider).__name__},
        )

    def replay(
        self,
        spec: ReplaySpec,
        cassette: ReplayCassette | None = None,
    ) -> Any:
        """Replay Turn (SmartTurn) stage.

        ``ARTIFACT`` returns the captured classification/decision.
        ``LIVE`` returns the captured audio window so SmartTurn can be
        re-run against the same snapshot.
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

        if "decision" in overrides or "result" in overrides:
            return overrides.get("decision", overrides.get("result"))
        if cassette is not None:
            record = cassette.last_record("stage_complete") or cassette.last_record()
            if record is not None:
                data = record.get("data") or {}
                if isinstance(data, dict):
                    for key in ("decision", "result", "prediction"):
                        if key in data:
                            return data[key]
        return None

    def replay_decision(self, snapshot: StageStateSnapshot) -> Any:
        """Replay a turn decision from a snapshot."""
        return snapshot.fields.get("decision", None)

    async def handle_upstream(
        self,
        signal: ControlSignal,
        ctx: RunContext | None = None,
    ) -> None:
        logger.debug("TurnStage received upstream signal: %s", signal)
        if ctx is not None:
            journal_append_control_signal(ctx, stage=self.name, signal=signal)


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
