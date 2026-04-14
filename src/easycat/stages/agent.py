"""AgentStage — wraps an agent (or ExternalAgentBridge) with journal recording."""

from __future__ import annotations

import logging
from typing import Any

from easycat.integrations.agents._factory import auto_adapt_agent
from easycat.runtime.context import RunContext
from easycat.runtime.records import JournalRecordKind
from easycat.runtime.replay import ReplayCassette, ReplayFidelity, ReplaySpec
from easycat.session._turn_context import TurnContext
from easycat.stages.base import ControlSignal, StageStateSnapshot

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
            if hasattr(self._provider, "_artifact_store") and ctx.artifact_store is not None:
                self._provider._artifact_store = ctx.artifact_store
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

    def replay(
        self,
        spec: ReplaySpec,
        cassette: ReplayCassette | None = None,
    ) -> Any:
        """Replay Agent stage.

        ``ARTIFACT`` returns the captured final response.  ``SIMULATED``
        returns the sequence of captured bridge events so downstream
        stages can be driven without calling the live LLM.  ``LIVE``
        returns the captured user input so the caller can re-run the
        bridge on a fresh agent.
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

        if spec.fidelity is ReplayFidelity.SIMULATED:
            if "events" in overrides or "result" in overrides:
                return overrides.get("events", overrides.get("result"))
            if cassette is not None:
                events = [
                    r.get("data") for r in cassette.records if r.get("name") == "bridge_event"
                ]
                if events:
                    return events
            return None

        # ARTIFACT
        if "response" in overrides or "result" in overrides:
            return overrides.get("response", overrides.get("result"))
        if cassette is not None:
            record = cassette.last_record("stage_complete") or cassette.last_record()
            if record is not None:
                data = record.get("data") or {}
                if isinstance(data, dict):
                    for key in ("response", "text", "result"):
                        if key in data:
                            return data[key]
                blob = cassette.blob(record.get("output_ref"))
                if blob is not None:
                    return blob
        return None

    async def handle_upstream(self, signal: ControlSignal) -> None:
        logger.debug("AgentStage received upstream signal: %s", signal)

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
