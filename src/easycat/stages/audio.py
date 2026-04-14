"""AudioStage — wraps NoiseReducer + EchoCanceller with journal recording."""

from __future__ import annotations

import logging
from typing import Any

from easycat.runtime.context import RunContext
from easycat.runtime.records import JournalRecordKind
from easycat.runtime.replay import ReplayCassette, ReplayFidelity, ReplaySpec
from easycat.session._turn_context import TurnContext
from easycat.stages.base import ControlSignal, StageStateSnapshot

logger = logging.getLogger(__name__)


class AudioStage:
    """Stage wrapper around :class:`NoiseReducer` and :class:`EchoCanceller`."""

    name = "audio"

    def __init__(
        self,
        provider: Any,
        *,
        echo_canceller: Any = None,
        journal: Any = None,
    ) -> None:
        self._provider = provider  # NoiseReducer
        self._echo_canceller = echo_canceller
        self._journal = journal
        self._last_snapshot = StageStateSnapshot(stage_name=self.name)

    async def execute(self, input: Any, ctx: RunContext, turn: TurnContext) -> Any:
        state_before = self.snapshot_state()
        self._record(ctx, "stage_start", turn_id=turn.id, state_before=state_before)
        try:
            chunk = input
            chunk = await self._provider.process(chunk)
            if self._echo_canceller is not None:
                chunk = await self._echo_canceller.process(chunk)
            result = chunk
        except Exception as exc:
            self._record(
                ctx, "stage_error", turn_id=turn.id, state_before=state_before, error=str(exc)
            )
            raise
        state_after = self.snapshot_state()
        self._record(
            ctx,
            "stage_complete",
            turn_id=turn.id,
            state_before=state_before,
            state_after=state_after,
        )
        return result

    def snapshot_state(self) -> StageStateSnapshot:
        fields: dict[str, Any] = {
            "noise_reducer": type(self._provider).__name__,
        }
        if self._echo_canceller is not None:
            fields["echo_canceller"] = type(self._echo_canceller).__name__
        return StageStateSnapshot(stage_name=self.name, fields=fields)

    def replay(
        self,
        spec: ReplaySpec,
        cassette: ReplayCassette | None = None,
    ) -> Any:
        """Replay Audio (NR/AEC) stage.

        ``ARTIFACT`` returns the captured processed audio from the
        cassette's output ref.  ``LIVE`` returns the raw input bytes so
        the caller can re-run the NR/AEC pipeline against a backend at
        the same version.
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

        if "audio" in overrides or "result" in overrides:
            return overrides.get("audio", overrides.get("result"))
        if cassette is not None:
            record = cassette.last_record("stage_complete") or cassette.last_record()
            if record is not None:
                blob = cassette.blob(record.get("output_ref"))
                if blob is not None:
                    return blob
                data = record.get("data") or {}
                if isinstance(data, dict):
                    for key in ("audio", "result"):
                        if key in data:
                            return data[key]
        return None

    async def handle_upstream(self, signal: ControlSignal) -> None:
        logger.debug("AudioStage received upstream signal: %s", signal)

    def _record(
        self, ctx: RunContext, name: str, *, turn_id: str | None = None, **kwargs: Any
    ) -> None:
        if ctx.journal is not None:
            payload = {k: str(v) if v is not None else None for k, v in kwargs.items()}
            payload["stage"] = self.name
            ctx.journal.append(
                kind=JournalRecordKind.EVENT,
                name=name,
                session_id=ctx.session_id,
                turn_id=turn_id,
                data=payload,
            )
