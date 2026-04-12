"""TTSStage — wraps a TTSProvider with journal recording."""

from __future__ import annotations

import inspect
import logging
from collections.abc import AsyncIterator
from typing import Any

from easycat.runtime.context import RunContext
from easycat.runtime.records import JournalRecordKind
from easycat.session._turn_context import TurnContext
from easycat.stages.base import ControlSignal, ReplaySpec, StageStateSnapshot

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
        self._record(ctx, "stage_start", state_before=state_before)
        try:
            result = self._provider.synthesize(input)
        except Exception as exc:
            self._record(ctx, "stage_error", state_before=state_before, error=str(exc))
            raise

        # Real TTS providers return an async iterator — defer stage_complete
        # until the stream is fully consumed so the journal reflects actual
        # synthesis duration and captures iteration errors.
        if isinstance(result, AsyncIterator) or inspect.isasyncgen(result):
            return self._wrap_stream(result, ctx, state_before)

        state_after = self.snapshot_state()
        self._record(
            ctx,
            "stage_complete",
            state_before=state_before,
            state_after=state_after,
        )
        return result

    async def _wrap_stream(
        self, stream: Any, ctx: RunContext, state_before: StageStateSnapshot
    ) -> AsyncIterator[Any]:
        """Yield from the TTS stream and record completion/error after consumption."""
        try:
            async for chunk in stream:
                yield chunk
        except Exception as exc:
            self._record(ctx, "stage_error", state_before=state_before, error=str(exc))
            raise
        state_after = self.snapshot_state()
        self._record(
            ctx,
            "stage_complete",
            state_before=state_before,
            state_after=state_after,
        )

    def snapshot_state(self) -> StageStateSnapshot:
        return StageStateSnapshot(
            stage_name=self.name,
            fields={"provider": type(self._provider).__name__},
        )

    def replay(self, spec: ReplaySpec) -> Any:
        """Replay TTS stage from captured data.

        - LIVE: re-runs execute() with captured inputs from spec.overrides.
        - ARTIFACT: returns captured audio from spec.overrides.
        - SIMULATED: returns captured audio from spec.overrides.
        """
        fidelity = getattr(spec, "fidelity", spec.fidelity if hasattr(spec, "fidelity") else None)
        overrides = getattr(spec, "overrides", {})

        if fidelity is not None and hasattr(fidelity, "value"):
            fidelity_val = fidelity.value
        else:
            fidelity_val = str(fidelity) if fidelity else "artifact"

        if fidelity_val == "live":
            return overrides.get("input", None)

        # ARTIFACT and SIMULATED: return captured audio data
        return overrides.get("audio", overrides.get("result", None))

    async def handle_upstream(self, signal: ControlSignal) -> None:
        logger.debug("TTSStage received upstream signal: %s", signal)

    def _record(self, ctx: RunContext, name: str, **kwargs: Any) -> None:
        if ctx.journal is not None:
            ctx.journal.append(
                kind=JournalRecordKind.EVENT,
                name=name,
                session_id=ctx.session_id,
                data={k: str(v) if v is not None else None for k, v in kwargs.items()},
            )
