"""AgentStage — wraps an agent (or ExternalAgentBridge) with journal recording."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from easycat.integrations.agents._factory import auto_adapt_agent
from easycat.runtime.context import RunContext
from easycat.runtime.replay import ReplayCassette, ReplayFidelity, ReplaySpec
from easycat.session._turn_context import TurnContext
from easycat.stages.base import (
    ControlSignal,
    StageStateSnapshot,
    journal_append_control_signal,
    journal_append_event,
)

logger = logging.getLogger(__name__)


class AgentStage:
    """Stage wrapper around an agent or ExternalAgentBridge.

    ``execute`` handles non-streaming agents: one ``stage_start`` with
    the input transcript, one ``stage_complete`` with the final text
    response.  ``execute_streaming`` covers the iterator path: same
    start/complete envelope plus one ``agent_delta`` record per
    streamed text event, so replays can reconstruct both the final
    response and the delta timeline.
    """

    name = "agent"

    def __init__(self, provider: Any, *, journal: Any = None) -> None:
        self._provider = auto_adapt_agent(provider)
        self._journal = journal
        self._last_snapshot = StageStateSnapshot(stage_name=self.name)

    def _wire_provider_debug(self, ctx: RunContext, turn: TurnContext) -> None:
        """Push journal/session/artifact/turn-id into bridge-backed providers.

        Bridges like ``BridgeAdapterShim`` expose these attributes so they
        can emit their own rich journal events; the stage mirrors Session's
        historical wiring so nothing downstream notices the new layer.
        """
        if hasattr(self._provider, "_journal"):
            self._provider._journal = ctx.journal
        if hasattr(self._provider, "_session_id"):
            self._provider._session_id = ctx.session_id
        if hasattr(self._provider, "_artifact_store") and ctx.artifact_store is not None:
            self._provider._artifact_store = ctx.artifact_store
        if hasattr(self._provider, "set_active_turn_id"):
            self._provider.set_active_turn_id(turn.id)

    async def execute(self, input: Any, ctx: RunContext, turn: TurnContext) -> Any:
        state_before = self.snapshot_state()
        self._wire_provider_debug(ctx, turn)
        journal_append_event(
            ctx,
            stage=self.name,
            name="stage_start",
            turn_id=turn.id,
            state_before=state_before,
            data_extra={"input": input if isinstance(input, str) else str(input)},
        )
        try:
            result = await self._provider.run(input)
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
        journal_append_event(
            ctx,
            stage=self.name,
            name="stage_complete",
            turn_id=turn.id,
            state_before=state_before,
            state_after=state_after,
            data_extra={"response": result if isinstance(result, str) else str(result)},
        )
        return result

    async def execute_streaming(
        self,
        input: Any,
        ctx: RunContext,
        turn: TurnContext,
        *,
        cancel_token: Any | None = None,
    ) -> AsyncIterator[Any]:
        """Streaming counterpart of ``execute``.

        Forwards ``run_streaming`` events to the caller while journaling
        a ``stage_start``, per-event ``agent_delta`` / ``agent_tool_*``
        markers, and a final ``stage_complete`` carrying the accumulated
        text response.  Session's streaming consumer keeps driving the
        event loop; the stage just observes.
        """
        state_before = self.snapshot_state()
        self._wire_provider_debug(ctx, turn)
        journal_append_event(
            ctx,
            stage=self.name,
            name="stage_start",
            turn_id=turn.id,
            state_before=state_before,
            data_extra={"input": input if isinstance(input, str) else str(input)},
        )
        accumulated: list[str] = []
        try:
            async for event in self._provider.run_streaming(input, cancel_token=cancel_token):
                etype = getattr(event, "type", None)
                etype_name = getattr(etype, "name", None) or str(etype)
                text = getattr(event, "text", None)
                if text:
                    journal_append_event(
                        ctx,
                        stage=self.name,
                        name="agent_delta",
                        turn_id=turn.id,
                        data_extra={"type": etype_name, "text": text},
                    )
                    if etype_name == "TEXT_DELTA":
                        accumulated.append(text)
                yield event
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
        journal_append_event(
            ctx,
            stage=self.name,
            name="stage_complete",
            turn_id=turn.id,
            state_before=state_before,
            state_after=state_after,
            data_extra={"response": "".join(accumulated)},
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
                    r.get("data") for r in cassette.records if r.get("name") == "agent_delta"
                ]
                if not events:
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

    async def handle_upstream(
        self,
        signal: ControlSignal,
        ctx: RunContext | None = None,
    ) -> None:
        logger.debug("AgentStage received upstream signal: %s", signal)
        if ctx is not None:
            journal_append_control_signal(ctx, stage=self.name, signal=signal)
