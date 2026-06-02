"""AgentStage — wraps an :class:`ExternalAgentBridge` with journal recording."""

from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from easycat import _observability as observability
from easycat._turn_context import TurnContext
from easycat.integrations.agents._agent_runner import AgentRunner
from easycat.integrations.agents._factory import auto_adapt_agent
from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentTurnInput,
    ExternalAgentBridge,
    RecorderContext,
)
from easycat.runtime.context import RunContext
from easycat.runtime.replay import ReplayCassette, ReplayFidelity, ReplaySpec
from easycat.stages.base import (
    ControlSignal,
    StageStateSnapshot,
    journal_append_control_signal,
    journal_append_event,
)

logger = logging.getLogger(__name__)


class AgentStage:
    """Stage wrapper around an :class:`ExternalAgentBridge`.

    ``execute_streaming`` drives ``bridge.invoke()`` and journals a
    ``stage_start``, per-event ``agent_delta`` / ``agent_tool_*`` marker,
    and a final ``stage_complete`` carrying the accumulated text response.
    Session's streaming consumer keeps driving the event loop; the stage
    just observes.

    ``execute`` provides a non-streaming convenience surface: it drives
    the same ``invoke()`` stream internally and returns the final text
    response as a string.  The bridge is still the single source of
    truth — there is no separate ``run()`` method anywhere in the stack.
    """

    name = "agent"

    def __init__(
        self,
        provider: Any,
        *,
        journal: Any = None,
        artifact_store: Any = None,
        session_id: str = "",
        mcp_servers: tuple[str, ...] = (),
    ) -> None:
        self._provider: ExternalAgentBridge = self._adapt_provider(provider)
        self._journal = journal
        self._artifact_store = artifact_store
        self._session_id = session_id
        self._mcp_servers = mcp_servers
        # Shadow history forwarded as ``turn_input.context`` when the
        # provider is a raw bridge (not wrapped in :class:`AgentRunner`).
        # AgentRunner already tracks its own history and forwards it, so
        # we avoid double-tracking when wrap_agent=True.  With
        # wrap_agent=False, the bridge would otherwise see ``context=[]``
        # on every turn.
        self._tracks_history = self._should_track_history(self._provider)
        self._history: list[dict[str, str]] = []
        self._history_epoch = 0

    @staticmethod
    def _adapt_provider(provider: Any) -> ExternalAgentBridge:
        adapted = auto_adapt_agent(provider)
        if isinstance(adapted, ExternalAgentBridge):
            return adapted

        # Safety net: plain objects with ``async run(text) -> str`` are
        # wrapped in a default-config AgentRunner so direct Session or
        # AgentStage construction keeps working.  ``create_session`` /
        # ``create_text_session`` wrap earlier with the user's
        # ``agent_runner`` config, so this fallback only applies to
        # ``wrap_agent=False`` paths and direct AgentStage users.
        run_fn = getattr(adapted, "run", None)
        if callable(run_fn) and not isinstance(adapted, type):
            return AgentRunner(adapted)

        raise TypeError(
            "AgentStage.provider must implement ExternalAgentBridge "
            f"after auto_adapt_agent() (got {type(provider).__name__}). "
            "Wrap it in AgentRunner or implement the bridge protocol."
        )

    @staticmethod
    def _should_track_history(provider: ExternalAgentBridge) -> bool:
        return not isinstance(provider, AgentRunner)

    def reset_history(self) -> None:
        """Clear the stage-owned shadow history for raw bridge providers."""
        self._history.clear()
        self._history_epoch += 1

    def set_provider(self, provider: Any) -> None:
        """Replace the bridge and discard shadow history from the old provider."""
        self._provider = self._adapt_provider(provider)
        self._tracks_history = self._should_track_history(self._provider)
        self.reset_history()

    # ── Recorder construction ───────────────────────────────────

    def _journal_ctx(self, ctx: RunContext) -> RunContext:
        """Return *ctx*, substituting the constructor journal as a fallback.

        Keeps the stage-event path (``journal_append_event``, which uses
        ``ctx.journal``) and the recorder path on a single recording sink
        even when the RunContext was built without a journal.
        """
        if ctx.journal is None and self._journal is not None:
            return dataclasses.replace(ctx, journal=self._journal)
        return ctx

    def _make_recorder(self, turn_id: str | None, ctx: RunContext) -> JournalAgentRecorder:
        # Prefer the per-run ``ctx.journal`` so the recorder writes to the
        # same sink as ``journal_append_event`` (which always uses
        # ``ctx.journal``).  ``self._journal`` is only a fallback for
        # direct construction where the caller wired a journal into the
        # stage but not into the RunContext.
        return JournalAgentRecorder(
            journal=ctx.journal if ctx.journal is not None else self._journal,
            artifact_store=self._artifact_store,
            context=RecorderContext(
                run_id=f"run-{uuid4().hex[:8]}",
                session_id=self._session_id,
                turn_id=turn_id,
                mcp_servers=self._mcp_servers,
            ),
        )

    # ── Execution ───────────────────────────────────────────────

    async def execute(self, input: Any, ctx: RunContext, turn: TurnContext) -> str:
        """Drive a full turn and return the accumulated text response."""
        accumulated = ""
        async for event in self.execute_streaming(input, ctx, turn):
            kind = getattr(event, "kind", None)
            text = getattr(event, "text", "")
            if kind == "text_delta" and text:
                accumulated += text
            elif kind == "done" and text:
                accumulated = text
        return accumulated

    async def execute_streaming(
        self,
        input: Any,
        ctx: RunContext,
        turn: TurnContext,
        *,
        cancel_token: Any | None = None,
        system_prefix: str | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        """Drive ``bridge.invoke()`` while journaling a stage_start/complete.

        ``system_prefix`` is an optional system-role message prepended
        to ``turn_input.context``; Session uses it to surface caller-ID
        metadata when ``caller_id_exposure == "system_message"``.
        """
        ctx = self._journal_ctx(ctx)
        bridge = self._provider
        history_epoch = self._history_epoch
        state_before = self.snapshot_state()
        journal_append_event(
            ctx,
            stage=self.name,
            name="stage_start",
            turn_id=turn.id,
            state_before=state_before,
            data_extra={"input": input if isinstance(input, str) else str(input)},
        )

        recorder = self._make_recorder(turn.id, ctx)
        input_text = input if isinstance(input, str) else str(input)
        base_context = list(self._history) if self._tracks_history else []
        if (
            not self._tracks_history
            and system_prefix
            and isinstance(bridge, AgentRunner)
            and bridge.is_bridge
        ):
            # AgentRunner owns history for wrapped bridges.  A transient
            # system prefix must augment that history, not replace it.
            base_context = list(bridge.history)
        if system_prefix:
            # System messages prepended by Session are transient — they
            # describe the current turn's environment (caller ID, etc.)
            # and must not be folded into the stage's shadow history,
            # otherwise they'd repeat on every subsequent turn.
            base_context = [{"role": "system", "content": system_prefix}, *base_context]
        turn_input = AgentTurnInput.from_text(
            input_text,
            context=base_context if (self._tracks_history or system_prefix) else None,
            turn_id=turn.id,
        )

        accumulated: list[str] = []
        errored = False
        started = time.perf_counter()
        try:
            with observability.span(
                "easycat.agent.invoke",
                {"easycat.stage": self.name, "easycat.surface": "agent_bridge"},
            ):
                async for event in bridge.invoke(turn_input, recorder, cancel_token):
                    kind = getattr(event, "kind", None)
                    text = getattr(event, "text", "")
                    if kind == "text_delta" and text:
                        journal_append_event(
                            ctx,
                            stage=self.name,
                            name="agent_delta",
                            turn_id=turn.id,
                            data_extra={"type": "TEXT_DELTA", "text": text},
                        )
                        accumulated.append(text)
                    elif kind == "done":
                        if text:
                            journal_append_event(
                                ctx,
                                stage=self.name,
                                name="agent_delta",
                                turn_id=turn.id,
                                data_extra={"type": "DONE", "text": text},
                            )
                            accumulated = [text]
                    elif kind == "tool_started" and getattr(event, "tool_name", ""):
                        journal_append_event(
                            ctx,
                            stage=self.name,
                            name="agent_delta",
                            turn_id=turn.id,
                            data_extra={
                                "type": "TOOL_STARTED",
                                "tool_name": event.tool_name,
                                "call_id": getattr(event, "call_id", ""),
                            },
                        )
                    elif kind == "tool_result":
                        journal_append_event(
                            ctx,
                            stage=self.name,
                            name="agent_delta",
                            turn_id=turn.id,
                            data_extra={
                                "type": "TOOL_RESULT",
                                "call_id": getattr(event, "call_id", ""),
                                "result": getattr(event, "result", ""),
                            },
                        )
                    yield event
        except Exception as exc:
            errored = True
            observability.increment_counter(
                "easycat.provider.errors.total",
                attributes={
                    "easycat.surface": "agent_bridge",
                    "easycat.provider": type(self._provider).__name__.lower(),
                    "easycat.error_type": type(exc).__name__,
                },
            )
            journal_append_event(
                ctx,
                stage=self.name,
                name="stage_error",
                turn_id=turn.id,
                state_before=state_before,
                error=str(exc),
            )
            raise
        finally:
            observability.record_histogram(
                "easycat.stage.latency",
                time.perf_counter() - started,
                {
                    "easycat.stage": self.name,
                    "easycat.result": "fail" if errored else "pass",
                },
            )
            # Use a finally block so shadow history is updated even when
            # the consumer breaks out of the stream early (e.g. send_text
            # stops iterating on the ``done`` event — triggering
            # ``GeneratorExit`` at the yield above).
            if not errored:
                final_text = "".join(accumulated)
                if (
                    self._tracks_history
                    and self._provider is bridge
                    and self._history_epoch == history_epoch
                    and final_text
                ):
                    # Record the turn in shadow history so the next
                    # ``invoke()`` forwards it as ``turn_input.context``
                    # for raw bridges that rely on explicit conversation
                    # state.
                    self._history.append({"role": "user", "content": input_text})
                    self._history.append({"role": "assistant", "content": final_text})
                state_after = self.snapshot_state()
                journal_append_event(
                    ctx,
                    stage=self.name,
                    name="stage_complete",
                    turn_id=turn.id,
                    state_before=state_before,
                    state_after=state_after,
                    data_extra={"response": final_text},
                )

    # ── Post-turn framework-state mutations ─────────────────────
    #
    # These thread the same journal sink the streaming path uses so that
    # interruption / last-assistant rewrites land on the recording
    # boundary (stages are the documented debug/replay surface) instead of
    # reaching around the stage straight to the live bridge.  Without this,
    # the journal captured agent streaming but not the mutations applied to
    # the same bridge on the hardest-to-debug path (barge-in).

    def apply_interruption(
        self,
        delivered_text: str,
        mode: Any,
        *,
        ctx: RunContext | None = None,
        turn_id: str | None = None,
        caused_by_signal_id: str | None = None,
    ) -> None:
        """Apply a barge-in to the bridge, threading a journal recorder.

        The recorder lets the bridge emit its four-step atomic
        interruption records (plan → committed → apply → success/failure)
        tied to this turn, matching the streaming path's recording.
        """
        run_ctx = self._journal_ctx(ctx) if ctx is not None else None
        recorder = self._make_recorder(turn_id, run_ctx) if run_ctx is not None else None
        self._provider.apply_interruption(
            delivered_text,
            mode,
            recorder=recorder,
            caused_by_signal_id=caused_by_signal_id,
        )

    def append_interruption_note(
        self,
        note: str,
        *,
        ctx: RunContext | None = None,
        turn_id: str | None = None,
    ) -> None:
        """Append an interruption note to the bridge and journal the fact."""
        if ctx is not None:
            journal_append_event(
                self._journal_ctx(ctx),
                stage=self.name,
                name="interruption_note",
                turn_id=turn_id,
                data_extra={"note": note},
            )
        self._provider.append_interruption_note(note)

    def replace_last_assistant_text(
        self,
        text: str,
        *,
        ctx: RunContext | None = None,
        turn_id: str | None = None,
    ) -> None:
        """Rewrite the bridge's last assistant entry and journal the rewrite.

        Records the framework-state mutation on the stage boundary so a
        postmortem reflects the cleaned text the next turn conditions on,
        not just the raw streamed output.
        """
        if ctx is not None:
            journal_append_event(
                self._journal_ctx(ctx),
                stage=self.name,
                name="replace_last_assistant_text",
                turn_id=turn_id,
                data_extra={"text": text},
            )
        self._provider.replace_last_assistant_text(text)

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
        """Observe and journal an upstream control signal (no cancel here).

        Cancellation is owned by the ``CancelOrchestrator`` / turn runner;
        this method only records that the stage saw the signal.
        """
        logger.debug("AgentStage received upstream signal: %s", signal)
        if ctx is not None:
            journal_append_control_signal(self._journal_ctx(ctx), stage=self.name, signal=signal)
