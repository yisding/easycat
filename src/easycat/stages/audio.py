"""AudioStage — wraps NoiseReducer + EchoCanceller with journal recording."""

from __future__ import annotations

import logging
from typing import Any

from easycat.runtime.context import RunContext
from easycat.runtime.replay import ReplayCassette, ReplayFidelity, ReplaySpec
from easycat.session._turn_context import TurnContext
from easycat.stages.base import (
    ControlSignal,
    StageStateSnapshot,
    audio_format_fields,
    journal_append_control_signal,
    journal_append_event,
    put_artifact,
)

logger = logging.getLogger(__name__)


class AudioStage:
    """Stage wrapper around :class:`NoiseReducer` and :class:`EchoCanceller`.

    ``execute`` feeds the input chunk through the NR + AEC chain; it
    records the raw input bytes as ``input_ref`` on ``stage_start`` and
    the processed output as ``output_ref`` on ``stage_complete`` so
    LIVE replay can re-drive a fresh NR backend and ARTIFACT replay can
    skip processing entirely.
    """

    name = "audio"

    def __init__(
        self,
        provider: Any,
        *,
        echo_canceller: Any = None,
        journal: Any = None,
    ) -> None:
        self._provider = provider
        self._echo_canceller = echo_canceller
        self._journal = journal
        self._last_snapshot = StageStateSnapshot(stage_name=self.name)

    async def execute(self, input: Any, ctx: RunContext, turn: TurnContext) -> Any:
        state_before = self.snapshot_state()
        raw_bytes = getattr(input, "data", None)
        input_ref = put_artifact(ctx, raw_bytes)
        start_extra = {
            "audio_bytes": len(raw_bytes) if isinstance(raw_bytes, (bytes, bytearray)) else 0,
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
            chunk = input
            chunk = await self._provider.process(chunk)
            if self._echo_canceller is not None:
                chunk = await self._echo_canceller.process(chunk)
            result = chunk
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
        processed_bytes = getattr(result, "data", None)
        output_ref = put_artifact(ctx, processed_bytes)
        complete_extra = {
            "audio_bytes": (
                len(processed_bytes) if isinstance(processed_bytes, (bytes, bytearray)) else 0
            ),
        }
        complete_extra.update(audio_format_fields(result))
        journal_append_event(
            ctx,
            stage=self.name,
            name="stage_complete",
            turn_id=turn.id,
            state_before=state_before,
            state_after=state_after,
            output_ref=output_ref,
            data_extra=complete_extra,
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

    async def handle_upstream(
        self,
        signal: ControlSignal,
        ctx: RunContext | None = None,
    ) -> None:
        logger.debug("AudioStage received upstream signal: %s", signal)
        if ctx is not None:
            journal_append_control_signal(ctx, stage=self.name, signal=signal)
