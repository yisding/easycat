"""LangChain bridge — wraps a ``Runnable`` via ``astream_events(version="v2")``.

Shallow integration suitable for LCEL chains, LangChain agents, or any
other composition that is *not* built with LangGraph (LangGraph graphs
get the deeper :class:`LangGraphBridge`).  The bridge surfaces text
deltas, tool calls, and unit transitions into the EasyCat journal so
voice-side debugging and barge-in work uniformly across agent
frameworks.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any
from uuid import uuid4

from easycat.cancel import CancelToken
from easycat.integrations.agents._context import normalize_context_messages
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


# No default ``include_types`` filter — LangChain's ``astream_events``
# filter drops ``on_custom_event`` (``dispatch_custom_event`` /
# ``adispatch_custom_event``) when ``include_types`` is set, which would
# silently break the custom-event TTS path documented in
# :func:`_custom_event_text`.  ``translate_stream_event`` already
# dispatches on the event *type* string, so unfiltered streams just
# produce more no-op events rather than spurious behaviour.  Callers
# that want to narrow the surface for performance can opt in via
# ``include_types=``.
_DEFAULT_INCLUDE_TYPES: tuple[str, ...] | None = None


class LangChainBridge:
    """Wraps a LangChain ``Runnable`` via ``astream_events(version="v2")``.

    Parameters
    ----------
    runnable:
        Any object implementing the LangChain ``Runnable`` protocol — an
        LCEL chain, a ``RunnableLambda``, a LangChain ``AgentExecutor``,
        etc.  Objects that are actually LangGraph ``CompiledStateGraph``
        instances should go through :class:`LangGraphBridge` instead;
        ``auto_adapt_agent()`` dispatches on the concrete type.
    display_name:
        Optional override for the top-level ``agent`` cursor display
        name (defaults to ``type(runnable).__name__``).
    input_key:
        Key under which ``turn_input.text`` is placed in the runnable's
        input dict.  Defaults to ``"input"`` (matching the LangChain
        Hub convention).  Pass ``None`` to pass the text as a bare
        string (useful for single-prompt runnables).
    history_key:
        Key under which the prior turn messages are placed.  Defaults
        to ``"history"``.  Set to ``None`` to disable history passing.
    messages_input:
        When ``True`` the runnable is fed a bare message *sequence*
        (``[*history, HumanMessage(text)]``) instead of a dict or string.
        Bare LangChain language models (``BaseChatModel`` / ``BaseLLM``
        such as ``ChatOpenAI(...)``) reject dict inputs, so
        ``auto_adapt_agent()`` enables this for them; ``input_key`` /
        ``history_key`` are ignored in this mode (history is threaded as
        messages instead).
    include_types:
        Optional ``astream_events(include_types=...)`` filter.  Defaults
        to ``None`` (surface every event) — narrowing the filter drops
        ``on_custom_event`` from ``dispatch_custom_event``, which would
        silently disable the custom-event TTS path.  Pass an explicit
        tuple only when performance demands it for very chatty chains.
    """

    COMMITTABLE_BOUNDARIES = {
        UnitKind.AGENT: CommitRule.BETWEEN_TURNS,
        UnitKind.MODEL_NODE: CommitRule.NON_COMMITTABLE,
        UnitKind.TOOL_CALL: CommitRule.BETWEEN_PHASES,
    }

    def __init__(
        self,
        runnable: Any,
        *,
        display_name: str | None = None,
        input_key: str | None = "input",
        history_key: str | None = "history",
        messages_input: bool = False,
        include_types: Sequence[str] | None = _DEFAULT_INCLUDE_TYPES,
    ) -> None:
        if runnable is None:
            raise BridgeInputError("LangChainBridge requires a non-None runnable=")
        if not hasattr(runnable, "astream_events"):
            raise BridgeInputError(
                "LangChainBridge requires a LangChain Runnable with astream_events(). "
                "Got: " + type(runnable).__name__
            )
        self._runnable = runnable
        self._display_name = display_name or type(runnable).__name__
        self._input_key = input_key
        self._history_key = history_key
        self._messages_input = messages_input
        self._include_types = list(include_types) if include_types is not None else None
        self._message_history: list[Any] = []
        self._last_output: Any = None

    # ── ExternalAgentBridge interface ─────────────────────────────

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
        # Open ``model_node`` cursors for ``on_chat_model_*`` events, keyed
        # by LangChain ``run_id`` so start/end always pair even when the
        # runnable interleaves multiple model calls.
        open_cursors: dict[str, ExecutionCursor] = {}
        # Run ids whose ``_end`` event arrived while a sibling cursor was
        # still on top of the recorder stack — closed in LIFO order once
        # the obstructing sibling(s) also end, preserving the recorder's
        # strict stack invariant for ``RunnableParallel`` / concurrent
        # runs.
        ended_runs: set[str] = set()
        # Track the top-level chain's ``run_id`` so we can capture its
        # ``on_chain_end.data.output`` as ``structured_output`` — non-text
        # runnables (``RunnableLambda(lambda _: {"answer": 42})``,
        # ``.with_structured_output(...)``) produce no ``text_delta`` and
        # would otherwise expose an empty string here.
        root_run_id: str | None = None
        captured_output: Any = None
        captured_output_set = False
        # Shared state for translator-side bookkeeping: tool-call dedup
        # across the chat_model ``tool_call_chunks`` path and the
        # ``on_tool_start`` / ``on_tool_end`` path, plus the set of chain
        # run-ids that have a model descendant (used to suppress chain
        # streams that would otherwise duplicate model tokens).
        tool_state: dict[str, Any] = {}

        input_payload = self._build_input(turn_input.text, turn_input.context)
        stream_kwargs: dict[str, Any] = {"version": "v2"}
        if self._include_types is not None:
            stream_kwargs["include_types"] = self._include_types

        try:
            stream = self._runnable.astream_events(input_payload, **stream_kwargs)
            async for event in stream:
                if cancel_token and cancel_token.is_cancelled:
                    recorder.record_cancellation_boundary(
                        mode=CancellationMode.IMMEDIATE_STOP,
                        reason="cancel_token_set",
                    )
                    break

                event_type = event.get("event") if isinstance(event, dict) else None
                if event_type == "on_chain_start" and not (event.get("parent_ids") or ()):
                    if root_run_id is None:
                        rid = str(event.get("run_id") or "")
                        if rid:
                            root_run_id = rid
                if event_type == "on_chain_end":
                    rid = str(event.get("run_id") or "")
                    if root_run_id is not None and rid == root_run_id:
                        raw_data = event.get("data")
                        data_dict = raw_data if isinstance(raw_data, dict) else {}
                        captured_output = data_dict.get("output") if data_dict else None
                        captured_output_set = True

                self._handle_cursor_lifecycle(
                    event, recorder, agent_cursor, open_cursors, ended_runs
                )
                for bridge_event in translate_stream_event(event, recorder, state=tool_state):
                    if bridge_event.kind == "text_delta":
                        accumulated += bridge_event.text
                    yield bridge_event
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

        # Prefer the top-level chain's actual output for ``structured_output``
        # (a dict / BaseModel / arbitrary value).  Fall back to the
        # accumulated text only when no ``on_chain_end`` was observed — e.g.
        # bare-chat-model runnables that never emit a chain event.
        self._last_output = captured_output if captured_output_set else accumulated
        self._append_to_history(turn_input.text, accumulated)
        recorder.record_unit_exited(agent_cursor.with_committable(True), reason=None)
        yield AgentBridgeEvent(
            kind="done",
            text=accumulated,
            structured_output=self._last_output,
        )

    def snapshot_state(self) -> FrameworkStateSnapshot:
        return FrameworkStateSnapshot(
            fields={
                "framework": "langchain",
                "runnable": self._display_name,
                "history_length": len(self._message_history),
            },
            kind="langchain",
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
        self._message_history.clear()
        self._last_output = None

    # ── History post-processing ───────────────────────────────────

    def replace_last_assistant_text(self, text: str) -> None:
        """Rewrite the last assistant message in history.

        Called by Session after post-processing (e.g. Markdown stripping)
        so the next turn conditions on cleaned text rather than raw LLM
        output.
        """
        self._rewrite_last_ai_content(text)

    def append_interruption_note(self, note: str) -> None:
        """Append an interruption note so the next turn sees it."""
        try:
            from langchain_core.messages import SystemMessage

            self._message_history.append(SystemMessage(content=note))
        except ImportError:
            self._message_history.append({"role": "system", "content": note})
        except Exception:
            logger.debug("Failed to append interruption note to LangChain history", exc_info=True)

    # ── Internal ─────────────────────────────────────────────────

    def _build_input(
        self,
        text: str,
        context: list[dict[str, str]] | None = None,
    ) -> Any:
        if self._messages_input:
            # Bare ``BaseChatModel`` / ``BaseLLM`` runnables only accept a
            # string or a message sequence — a dict raises ``Invalid input
            # type``.  Thread prior turns (and per-turn context) as
            # messages so the auto-adapted model stays conversational.
            history = self._history_with_context(context)
            return [*history, _context_to_message({"role": "user", "content": text})]
        if self._input_key is None:
            return text
        payload: dict[str, Any] = {self._input_key: text}
        if self._history_key is not None:
            payload[self._history_key] = self._history_with_context(context)
        return payload

    def _history_with_context(self, context: list[dict[str, str]] | None) -> list[Any]:
        """Prepend per-turn system/developer context messages to history.

        The bridge already owns prior conversation state via
        ``_message_history``; per-turn context from Session (caller-id
        metadata, system-prefix instructions, ``AgentTurnInput.context``)
        is forwarded for this single turn so prompts and agents that
        condition on it can see it.  User/assistant items in the caller's
        context are filtered out by ``normalize_context_messages`` to
        avoid duplicating our own history.
        """
        context_msgs = normalize_context_messages(context, own_history=True)
        if not context_msgs:
            return list(self._message_history)
        converted = [_context_to_message(item) for item in context_msgs]
        return [*converted, *self._message_history]

    def _append_to_history(self, user_text: str, assistant_text: str) -> None:
        """Extend message history after a successful turn.

        Uses LangChain's typed message classes when available, falling
        back to plain dicts otherwise.  The fallback path lets this
        bridge function against duck-typed test doubles that don't
        depend on ``langchain_core``.
        """
        try:
            from langchain_core.messages import AIMessage, HumanMessage

            self._message_history.append(HumanMessage(content=user_text))
            if assistant_text:
                self._message_history.append(AIMessage(content=assistant_text))
        except ImportError:
            self._message_history.append({"role": "user", "content": user_text})
            if assistant_text:
                self._message_history.append({"role": "assistant", "content": assistant_text})

    def _rewrite_last_ai_content(self, replacement: str) -> None:
        for i in range(len(self._message_history) - 1, -1, -1):
            msg = self._message_history[i]
            role = _role_of(msg)
            if role != "assistant":
                continue
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
                    return
            # Plain string or empty content — overwrite.
            _set_content(msg, replacement)
            return

    def _serialize_framework_state(self) -> bytes:
        try:
            payload = [
                {"role": _role_of(m), "content": _content_of(m)} for m in self._message_history
            ]
            return json.dumps(payload, default=str).encode()
        except (TypeError, ValueError):
            return b"[]"

    def _plan_interruption(self, delivered_text: str, mode: CancellationMode) -> InterruptionPlan:
        replacement = delivered_text + "..." if delivered_text else ""
        pre_ref = f"langchain-pre-{id(self._message_history):x}"
        post_ref = f"langchain-post-{id(self._message_history):x}"
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
        self._rewrite_last_ai_content(replacement)

    def _handle_cursor_lifecycle(
        self,
        event: dict[str, Any],
        recorder: AgentRecorder,
        agent_cursor: ExecutionCursor,
        open_cursors: dict[str, ExecutionCursor],
        ended_runs: set[str],
    ) -> None:
        """Open / close ``model_node`` cursors from chat-model events.

        Tool calls are recorded as ``tool_phase_changed`` records by the
        translator; they don't open a cursor.  ``_end`` events that arrive
        out of LIFO order (e.g. ``RunnableParallel`` running two chat
        models concurrently) are deferred via ``ended_runs`` and flushed
        through ``_close_top_ended_cursors`` so the recorder's strict
        stack invariant holds.
        """
        event_type = event.get("event")
        if event_type in ("on_chat_model_start", "on_llm_start"):
            run_id = str(event.get("run_id") or uuid4().hex[:8])
            if run_id in open_cursors:
                return
            cursor = ExecutionCursor(
                unit_id=f"model-{run_id}",
                unit_kind=UnitKind.MODEL_NODE,
                display_name=str(event.get("name") or "model"),
                parent_unit_id=agent_cursor.unit_id,
                entered_at=time.monotonic_ns(),
                committable=False,
            )
            recorder.record_unit_entered(cursor)
            open_cursors[run_id] = cursor
        elif event_type in ("on_chat_model_end", "on_llm_end"):
            run_id = str(event.get("run_id") or "")
            if run_id and run_id in open_cursors:
                ended_runs.add(run_id)
                _close_top_ended_cursors(recorder, open_cursors, ended_runs)


# ── Helpers ──────────────────────────────────────────────────────


def _close_top_ended_cursors(
    recorder: AgentRecorder,
    open_cursors: dict[str, ExecutionCursor],
    ended_runs: set[str],
) -> None:
    """Pop cursors from the top of the stack while they're marked ended.

    LangChain emits start/end events in chronological order, so for
    parallel branches (``RunnableParallel``, parallel LangGraph nodes)
    an ``_end`` event can arrive while a sibling cursor is still the
    top of ``JournalAgentRecorder``'s stack.  We hold each non-top close
    in ``ended_runs`` and flush them in LIFO order once the obstructing
    siblings above also end, so the recorder's strict stack invariant
    is preserved without dropping the cursor.
    """
    while open_cursors:
        last_run_id = next(reversed(open_cursors))
        if last_run_id not in ended_runs:
            break
        cursor = open_cursors.pop(last_run_id)
        ended_runs.discard(last_run_id)
        recorder.record_unit_exited(cursor.with_committable(True), reason=None)


def _role_of(msg: Any) -> str:
    """Best-effort role extraction for both dict and typed messages."""
    if isinstance(msg, dict):
        return str(msg.get("role") or msg.get("type") or "")
    msg_type = getattr(msg, "type", None)
    if msg_type == "ai":
        return "assistant"
    if msg_type == "human":
        return "user"
    if msg_type == "system":
        return "system"
    if msg_type == "tool":
        return "tool"
    return getattr(msg, "role", "") or ""


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


def _context_to_message(item: dict[str, str]) -> Any:
    """Convert a normalized ``{"role", "content"}`` dict to a LangChain message.

    Falls back to the dict itself when ``langchain_core`` is not
    importable — LangChain prompt templates accept both shapes for the
    placeholder-history pattern, and tests run without ``langchain_core``
    installed.
    """
    role = item.get("role", "system")
    content = item.get("content", "")
    try:
        from langchain_core.messages import (
            AIMessage,
            HumanMessage,
            SystemMessage,
            ToolMessage,
        )
    except ImportError:
        return {"role": role, "content": content}
    if role == "system" or role == "developer":
        return SystemMessage(content=content)
    if role == "user" or role == "human":
        return HumanMessage(content=content)
    if role == "assistant" or role == "ai":
        return AIMessage(content=content)
    if role == "tool":
        return ToolMessage(content=content, tool_call_id="")
    return SystemMessage(content=content)
