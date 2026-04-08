"""TurnStage — wraps SmartTurn with journal recording."""

from __future__ import annotations

import logging
from typing import Any

from easycat.runtime.context import RunContext
from easycat.runtime.records import JournalRecordKind
from easycat.session._turn_context import TurnContext
from easycat.stages.base import ControlSignal, ReplaySpec, StageStateSnapshot

logger = logging.getLogger(__name__)


class TurnStage:
    """Stage wrapper around :class:`SmartTurnProvider`."""

    name = "turn"

    def __init__(self, provider: Any, *, journal: Any = None) -> None:
        self._provider = provider
        self._journal = journal
        self._last_snapshot = StageStateSnapshot(stage_name=self.name)

    async def execute(self, input: Any, ctx: RunContext, turn: TurnContext) -> Any:
        state_before = self.snapshot_state()
        self._record(ctx, "stage_start", state_before=state_before)
        try:
            result = await self._provider.detect(input)
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
        return StageStateSnapshot(
            stage_name=self.name,
            fields={"provider": type(self._provider).__name__},
        )

    def replay(self, spec: ReplaySpec) -> Any:
        raise NotImplementedError("Replay not implemented until WS4")

    def replay_decision(self, snapshot: StageStateSnapshot) -> Any:
        """Replay a turn decision from a snapshot.  Stub until WS4."""
        raise NotImplementedError("replay_decision not implemented until WS4")

    async def handle_upstream(self, signal: ControlSignal) -> None:
        logger.debug("TurnStage received upstream signal: %s", signal)

    def _record(self, ctx: RunContext, name: str, **kwargs: Any) -> None:
        if ctx.journal is not None:
            ctx.journal.append(
                kind=JournalRecordKind.EVENT,
                name=name,
                session_id=ctx.session_id,
                data={k: str(v) if v is not None else None for k, v in kwargs.items()},
            )
