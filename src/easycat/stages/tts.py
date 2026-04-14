"""TTSStage — wraps a TTSProvider with journal recording."""

from __future__ import annotations

import inspect
import logging
from collections.abc import AsyncIterator
from typing import Any

from easycat.runtime.context import RunContext
from easycat.runtime.records import JournalRecordKind
from easycat.runtime.replay import ReplayCassette, ReplayFidelity, ReplaySpec
from easycat.session._turn_context import TurnContext
from easycat.stages.base import ControlSignal, StageStateSnapshot

logger = logging.getLogger(__name__)


class TTSStage:
    """Stage wrapper around a :class:`TTSProvider`."""

    name = "tts"

    def __init__(self, provider: Any, *, journal: Any = None) -> None:
        self._provider = provider
        self._journal = journal
        self._last_snapshot = StageStateSnapshot(stage_name=self.name)

    async def execute(self, input: Any, ctx: RunContext, turn: TurnContext) -> Any:
        state_before = self.snapshot_state()
        self._record(ctx, "stage_start", turn_id=turn.id, state_before=state_before)
        try:
            result = self._provider.synthesize(input)
        except Exception as exc:
            self._record(
                ctx, "stage_error", turn_id=turn.id, state_before=state_before, error=str(exc)
            )
            raise

        # Real TTS providers return an async iterator — defer stage_complete
        # until the stream is fully consumed so the journal reflects actual
        # synthesis duration and captures iteration errors.
        if isinstance(result, AsyncIterator) or inspect.isasyncgen(result):
            return self._wrap_stream(result, ctx, turn.id, state_before)

        state_after = self.snapshot_state()
        self._record(
            ctx,
            "stage_complete",
            turn_id=turn.id,
            state_before=state_before,
            state_after=state_after,
        )
        return result

    async def _wrap_stream(
        self, stream: Any, ctx: RunContext, turn_id: str, state_before: StageStateSnapshot
    ) -> AsyncIterator[Any]:
        """Yield from the TTS stream and record completion/error after consumption."""
        try:
            async for chunk in stream:
                yield chunk
        except Exception as exc:
            self._record(
                ctx, "stage_error", turn_id=turn_id, state_before=state_before, error=str(exc)
            )
            raise
        state_after = self.snapshot_state()
        self._record(
            ctx,
            "stage_complete",
            turn_id=turn_id,
            state_before=state_before,
            state_after=state_after,
        )

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
        """Replay TTS stage.

        ``ARTIFACT``/``SIMULATED`` returns the captured audio bytes
        (either from ``spec.overrides["audio"]`` or from the cassette's
        last ``stage_complete`` output ref).  ``LIVE`` returns the
        captured input text so the caller can re-run synthesis on a
        fresh provider.
        """
        overrides = spec.overrides
        if spec.fidelity is ReplayFidelity.LIVE:
            if "input" in overrides:
                return overrides["input"]
            if cassette is not None:
                record = cassette.last_record("stage_start") or cassette.last_record()
                if record is not None:
                    data = record.get("data") or {}
                    if isinstance(data, dict) and "input" in data:
                        return data["input"]
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
                    for key in ("audio", "audio_bytes", "result"):
                        if key in data:
                            return data[key]
        return None

    async def handle_upstream(self, signal: ControlSignal) -> None:
        logger.debug("TTSStage received upstream signal: %s", signal)

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
