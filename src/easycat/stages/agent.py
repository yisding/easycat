"""AgentStage — wraps an agent (or ExternalAgentBridge) with journal recording."""

from __future__ import annotations

import logging
from typing import Any

from easycat.integrations.agents._factory import auto_adapt_agent
from easycat.runtime.context import RunContext
from easycat.runtime.records import JournalRecordKind
from easycat.session._turn_context import TurnContext
from easycat.stages.base import ControlSignal, ReplaySpec, StageStateSnapshot

logger = logging.getLogger(__name__)


class AgentStage:
    """Stage wrapper around an agent or ExternalAgentBridge."""

    name = "agent"

    def __init__(self, provider: Any, *, journal: Any = None) -> None:
        self._provider = auto_adapt_agent(provider)
        self._journal = journal
        self._last_snapshot = StageStateSnapshot(stage_name=self.name)

    async def execute(self, input: Any, ctx: RunContext, turn: TurnContext) -> Any:
        state_before = self.snapshot_state()
        self._record(ctx, "stage_start", turn_id=turn.id, state_before=state_before)
        try:
            if hasattr(self._provider, "_journal"):
                self._provider._journal = ctx.journal
            if hasattr(self._provider, "_session_id"):
                self._provider._session_id = ctx.session_id
            if hasattr(self._provider, "set_active_turn_id"):
                self._provider.set_active_turn_id(turn.id)
            result = await self._provider.run(input)
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
        return StageStateSnapshot(
            stage_name=self.name,
            fields={"provider": type(self._provider).__name__},
        )

    def replay(self, spec: ReplaySpec) -> Any:
        """Replay Agent stage from captured data.

        - LIVE: returns captured input for re-processing.
        - ARTIFACT: returns captured agent response from spec.overrides.
        - SIMULATED: returns captured events/response from spec.overrides.
        """
        fidelity = getattr(spec, "fidelity", spec.fidelity if hasattr(spec, "fidelity") else None)
        overrides = getattr(spec, "overrides", {})

        if fidelity is not None and hasattr(fidelity, "value"):
            fidelity_val = fidelity.value
        else:
            fidelity_val = str(fidelity) if fidelity else "artifact"

        if fidelity_val == "live":
            return overrides.get("input", None)

        if fidelity_val == "simulated":
            return overrides.get("events", overrides.get("result", None))

        # ARTIFACT
        return overrides.get("response", overrides.get("result", None))

    async def handle_upstream(self, signal: ControlSignal) -> None:
        logger.debug("AgentStage received upstream signal: %s", signal)

    def _record(
        self, ctx: RunContext, name: str, *, turn_id: str | None = None, **kwargs: Any
    ) -> None:
        if ctx.journal is not None:
            ctx.journal.append(
                kind=JournalRecordKind.EVENT,
                name=name,
                session_id=ctx.session_id,
                turn_id=turn_id,
                data={k: str(v) if v is not None else None for k, v in kwargs.items()},
            )
