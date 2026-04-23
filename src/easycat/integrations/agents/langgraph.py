"""LangGraph bridge — wraps a ``CompiledStateGraph`` with checkpointer support.

A compiled LangGraph graph is itself a LangChain ``Runnable``, so the
bridge drives it via ``graph.astream_events(input, version="v2")``
exactly the way :class:`LangChainBridge` does.  The per-event
``metadata`` dict carries ``langgraph_node``, ``langgraph_step``,
``thread_id`` and ``checkpoint_id`` fields that we hoist into
``workflow_node`` cursors and ``state_snapshot`` records.  This keeps
both bridges on one event vocabulary and drops the four-shape tuple
normalizer, the node-key diff tracking, and the separate ``debug``
stream-mode branch the earlier implementation required.

Interruption patches the last AI message via LangGraph's native
``update_state``.  Because LangGraph's ``add_messages`` reducer dedupes
by message ``id``, we re-send the edited message under the same id so
it replaces instead of appending.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any
from uuid import uuid4

from easycat.cancel import CancelToken
from easycat.integrations.agents._helpers import split_replacement_by_original_parts
from easycat.integrations.agents._langchain_events import translate_stream_event
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
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


# ``chain`` is needed so every node execution emits ``on_chain_start`` /
# ``on_chain_end`` events from which we build workflow_node cursors.
# ``chat_model`` and ``tool`` give token + tool visibility.
_DEFAULT_INCLUDE_TYPES: tuple[str, ...] = ("chat_model", "tool", "chain")


class LangGraphBridge:
    """Wraps a LangGraph ``CompiledStateGraph``.

    Parameters
    ----------
    graph:
        A LangGraph compiled graph (``langgraph.graph.state.
        CompiledStateGraph``).  The graph **must** be compiled with a
        checkpointer — without one, ``update_state`` / ``get_state`` are
        unavailable and interruption patching cannot work.
    thread_id:
        Optional existing thread id to resume an earlier conversation.
        Defaults to a fresh UUID.
    messages_key:
        Key under which to inject the user's utterance into the graph's
        initial input dict.  Defaults to ``"messages"``.  Set to
        ``None`` to pass the text as a bare string input instead.
    display_name:
        Optional label for the outer ``agent`` cursor (defaults to
        ``type(graph).__name__``).
    include_types:
        Runnable types to surface via ``astream_events(include_types=
        ...)``.  Defaults to ``("chat_model", "tool", "chain")`` —
        ``chain`` is needed so every node entry is observable for
        workflow_node cursors.  Pass ``None`` to surface every event.
    """

    COMMITTABLE_BOUNDARIES = {
        UnitKind.AGENT: CommitRule.BETWEEN_TURNS,
        UnitKind.WORKFLOW_NODE: CommitRule.BETWEEN_NODES,
        UnitKind.MODEL_NODE: CommitRule.NON_COMMITTABLE,
        UnitKind.TOOL_CALL: CommitRule.BETWEEN_PHASES,
    }

    def __init__(
        self,
        graph: Any,
        *,
        thread_id: str | None = None,
        messages_key: str | None = "messages",
        display_name: str | None = None,
        include_types: Sequence[str] | None = _DEFAULT_INCLUDE_TYPES,
    ) -> None:
        if graph is None:
            raise BridgeInputError("LangGraphBridge requires a non-None graph=")
        if not hasattr(graph, "astream_events"):
            raise BridgeInputError(
                "LangGraphBridge requires a compiled LangGraph graph with "
                "astream_events() — got: " + type(graph).__name__
            )
        checkpointer = getattr(graph, "checkpointer", None)
        if checkpointer is None:
            raise BridgeInputError(
                "LangGraphBridge requires a graph compiled with a checkpointer. "
                "Call graph.compile(checkpointer=InMemorySaver()) (or another "
                "checkpointer) before passing it to LangGraphBridge."
            )
        self._graph = graph
        self._thread_id = thread_id or str(uuid.uuid4())
        self._messages_key = messages_key
        self._display_name = display_name or type(graph).__name__
        self._include_types = list(include_types) if include_types is not None else None
        self._last_output: Any = None

    # ── ExternalAgentBridge interface ─────────────────────────────

    def _config(self) -> dict[str, Any]:
        return {"configurable": {"thread_id": self._thread_id}}

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        agent_cursor = ExecutionCursor(
            unit_id=f"agent-{uuid4().hex[:8]}",
            unit_kind=UnitKind.AGENT,
            display_name=self._display_name,
            entered_at=time.monotonic_ns(),
            committable=False,
        )
        recorder.record_unit_entered(agent_cursor)

        accumulated = ""
        # Cursors open inside this turn, keyed by LangChain ``run_id``.
        # Each node entry opens a workflow_node cursor; each chat_model
        # call opens a model_node cursor.  Closing is driven by the
        # matching ``_end`` event for the same run_id.
        open_cursors: dict[str, ExecutionCursor] = {}
        # Previously seen node at each subgraph namespace depth, so we
        # can emit handoff triples when a node changes at the same level.
        last_node_by_ns: dict[tuple[str, ...], str] = {}
        # Checkpoint ids we've already emitted state_snapshot records
        # for, to avoid duplicates when the same id appears on multiple
        # events within a super-step.
        seen_checkpoints: set[str] = set()

        config = self._config()
        input_payload = self._build_input(turn_input.text)
        stream_kwargs: dict[str, Any] = {"version": "v2", "config": config}
        if self._include_types is not None:
            stream_kwargs["include_types"] = self._include_types

        try:
            stream = self._graph.astream_events(input_payload, **stream_kwargs)
            async for event in stream:
                if cancel_token and cancel_token.is_cancelled:
                    recorder.record_cancellation_boundary(
                        mode=CancellationMode.IMMEDIATE_STOP,
                        reason="cancel_token_set",
                    )
                    break

                for bridge_event in self._handle_cursor_lifecycle(
                    event, recorder, agent_cursor, open_cursors, last_node_by_ns
                ):
                    yield bridge_event

                for bridge_event in translate_stream_event(event, recorder):
                    if bridge_event.kind == "text_delta":
                        accumulated += bridge_event.text
                    yield bridge_event

                self._maybe_record_checkpoint(event, recorder, seen_checkpoints)
        except Exception as exc:
            for cursor in reversed(list(open_cursors.values())):
                try:
                    recorder.record_unit_exited(cursor, reason="error")
                except Exception:
                    logger.debug("Failed to close cursor during error cleanup", exc_info=True)
            recorder.record_framework_error(ErrorInfo.from_exception(exc))
            recorder.record_unit_exited(agent_cursor, reason="error")
            raise

        for cursor in reversed(list(open_cursors.values())):
            recorder.record_unit_exited(cursor.with_committable(True), reason=None)
        open_cursors.clear()

        # Surface the final checkpoint + capture the last message for
        # ``structured_output``.  Best-effort: a graph compiled without
        # a checkpointer would have been rejected at construction, but
        # ``get_state`` can still fail on transient checkpointer errors.
        try:
            final_state = self._graph.get_state(config)
            checkpoint_id = _get_checkpoint_id(final_state)
            if checkpoint_id and checkpoint_id not in seen_checkpoints:
                recorder.record_state_snapshot(ref=f"langgraph:{checkpoint_id}")
            self._last_output = _messages_tail(final_state, self._messages_key or "messages")
        except Exception:  # pragma: no cover — best-effort.
            logger.debug("Failed to fetch final LangGraph state", exc_info=True)

        recorder.record_unit_exited(agent_cursor.with_committable(True), reason=None)
        yield AgentBridgeEvent(
            kind="done",
            text=accumulated,
            structured_output=self._last_output,
        )

    def snapshot_state(self) -> FrameworkStateSnapshot:
        fields: dict[str, Any] = {
            "framework": "langgraph",
            "graph": self._display_name,
            "thread_id": self._thread_id,
        }
        try:
            state = self._graph.get_state(self._config())
            fields["checkpoint_id"] = _get_checkpoint_id(state)
            next_nodes = getattr(state, "next", None)
            if next_nodes is not None:
                fields["next_nodes"] = list(next_nodes)
            metadata = getattr(state, "metadata", None) or {}
            if isinstance(metadata, dict):
                fields["step"] = metadata.get("step")
        except Exception:  # pragma: no cover — missing checkpointer or fresh graph.
            logger.debug("snapshot_state: failed to fetch graph state", exc_info=True)
        return FrameworkStateSnapshot(
            fields=fields,
            kind="langgraph",
        )

    def apply_interruption(
        self,
        delivered_text: str,
        mode: CancellationMode,
        recorder: AgentRecorder | None = None,
        caused_by_signal_id: str | None = None,
    ) -> None:
        plan = self._plan_interruption(delivered_text, mode)

        actual_pre_ref = plan.pre_state_ref
        if recorder is not None:
            actual_pre_ref = recorder.record_state_snapshot(
                plan.pre_state_ref,
                payload=self._serialize_framework_state(),
            )

        if recorder is not None:
            try:
                recorder.record_state_committed(
                    mutation_kind=plan.mutation_kind,
                    pre_state_ref=actual_pre_ref,
                    post_state_ref=plan.post_state_ref,
                )
            except Exception:
                return

        try:
            self._apply_planned_mutation(plan)
        except Exception as exc:
            if recorder is not None:
                recorder.record_interruption_apply_failed(
                    mutation_kind=plan.mutation_kind,
                    pre_state_ref=actual_pre_ref,
                    post_state_ref=plan.post_state_ref,
                    failure_error=ErrorInfo.from_exception(exc),
                )
            raise

        if recorder is not None:
            recorder.record_state_snapshot(
                plan.post_state_ref,
                payload=self._serialize_framework_state(),
            )
            recorder.record_cancellation_boundary(
                mode=mode,
                reason=plan.mutation_kind,
                caused_by_signal_id=caused_by_signal_id,
            )

    def reset(self) -> None:
        self._thread_id = str(uuid.uuid4())
        self._last_output = None

    # ── History post-processing ───────────────────────────────────

    def replace_last_assistant_text(self, text: str) -> None:
        """Rewrite the last AI message in graph state to ``text``."""
        self._rewrite_last_ai_message(text)

    def append_interruption_note(self, note: str) -> None:
        """Append an interruption note to graph history so the next turn sees it."""
        try:
            from langchain_core.messages import SystemMessage

            new_msg = SystemMessage(content=note)
            self._graph.update_state(self._config(), {self._messages_key or "messages": [new_msg]})
        except ImportError:
            # Fallback — use a plain dict message; LangGraph accepts these
            # for the ``add_messages`` reducer too.
            try:
                self._graph.update_state(
                    self._config(),
                    {self._messages_key or "messages": [{"role": "system", "content": note}]},
                )
            except Exception:
                logger.debug("Failed to append interruption note via update_state", exc_info=True)
        except Exception:
            logger.debug("Failed to append interruption note to LangGraph", exc_info=True)

    # ── Internal ─────────────────────────────────────────────────

    def _build_input(self, text: str) -> Any:
        if self._messages_key is None:
            return text
        return {self._messages_key: [("user", text)]}

    def _handle_cursor_lifecycle(
        self,
        event: dict[str, Any],
        recorder: AgentRecorder,
        agent_cursor: ExecutionCursor,
        open_cursors: dict[str, ExecutionCursor],
        last_node_by_ns: dict[tuple[str, ...], str],
    ) -> list[AgentBridgeEvent]:
        """Open / close workflow_node + model_node cursors for one event.

        Each node invocation in a LangGraph run appears as an
        ``on_chain_start`` event whose ``metadata`` carries
        ``langgraph_node``, ``langgraph_checkpoint_ns`` and
        ``langgraph_step``.  We open a workflow_node cursor keyed by
        ``run_id`` and emit a handoff triple whenever the active node
        at a given checkpoint_ns changes.

        Chat-model calls open a ``model_node`` cursor nested inside the
        enclosing workflow_node (or the outer agent_cursor for
        plain-runnable events).  ``_end`` events close the matching
        cursor by ``run_id``.
        """
        events: list[AgentBridgeEvent] = []
        event_type = event.get("event")
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        node_name = metadata.get("langgraph_node")
        ns_raw = metadata.get("langgraph_checkpoint_ns", "")
        ns: tuple[str, ...] = tuple(ns_raw.split("|")) if ns_raw else ()

        if event_type == "on_chain_start":
            # Only node-entry chain_starts have langgraph_node set AND a
            # name that matches the node name (other chain_starts are
            # internal runnables inside the node).
            if (
                isinstance(node_name, str)
                and node_name
                and event.get("name") == node_name
                and node_name not in ("__start__", "__end__")
            ):
                run_id = str(event.get("run_id") or uuid4().hex[:8])
                if run_id in open_cursors:
                    return events

                prev = last_node_by_ns.get(ns)
                if prev and prev != node_name:
                    recorder.record_framework_handoff(
                        from_unit=prev,
                        to_unit=node_name,
                        reason="langgraph_edge",
                    )
                    events.append(
                        AgentBridgeEvent(
                            kind="handoff",
                            from_unit=prev,
                            to_unit=node_name,
                            reason="langgraph_edge",
                        )
                    )

                cursor = ExecutionCursor(
                    unit_id=f"node-{run_id[:8]}",
                    unit_kind=UnitKind.WORKFLOW_NODE,
                    display_name=node_name,
                    parent_unit_id=self._nearest_parent_id(open_cursors, ns, agent_cursor.unit_id),
                    entered_at=time.monotonic_ns(),
                    committable=False,
                )
                recorder.record_unit_entered(cursor)
                open_cursors[run_id] = cursor
                last_node_by_ns[ns] = node_name

        elif event_type in ("on_chat_model_start", "on_llm_start"):
            run_id = str(event.get("run_id") or uuid4().hex[:8])
            if run_id in open_cursors:
                return events
            cursor = ExecutionCursor(
                unit_id=f"model-{run_id[:8]}",
                unit_kind=UnitKind.MODEL_NODE,
                display_name=str(event.get("name") or "model"),
                parent_unit_id=self._nearest_parent_id(open_cursors, ns, agent_cursor.unit_id),
                entered_at=time.monotonic_ns(),
                committable=False,
            )
            recorder.record_unit_entered(cursor)
            open_cursors[run_id] = cursor

        elif event_type in ("on_chain_end", "on_chat_model_end", "on_llm_end"):
            run_id = str(event.get("run_id") or "")
            cursor = open_cursors.pop(run_id, None)
            if cursor is not None:
                recorder.record_unit_exited(cursor.with_committable(True), reason=None)

        return events

    def _nearest_parent_id(
        self,
        open_cursors: dict[str, ExecutionCursor],
        _ns: tuple[str, ...],
        default: str,
    ) -> str:
        """Parent for a new cursor = the most recent open workflow_node, else agent."""
        for cursor in reversed(open_cursors.values()):
            if cursor.unit_kind == UnitKind.WORKFLOW_NODE:
                return cursor.unit_id
        return default

    def _maybe_record_checkpoint(
        self,
        event: dict[str, Any],
        recorder: AgentRecorder,
        seen: set[str],
    ) -> None:
        """Emit a ``state_snapshot`` record when a new checkpoint_id appears.

        Each LangGraph event's ``metadata`` dict carries the current
        ``checkpoint_id`` (populated when a checkpointer is configured,
        which the bridge enforces at construction).  We dedupe against
        ids we've already recorded so each checkpoint shows up once.
        """
        metadata = event.get("metadata")
        if not isinstance(metadata, dict):
            return
        checkpoint_id = metadata.get("checkpoint_id")
        if isinstance(checkpoint_id, str) and checkpoint_id and checkpoint_id not in seen:
            seen.add(checkpoint_id)
            recorder.record_state_snapshot(ref=f"langgraph:{checkpoint_id}")

    def _serialize_framework_state(self) -> bytes:
        try:
            state = self._graph.get_state(self._config())
        except Exception:
            return b"{}"
        values = getattr(state, "values", None)
        if values is None:
            return b"{}"
        try:
            return json.dumps(_safe_values_for_serialization(values), default=str).encode()
        except (TypeError, ValueError):
            return b"{}"

    def _plan_interruption(self, delivered_text: str, mode: CancellationMode) -> InterruptionPlan:
        replacement = delivered_text + "..." if delivered_text else ""
        pre_ref = f"langgraph-pre-{self._thread_id}"
        post_ref = f"langgraph-post-{self._thread_id}"
        return InterruptionPlan(
            mutation_kind="interrupt_truncate",
            pre_state_ref=pre_ref,
            post_state_ref=post_ref,
            framework_instructions={
                "replacement": replacement,
                "delivered_text": delivered_text,
                "mode": mode.value,
            },
        )

    def _apply_planned_mutation(self, plan: InterruptionPlan) -> None:
        replacement = plan.framework_instructions["replacement"]
        self._rewrite_last_ai_message(replacement)

    def _rewrite_last_ai_message(self, replacement: str) -> None:
        """Replace the last AI message in graph state with ``replacement``.

        LangGraph's ``add_messages`` reducer dedupes by message ``id``,
        so re-sending the same AI message with an edited ``content``
        field replaces it in place instead of appending.  If no AI
        message exists yet (e.g. the graph hasn't produced one), this
        is a no-op.
        """
        try:
            state = self._graph.get_state(self._config())
        except Exception:
            logger.debug("rewrite_last_ai: get_state failed", exc_info=True)
            return
        values = getattr(state, "values", None) or {}
        key = self._messages_key or "messages"
        messages = values.get(key) if isinstance(values, dict) else None
        if not messages:
            return

        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if _message_is_ai(msg):
                content = _content_of(msg)
                if isinstance(content, list):
                    text_parts = [
                        p for p in content if isinstance(p, dict) and p.get("type") == "text"
                    ]
                    if text_parts:
                        originals = [str(p.get("text", "")) for p in text_parts]
                        splits = split_replacement_by_original_parts(originals, replacement)
                        for part, repl in zip(text_parts, splits):
                            part["text"] = repl
                    else:
                        _set_content(msg, replacement)
                else:
                    _set_content(msg, replacement)
                self._graph.update_state(self._config(), {key: [msg]})
                return


# ── Helpers ──────────────────────────────────────────────────────


def _get_checkpoint_id(state: Any) -> str | None:
    config = getattr(state, "config", None)
    if not isinstance(config, dict):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    cp = configurable.get("checkpoint_id")
    return str(cp) if cp else None


def _messages_tail(state: Any, key: str = "messages") -> Any:
    values = getattr(state, "values", None)
    if isinstance(values, dict):
        msgs = values.get(key)
        if msgs:
            return msgs[-1]
    return None


def _message_is_ai(msg: Any) -> bool:
    msg_type = getattr(msg, "type", None)
    if msg_type == "ai":
        return True
    if isinstance(msg, dict):
        return msg.get("role") == "assistant" or msg.get("type") == "ai"
    return False


def _content_of(msg: Any) -> Any:
    if isinstance(msg, dict):
        return msg.get("content", "")
    return getattr(msg, "content", "")


def _set_content(msg: Any, value: Any) -> None:
    if isinstance(msg, dict):
        msg["content"] = value
        return
    try:
        msg.content = value
    except (AttributeError, TypeError):
        object.__setattr__(msg, "content", value)


def _safe_values_for_serialization(values: Any) -> Any:
    """Reduce a LangGraph state-values dict to JSON-safe primitives.

    Messages are converted to ``{"role", "content"}`` dicts; other
    values are passed through and ``default=str`` in the caller handles
    the rest.
    """
    if isinstance(values, dict):
        out: dict[str, Any] = {}
        for k, v in values.items():
            if isinstance(v, list) and v and any(hasattr(m, "type") for m in v):
                out[k] = [_message_summary(m) for m in v]
            else:
                out[k] = v
        return out
    return values


def _message_summary(msg: Any) -> dict[str, Any]:
    role = ""
    msg_type = getattr(msg, "type", None)
    if msg_type == "ai":
        role = "assistant"
    elif msg_type == "human":
        role = "user"
    elif msg_type == "system":
        role = "system"
    elif msg_type == "tool":
        role = "tool"
    elif isinstance(msg, dict):
        role = msg.get("role") or msg.get("type") or ""
    content = _content_of(msg)
    return {"role": role, "content": content}


__all__: Sequence[str] = ["LangGraphBridge"]
