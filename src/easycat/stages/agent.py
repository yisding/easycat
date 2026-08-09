"""AgentStage — wraps an :class:`ExternalAgentBridge` with journal recording."""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import AsyncGenerator, Callable
from typing import Any, Literal
from uuid import uuid4

from easycat import _observability as observability
from easycat._turn_context import TurnContext
from easycat.integrations.agents._agent_runner import (
    AgentRunner,
    PreparedAgentResponse,
    close_stream_after_done,
)
from easycat.integrations.agents._factory import auto_adapt_agent
from easycat.integrations.agents._helpers import aclose_quietly
from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents._text_stream import AgentTextStream
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
    ExternalAgentBridge,
    NullAgentRecorder,
    RecorderContext,
)
from easycat.runtime.context import RunContext
from easycat.runtime.replay import ReplayCassette, ReplayFidelity, ReplaySpec
from easycat.stages.base import (
    ControlSignal,
    StageStateSnapshot,
    journal_append_control_signal,
    journal_append_event,
    journal_ctx,
    live_replay_input,
    record_stage_failure,
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

    @property
    def supports_preemptive_generation(self) -> bool:
        """Whether the provider can prepare a voice response transactionally."""
        return bool(
            isinstance(self._provider, AgentRunner)
            and self._provider.supports_preemptive_generation
        )

    @property
    def preemptive_max_retries(self) -> int:
        """Maximum preemptive attempts allowed for one voice turn."""
        if isinstance(self._provider, AgentRunner):
            return self._provider.preemptive_max_retries
        return 0

    async def prepare_preemptive(self, input: Any, turn: TurnContext) -> PreparedAgentResponse:
        """Prepare a simple-agent response without committing conversation state."""
        if not isinstance(self._provider, AgentRunner):
            raise RuntimeError("agent provider does not support preemptive generation")  # noqa: TRY004 domain-specific validation error
        input_text = input if isinstance(input, str) else str(input)
        return await self._provider.prepare_response(
            AgentTurnInput.from_text(input_text, turn_id=turn.id)
        )

    def _make_recorder(self, turn_id: str | None, ctx: RunContext) -> AgentRecorder:
        # Prefer the per-run ``ctx.journal`` so the recorder writes to the
        # same sink as ``journal_append_event`` (which always uses
        # ``ctx.journal``).  ``self._journal`` is only a fallback for
        # direct construction where the caller wired a journal into the
        # stage but not into the RunContext.
        journal = ctx.journal if ctx.journal is not None else self._journal
        if journal is None and self._artifact_store is None:
            return NullAgentRecorder(
                RecorderContext(
                    run_id="null",
                    session_id=self._session_id,
                    turn_id=turn_id,
                    mcp_servers=self._mcp_servers,
                )
            )
        return JournalAgentRecorder(
            journal=journal,
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
        accumulated = AgentTextStream()
        async for event in self.execute_streaming(input, ctx, turn):
            kind = getattr(event, "kind", None)
            text = getattr(event, "text", "")
            if kind in {"text_delta", "text_replace"}:
                accumulated.apply(event)
            elif kind == "done" and text:
                accumulated.replace_final(text)
        return accumulated.text

    async def execute_streaming(
        self,
        input: Any,
        ctx: RunContext,
        turn: TurnContext,
        *,
        cancel_token: Any | None = None,
        system_prefix: str | None = None,
        prepared_response: PreparedAgentResponse | None = None,
        input_role: Literal["system", "user"] = "user",
        commit_guard: Callable[[], bool] | None = None,
    ) -> AsyncGenerator[AgentBridgeEvent, None]:
        """Drive ``bridge.invoke()`` while journaling a stage_start/complete.

        ``system_prefix`` is an optional system-role message prepended
        to ``turn_input.context``; Session uses it to surface caller-ID
        metadata when ``caller_id_exposure == "system_message"``.
        """
        ctx = journal_ctx(ctx, self._journal)
        bridge = self._provider
        history_epoch = self._history_epoch
        journal_enabled = ctx.journal is not None
        input_text = input if isinstance(input, str) else str(input)
        state_before = self.snapshot_state() if journal_enabled else None
        start_sequence = None
        if journal_enabled:
            start_sequence = journal_append_event(
                ctx,
                stage=self.name,
                name="stage_start",
                turn_id=turn.id,
                state_before=state_before,
                data_extra={"input": input_text},
            )

        recorder = self._make_recorder(turn.id, ctx)
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
            role=input_role,
        )

        accumulated = AgentTextStream()
        errored = False
        started = time.perf_counter()
        try:
            span = (
                observability.span(
                    "easycat.agent.invoke",
                    {"easycat.stage": self.name, "easycat.surface": "agent_bridge"},
                )
                if observability.tracing_available()
                else contextlib.nullcontext()
            )
            with span:
                if prepared_response is not None:
                    if not isinstance(bridge, AgentRunner):
                        raise RuntimeError("prepared response requires AgentRunner")
                    if prepared_response.input_text != input_text:
                        raise RuntimeError("prepared response transcript does not match input")
                    stream = bridge.invoke_prepared(
                        prepared_response,
                        recorder,
                        cancel_token,
                        commit_guard=commit_guard,
                    )
                elif isinstance(bridge, AgentRunner):
                    stream = bridge.invoke(
                        turn_input,
                        recorder,
                        cancel_token,
                        commit_guard=commit_guard,
                    )
                else:
                    stream = bridge.invoke(turn_input, recorder, cancel_token)
                try:
                    async for event in stream:
                        if commit_guard is not None and not commit_guard():
                            break
                        kind = getattr(event, "kind", None)
                        text = getattr(event, "text", "")
                        if kind in {"text_delta", "text_replace"}:
                            # Record the delivered token before yielding it.
                            # Async generator cleanup (``aclose()``, disconnects,
                            # or cancellation) resumes by injecting
                            # ``GeneratorExit`` at the suspended yield, so
                            # post-yield code is not a reliable audit boundary
                            # for text already handed to downstream TTS or text
                            # clients.
                            cancelled = bool(cancel_token and cancel_token.is_cancelled)
                            if journal_enabled and not cancelled:
                                delta_data: dict[str, Any] = {
                                    "type": (
                                        "TEXT_REPLACE" if kind == "text_replace" else "TEXT_DELTA"
                                    ),
                                    "text": text,
                                }
                                part_index = getattr(event, "part_index", None)
                                if part_index is not None:
                                    delta_data["part_index"] = part_index
                                journal_append_event(
                                    ctx,
                                    stage=self.name,
                                    name="agent_delta",
                                    turn_id=turn.id,
                                    data_extra=delta_data,
                                )
                            if not cancelled:
                                accumulated.apply(event)
                            yield event
                            continue
                        elif kind == "done":
                            cancelled = bool(cancel_token and cancel_token.is_cancelled)
                            if text and not cancelled:
                                if journal_enabled:
                                    journal_append_event(
                                        ctx,
                                        stage=self.name,
                                        name="agent_delta",
                                        turn_id=turn.id,
                                        data_extra={"type": "DONE", "text": text},
                                    )
                                accumulated.replace_final(text)
                        elif kind == "tool_started" and getattr(event, "tool_name", ""):
                            if journal_enabled:
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
                            if journal_enabled:
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
                        if kind == "done":
                            await close_stream_after_done(stream)
                            yield event
                            return
                        yield event
                finally:
                    # Forward an early consumer close (a barge-in ``aclose()``
                    # injects ``GeneratorExit`` at a ``yield event`` above) down
                    # into ``bridge.invoke()``.  ``async for`` does not do this
                    # on its own, so without it the wrapped bridge is left
                    # suspended and only GC-finalized, letting its
                    # ``BaseException`` cleanup (which persists the partial
                    # turn) race the next ``apply_interruption()``.  On normal
                    # completion the stream is already drained via
                    # ``close_stream_after_done`` so this is a no-op.
                    await aclose_quietly(stream)
        except Exception as exc:
            errored = True
            elapsed_ms = (time.perf_counter() - started) * 1000
            record_stage_failure(
                exc,
                ctx,
                stage=self.name,
                provider=type(self._provider).__name__.lower(),
                surface="agent_bridge",
                elapsed_ms=elapsed_ms,
                sequence=start_sequence,
                turn_id=turn.id,
                state_before=state_before,
            )
            raise
        finally:
            if observability.metrics_available():
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
                elapsed_ms = (time.perf_counter() - started) * 1000
                # Cancellation can arrive while a terminal bridge stream is
                # being drained after its ``done`` event. Recheck at the
                # commit boundary so unheard terminal text cannot enter the
                # raw-bridge shadow history or stage completion record.
                final_text = (
                    ""
                    if (cancel_token and cancel_token.is_cancelled)
                    or (commit_guard is not None and not commit_guard())
                    else accumulated.text
                )
                if (
                    self._tracks_history
                    and input_role != "system"
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
                if journal_enabled:
                    state_after = self.snapshot_state()
                    journal_append_event(
                        ctx,
                        stage=self.name,
                        name="stage_complete",
                        turn_id=turn.id,
                        state_before=state_before,
                        state_after=state_after,
                        data_extra={"response": final_text, "elapsed_ms": elapsed_ms},
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
        run_ctx = journal_ctx(ctx, self._journal) if ctx is not None else None
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
                journal_ctx(ctx, self._journal),
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
                journal_ctx(ctx, self._journal),
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
            return live_replay_input(spec, cassette, source="data_input")

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
            journal_append_control_signal(
                journal_ctx(ctx, self._journal), stage=self.name, signal=signal
            )
