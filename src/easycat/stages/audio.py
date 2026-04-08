"""AudioStage — wraps NoiseReducer + EchoCanceller with journal recording."""

from __future__ import annotations

import logging
from typing import Any

from easycat.runtime.context import RunContext
from easycat.runtime.records import JournalRecordKind
from easycat.session._turn_context import TurnContext
from easycat.stages.base import ControlSignal, ReplaySpec, StageStateSnapshot

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
        self._record(ctx, "stage_start", state_before=state_before)
        try:
            chunk = input
            if self._echo_canceller is not None:
                chunk = await self._echo_canceller.process(chunk)
            result = await self._provider.process(chunk)
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
        return result

    def snapshot_state(self) -> StageStateSnapshot:
        fields: dict[str, Any] = {
            "noise_reducer": type(self._provider).__name__,
        }
        if self._echo_canceller is not None:
            fields["echo_canceller"] = type(self._echo_canceller).__name__
        return StageStateSnapshot(stage_name=self.name, fields=fields)

    def replay(self, spec: ReplaySpec) -> Any:
        raise NotImplementedError("Replay not implemented until WS4")

    async def handle_upstream(self, signal: ControlSignal) -> None:
        logger.debug("AudioStage received upstream signal: %s", signal)

    def _record(self, ctx: RunContext, name: str, **kwargs: Any) -> None:
        if ctx.journal is not None:
            ctx.journal.append(
                kind=JournalRecordKind.EVENT,
                name=name,
                session_id=ctx.session_id,
                data={k: str(v) if v is not None else None for k, v in kwargs.items()},
            )
