"""LangGraph bridge — wraps a ``CompiledStateGraph`` with checkpointer support.

Deep integration via ``graph.astream(stream_mode=["updates", "messages",
"custom", "debug"], subgraphs=True)``.  Node transitions become
``workflow_node`` cursors; LLM tokens feed through the shared
``_langchain_events.translate_message_chunk`` translator.  Checkpoint
IDs flow from ``graph.get_state(config)`` into the journal so
LangGraph's native ``checkpoint_id`` vocabulary is preserved alongside
EasyCat's own monotonic ``sequence`` numbers.

Interruption patches the last AI message via LangGraph's native
``update_state(config, values, as_node=None)``.  Because LangGraph's
``add_messages`` reducer dedupes by message ``id``, we re-send the
edited message under the same id so it replaces instead of appending.
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
from easycat.integrations.agents._base_adapter import split_replacement_by_original_parts
from easycat.integrations.agents._langchain_events import translate_message_chunk
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
    ) -> None:
        if graph is None:
            raise BridgeInputError("LangGraphBridge requires a non-None graph=")
        if not hasattr(graph, "astream"):
            raise BridgeInputError(
                "LangGraphBridge requires a compiled LangGraph graph with "
                "astream() — got: " + type(graph).__name__
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
        open_node_cursors: dict[tuple[str, ...], dict[str, ExecutionCursor]] = {}
        last_node_by_ns: dict[tuple[str, ...], str] = {}

        config = self._config()
        input_payload = self._build_input(turn_input.text)

        try:
            stream = self._graph.astream(
                input_payload,
                config=config,
                stream_mode=["updates", "messages", "custom", "debug"],
                subgraphs=True,
            )
            async for item in stream:
                if cancel_token and cancel_token.is_cancelled:
                    recorder.record_cancellation_boundary(
                        mode=CancellationMode.IMMEDIATE_STOP,
                        reason="cancel_token_set",
                    )
                    break

                ns, stream_mode, payload = _unpack_stream_item(item)

                if stream_mode == "updates" and isinstance(payload, dict):
                    for bridge_event in self._handle_updates(
                        ns,
                        payload,
                        recorder,
                        agent_cursor,
                        open_node_cursors,
                        last_node_by_ns,
                    ):
                        yield bridge_event
                elif stream_mode == "messages":
                    chunk = payload[0] if isinstance(payload, tuple) else payload
                    for bridge_event in translate_message_chunk(chunk, recorder):
                        if bridge_event.kind == "text_delta":
                            accumulated += bridge_event.text
                        yield bridge_event
                elif stream_mode == "custom":
                    # User-emitted data from `get_stream_writer()`;
                    # surface as a tool_delta so the UI can observe it
                    # without it being fed to TTS.
                    yield AgentBridgeEvent(
                        kind="tool_delta",
                        tool_name="custom",
                        text=_safe_repr(payload),
                    )
                elif stream_mode == "debug" and isinstance(payload, dict):
                    # Debug payloads carry ``{"type": "task"|"task_result"|
                    # "checkpoint", "payload": {...}}`` entries; we only
                    # care about checkpoints for state snapshot records.
                    if payload.get("type") == "checkpoint":
                        inner = payload.get("payload") or {}
                        cfg = inner.get("config") or {}
                        checkpoint_id = (
                            cfg.get("configurable", {}).get("checkpoint_id")
                            if isinstance(cfg, dict)
                            else None
                        )
                        if checkpoint_id:
                            recorder.record_state_snapshot(ref=f"langgraph:{checkpoint_id}")
        except Exception as exc:
            # Close any still-open node cursors (deepest-last per ns).
            for ns_key, cursors in list(open_node_cursors.items()):
                for cursor in reversed(list(cursors.values())):
                    try:
                        recorder.record_unit_exited(cursor, reason="error")
                    except Exception:
                        logger.debug("Failed to close node cursor on error", exc_info=True)
                open_node_cursors.pop(ns_key, None)
            recorder.record_framework_error(ErrorInfo.from_exception(exc))
            recorder.record_unit_exited(agent_cursor, reason="error")
            raise

        # Close any remaining open node cursors after the stream drains.
        for ns_key, cursors in list(open_node_cursors.items()):
            for cursor in reversed(list(cursors.values())):
                recorder.record_unit_exited(cursor.with_committable(True), reason=None)
            open_node_cursors.pop(ns_key, None)

        # Surface the final checkpoint as a state snapshot + record the
        # committable agent exit.
        try:
            final_state = self._graph.get_state(config)
            checkpoint_id = _get_checkpoint_id(final_state)
            if checkpoint_id:
                recorder.record_state_snapshot(ref=f"langgraph:{checkpoint_id}")
            self._last_output = _messages_tail(final_state)
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

    def _handle_updates(
        self,
        ns: tuple[str, ...],
        payload: dict[str, Any],
        recorder: AgentRecorder,
        agent_cursor: ExecutionCursor,
        open_node_cursors: dict[tuple[str, ...], dict[str, ExecutionCursor]],
        last_node_by_ns: dict[tuple[str, ...], str],
    ) -> list[AgentBridgeEvent]:
        """Translate one ``stream_mode="updates"`` payload into cursors.

        LangGraph emits ``{node_name: state_delta}`` dicts per
        super-step.  We treat each key as a ``workflow_node`` cursor
        and emit handoff triples whenever the node changes within the
        same namespace.
        """
        events: list[AgentBridgeEvent] = []
        cursors = open_node_cursors.setdefault(ns, {})
        for node_name, _delta in payload.items():
            if not isinstance(node_name, str):
                continue
            # Skip sentinel/internal keys.
            if node_name in ("__start__", "__end__"):
                continue

            prev = last_node_by_ns.get(ns)
            if prev and prev != node_name and prev in cursors:
                prev_cursor = cursors.pop(prev)
                recorder.record_unit_exited(prev_cursor.with_committable(True), reason=None)
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

            if node_name not in cursors:
                parent_id = agent_cursor.unit_id
                # Nested subgraphs: attach to the innermost still-open
                # parent cursor in the enclosing namespace.
                for depth in range(len(ns) - 1, -1, -1):
                    parent_ns = ns[: depth + 1]
                    parent_map = open_node_cursors.get(parent_ns)
                    if parent_map:
                        parent_id = next(reversed(parent_map.values())).unit_id
                        break
                cursor = ExecutionCursor(
                    unit_id=f"node-{uuid4().hex[:8]}",
                    unit_kind=UnitKind.WORKFLOW_NODE,
                    display_name=node_name,
                    parent_unit_id=parent_id,
                    entered_at=time.monotonic_ns(),
                    committable=False,
                )
                recorder.record_unit_entered(cursor)
                cursors[node_name] = cursor
            last_node_by_ns[ns] = node_name
        return events

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


def _unpack_stream_item(item: Any) -> tuple[tuple[str, ...], str, Any]:
    """Normalise a chunk from ``astream(stream_mode=[...], subgraphs=True)``.

    With ``subgraphs=True`` + multiple stream modes, LangGraph yields
    ``(ns_tuple, stream_mode, payload)``.  With only subgraphs,
    ``(ns_tuple, payload)``.  This helper smooths over the difference
    so callers always get ``(ns, mode, payload)``.
    """
    if isinstance(item, tuple):
        if len(item) == 3:
            ns, mode, payload = item
            if isinstance(ns, tuple) and isinstance(mode, str):
                return ns, mode, payload
        if len(item) == 2:
            first, second = item
            if isinstance(first, tuple) and isinstance(second, str):
                # (ns, mode) — no payload, unusual but tolerated.
                return first, second, None
            if isinstance(first, str):
                # (mode, payload) — subgraphs=False path.
                return (), first, second
            if isinstance(first, tuple):
                # (ns, payload) — single mode + subgraphs=True.
                return first, "", second
    return (), "", item


def _get_checkpoint_id(state: Any) -> str | None:
    config = getattr(state, "config", None)
    if not isinstance(config, dict):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    cp = configurable.get("checkpoint_id")
    return str(cp) if cp else None


def _messages_tail(state: Any) -> Any:
    values = getattr(state, "values", None)
    if isinstance(values, dict):
        msgs = values.get("messages")
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


def _safe_repr(value: Any) -> str:
    try:
        if isinstance(value, (str, int, float, bool)):
            return str(value)
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return repr(value)


__all__: Sequence[str] = ["LangGraphBridge"]
