"""PydanticAI bridge — Agent mode and pydantic_graph Graph mode.

One bridge class, two input modes.  Duck-types at construction and
routes to the appropriate internal path.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import uuid4

from easycat.cancel import CancelToken
from easycat.integrations.agents._pydantic_ai_events import translate_event
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
    BridgeConfigurationError,
    BridgeInputError,
    CancellationMode,
    CommitRule,
    ExecutionCursor,
    FrameworkStateSnapshot,
    InterruptionPlan,
    UnitKind,
)
from easycat.runtime.records import ErrorInfo

logger = logging.getLogger(__name__)


class PydanticAIBridge:
    """Bridge for ``pydantic_ai.Agent`` and ``pydantic_graph.Graph``.

    Accepts either ``agent=`` (single-agent mode) or ``graph=`` with
    ``state_factory`` and ``initial_node_factory`` (graph mode).
    """

    COMMITTABLE_BOUNDARIES = {
        UnitKind.AGENT: CommitRule.BETWEEN_TURNS,
        UnitKind.MODEL_NODE: CommitRule.NON_COMMITTABLE,
        UnitKind.TOOL_CALL: CommitRule.BETWEEN_PHASES,
        UnitKind.USER_PROMPT: CommitRule.NON_COMMITTABLE,
        UnitKind.WORKFLOW_NODE: CommitRule.BETWEEN_NODES,
    }

    def __init__(
        self,
        *,
        agent: Any | None = None,
        deps: Any = None,
        model_settings: Any = None,
        graph: Any | None = None,
        state_factory: Callable[[], Any] | None = None,
        initial_node_factory: Callable[[str, Any], Any] | None = None,
        agents: list[Any] | None = None,
        mcp_servers: list[Any] | None = None,
    ) -> None:
        if agent is not None and graph is not None:
            raise BridgeInputError("Cannot pass both agent= and graph= to PydanticAIBridge")
        if agent is None and graph is None:
            raise BridgeInputError("Must pass either agent= or graph= to PydanticAIBridge")

        if graph is not None:
            if state_factory is None or initial_node_factory is None:
                raise BridgeInputError(
                    "Graph mode requires state_factory and initial_node_factory"
                )
            # Convention validation: probe for _easycat_event_handler.
            probe = state_factory()
            if not hasattr(probe, "_easycat_event_handler"):
                raise BridgeConfigurationError(
                    "Graph state object must have an '_easycat_event_handler' "
                    "attribute. Add `_easycat_event_handler: Any = None` to "
                    "your state dataclass and pass "
                    "`event_stream_handler=ctx.state._easycat_event_handler` "
                    "to each agent.run() call inside graph nodes."
                )
            self._mode = "graph"
            self._graph = graph
            self._state_factory = state_factory
            self._initial_node_factory = initial_node_factory
            self._agents = agents
            self._agent = None
            self._state: Any = None
            self._active_node: str | None = None
        else:
            self._mode = "agent"
            self._agent = agent
            self._graph = None
            self._state_factory = None
            self._initial_node_factory = None
            self._agents = None
            self._state = None
            self._active_node = None

        self._deps = deps
        self._model_settings = model_settings
        self._mcp_servers = mcp_servers
        self._message_history: list[Any] = []
        self._last_output: Any = None

    # ── ExternalAgentBridge interface ─────────────────────────────

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        if self._mode == "graph":
            async for event in self._invoke_graph(turn_input, recorder, cancel_token):
                yield event
        else:
            async for event in self._invoke_agent(turn_input, recorder, cancel_token):
                yield event

    def snapshot_state(self) -> FrameworkStateSnapshot:
        if self._mode == "graph":
            fields: dict[str, Any] = {
                "mode": "graph",
                "graph": type(self._graph).__name__ if self._graph else "",
                "active_node": self._active_node,
            }
            # State may be large — use artifact ref if needed.
            state_ref = None
            if self._state is not None:
                try:
                    state_json = json.dumps(_safe_state_dict(self._state), default=str)
                    if len(state_json) > 4096:
                        state_ref = f"state-{uuid4().hex[:16]}"
                        fields["state_summary"] = type(self._state).__name__
                    else:
                        fields["state"] = json.loads(state_json)
                except (TypeError, ValueError):
                    fields["state_summary"] = type(self._state).__name__
            return FrameworkStateSnapshot(
                fields=fields,
                state_ref=state_ref,
                kind="pydantic_ai_graph",
            )
        return FrameworkStateSnapshot(
            fields={
                "mode": "agent",
                "agent": _agent_name(self._agent),
                "turn_count": len(self._message_history),
            },
            kind="pydantic_ai_agent",
        )

    def apply_interruption(
        self,
        delivered_text: str,
        mode: CancellationMode,
        recorder: AgentRecorder | None = None,
        caused_by_signal_id: str | None = None,
    ) -> None:
        # Step 1: plan the mutation.
        plan = self._plan_interruption(delivered_text, mode)

        # Step 2: write FrameworkStateCommitted to the journal.
        if recorder is not None:
            try:
                recorder.record_state_committed(
                    mutation_kind=plan.mutation_kind,
                    pre_state_ref=plan.pre_state_ref,
                    post_state_ref=plan.post_state_ref,
                )
            except Exception:
                return

        # Step 3: apply the planned mutation.
        try:
            self._apply_planned_mutation(plan)
        except Exception as exc:
            # Step 4a: mutation failed.
            if recorder is not None:
                recorder.record_interruption_apply_failed(
                    mutation_kind=plan.mutation_kind,
                    pre_state_ref=plan.pre_state_ref,
                    post_state_ref=plan.post_state_ref,
                    failure_error=ErrorInfo.from_exception(exc),
                )
            raise

        # Step 4b: success.
        if recorder is not None:
            recorder.record_cancellation_boundary(
                mode=mode,
                reason=plan.mutation_kind,
                caused_by_signal_id=caused_by_signal_id,
            )

    def _plan_interruption(self, delivered_text: str, mode: CancellationMode) -> InterruptionPlan:
        replacement = delivered_text + "..." if delivered_text else ""
        mutation_kind = "interrupt_truncate"
        if self._mode == "graph":
            mutation_kind = "interrupt_truncate_graph"
        pre_ref = f"pydantic-pre-{id(self._message_history):x}"
        post_ref = f"pydantic-post-{id(self._message_history):x}"
        return InterruptionPlan(
            mutation_kind=mutation_kind,
            pre_state_ref=pre_ref,
            post_state_ref=post_ref,
            framework_instructions={
                "replacement": replacement,
                "mode": self._mode,
                "use_custom_truncation": (
                    self._mode == "graph"
                    and self._state is not None
                    and hasattr(self._state, "truncate_last_assistant")
                ),
                "delivered_text": delivered_text,
            },
        )

    def _apply_planned_mutation(self, plan: InterruptionPlan) -> None:
        instructions = plan.framework_instructions
        replacement = instructions["replacement"]

        if instructions.get("use_custom_truncation"):
            self._state.truncate_last_assistant(instructions["delivered_text"])
            return

        try:
            from pydantic_ai.messages import ModelResponse
        except ImportError:
            return

        history = self._message_history
        for i in range(len(history) - 1, -1, -1):
            msg = history[i]
            if isinstance(msg, ModelResponse):
                for part in msg.parts:
                    if type(part).__name__ == "TextPart":
                        try:
                            part.content = replacement
                        except (AttributeError, TypeError):
                            object.__setattr__(part, "content", replacement)
                        return
                break

    def reset(self) -> None:
        self._message_history.clear()
        self._last_output = None
        self._state = None
        self._active_node = None

    # ── Agent mode ───────────────────────────────────────────────

    async def _invoke_agent(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        agent_cursor = ExecutionCursor(
            unit_id=f"agent-{uuid4().hex[:8]}",
            unit_kind=UnitKind.AGENT,
            display_name=_agent_name(self._agent),
            entered_at=time.monotonic_ns(),
            committable=False,
        )
        recorder.record_unit_entered(agent_cursor)

        accumulated = ""
        raw_output: Any = None

        try:
            if hasattr(self._agent, "iter"):
                async for ev in self._stream_via_iter(turn_input.text, recorder, cancel_token):
                    if ev.kind == "text_delta":
                        accumulated += ev.text
                    yield ev
                raw_output = self._last_output
            else:
                async for ev in self._stream_via_run_stream(turn_input.text, cancel_token):
                    if ev.kind == "text_delta":
                        accumulated += ev.text
                    yield ev
                raw_output = self._last_output
        except Exception as exc:
            recorder.record_framework_error(ErrorInfo.from_exception(exc))
            recorder.record_unit_exited(agent_cursor, reason="error")
            raise

        recorder.record_unit_exited(agent_cursor.with_committable(True), reason=None)
        yield AgentBridgeEvent(
            kind="done",
            text=accumulated,
            structured_output=raw_output,
        )

    async def _stream_via_iter(
        self,
        text: str,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        """Stream using ``agent.iter()`` with full event capture."""
        async with self._agent.iter(
            text,
            message_history=self._message_history or None,
            deps=self._deps,
            model_settings=self._model_settings,
        ) as agent_run:
            interrupted = False
            async for node in agent_run:
                node_cls = type(node).__name__
                is_tool_node = node_cls == "CallToolsNode"

                if cancel_token and cancel_token.is_cancelled:
                    interrupted = True
                    if not is_tool_node:
                        break

                if not hasattr(node, "stream"):
                    continue

                async with node.stream(agent_run.ctx) as stream:
                    async for event in stream:
                        if cancel_token and cancel_token.is_cancelled and not interrupted:
                            interrupted = True
                            if not is_tool_node:
                                break
                        mapped = translate_event(event, recorder)
                        if mapped is not None:
                            if interrupted and mapped.kind == "text_delta":
                                continue
                            yield mapped

            self._message_history = agent_run.new_messages()
            raw = getattr(agent_run, "output", None)
            if raw is None:
                _result = getattr(agent_run, "result", None)
                if _result is not None:
                    raw = getattr(_result, "output", None)
            self._last_output = raw

    async def _stream_via_run_stream(
        self,
        text: str,
        cancel_token: CancelToken | None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        """Fallback: stream text-only via ``agent.run_stream()``."""
        async with self._agent.run_stream(
            text,
            message_history=self._message_history or None,
            deps=self._deps,
            model_settings=self._model_settings,
        ) as result:
            accumulated = ""
            async for full_text in result.stream_text():
                if cancel_token and cancel_token.is_cancelled:
                    break
                delta = full_text[len(accumulated) :]
                if delta:
                    yield AgentBridgeEvent(kind="text_delta", text=delta)
                accumulated = full_text

            self._message_history = result.new_messages()
            self._last_output = getattr(result, "output", None)

    # ── Graph mode ───────────────────────────────────────────────

    async def _invoke_graph(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        state = self._state_factory()
        self._state = state
        initial_node = self._initial_node_factory(turn_input.text, state)

        # Install event handler on state for per-agent capture.
        _handler = _GraphEventHandler(recorder)
        state._easycat_event_handler = _handler

        accumulated = ""
        prev_node_name: str | None = None
        prev_cursor: ExecutionCursor | None = None

        try:
            async with self._graph.iter(initial_node, state=state) as graph_run:
                async for node in graph_run:
                    node_name = type(node).__name__
                    self._active_node = node_name

                    if cancel_token and cancel_token.is_cancelled:
                        break

                    # Emit handoff triple if node changed.
                    if prev_node_name is not None and node_name != prev_node_name:
                        if prev_cursor is not None:
                            recorder.record_unit_exited(
                                prev_cursor.with_committable(True), reason=None
                            )
                        recorder.record_framework_handoff(
                            from_unit=prev_node_name,
                            to_unit=node_name,
                            reason="graph_transition",
                        )

                    cursor = ExecutionCursor(
                        unit_id=f"node-{uuid4().hex[:8]}",
                        unit_kind=UnitKind.WORKFLOW_NODE,
                        display_name=node_name,
                        entered_at=time.monotonic_ns(),
                        committable=False,
                    )
                    recorder.record_unit_entered(cursor)
                    prev_cursor = cursor
                    prev_node_name = node_name

                    # Let the node run — agent calls inside will use the
                    # event handler on the state.

            # Close the last cursor.
            if prev_cursor is not None:
                recorder.record_unit_exited(prev_cursor.with_committable(True), reason=None)
                prev_cursor = None

            # Collect any text from the handler.
            accumulated = _handler.accumulated_text

            # Capture result.
            result = getattr(graph_run, "result", None)
            if result is not None:
                output = getattr(result, "output", None)
                self._last_output = output
                if output and isinstance(output, str):
                    accumulated = accumulated or output

            # Capture graph run history as a state snapshot record.
            run_history = getattr(graph_run, "history", None)
            if run_history is not None:
                try:
                    serialized = json.dumps(
                        [
                            {"node": type(n).__name__, "index": i}
                            for i, n in enumerate(run_history)
                        ],
                        default=str,
                    )
                    recorder.record_state_snapshot(
                        ref=f"graph-history-{uuid4().hex[:8]}",
                        payload=serialized.encode("utf-8"),
                    )
                except (TypeError, ValueError):
                    logger.debug("Failed to serialize graph run history")

            # Convention enforcement: verify handler was actually used.
            if not _handler.was_called and prev_node_name is not None:
                from easycat.integrations.agents.base import (
                    ConventionViolationError,
                )

                raise ConventionViolationError(
                    "PydanticAIBridge Graph mode: the _easycat_event_handler "
                    "was installed on the state but no agent call passed it "
                    "through via event_stream_handler=. Ensure every "
                    "agent.run() / agent.iter() call inside graph nodes "
                    "passes event_stream_handler=ctx.state._easycat_event_handler."
                )

        except Exception as exc:
            recorder.record_framework_error(ErrorInfo.from_exception(exc))
            if prev_cursor is not None:
                recorder.record_unit_exited(prev_cursor, reason="error")
            raise

        yield AgentBridgeEvent(
            kind="done",
            text=accumulated,
            structured_output=self._last_output,
        )


# ── Graph event handler ──────────────────────────────────────────


class _GraphEventHandler:
    """Installed on graph state to capture per-agent events.

    Agents inside graph nodes call this via
    ``event_stream_handler=ctx.state._easycat_event_handler``.
    """

    def __init__(self, recorder: AgentRecorder) -> None:
        self._recorder = recorder
        self._accumulated_text = ""
        self._was_called = False

    @property
    def accumulated_text(self) -> str:
        return self._accumulated_text

    @property
    def was_called(self) -> bool:
        return self._was_called

    async def __call__(self, event: Any) -> None:
        """Handle a PydanticAI streaming event from inside a graph node."""
        self._was_called = True
        mapped = translate_event(event, self._recorder)
        if mapped is not None and mapped.kind == "text_delta":
            self._accumulated_text += mapped.text


# ── Helpers ──────────────────────────────────────────────────────


def _agent_name(agent: Any) -> str:
    return getattr(agent, "name", None) or type(agent).__name__


def _safe_state_dict(state: Any) -> dict[str, Any]:
    """Extract a JSON-safe dict from a state object."""
    from dataclasses import fields as dc_fields

    result: dict[str, Any] = {}
    try:
        for f in dc_fields(state):
            if f.name.startswith("_"):
                continue
            val = getattr(state, f.name, None)
            try:
                json.dumps(val, default=str)
                result[f.name] = val
            except (TypeError, ValueError):
                result[f.name] = repr(val)
    except TypeError:
        # Not a dataclass — use vars.
        for k, v in vars(state).items():
            if k.startswith("_"):
                continue
            try:
                json.dumps(v, default=str)
                result[k] = v
            except (TypeError, ValueError):
                result[k] = repr(v)
    return result
