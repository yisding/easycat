"""OpenAI Agents SDK bridge for the debug-first runtime.

Implements :class:`ExternalAgentBridge` on top of the ``agents`` package
and records execution state to the journal via :class:`AgentRecorder`.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from easycat.cancel import CancelToken
from easycat.integrations.agents._context import normalize_context_messages
from easycat.integrations.agents._helpers import split_replacement_by_original_parts
from easycat.integrations.agents._openai_agents_events import (
    extract_text_delta,
    extract_tool_delta,
    map_run_item,
)
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
    CancellationMode,
    CommitRule,
    ExecutionCursor,
    FrameworkStateSnapshot,
    InterruptionPlan,
    UnitKind,
)
from easycat.runtime.records import ErrorInfo

try:
    from agents import Runner  # type: ignore[import-untyped]
except ImportError:
    Runner = None  # type: ignore[assignment,misc]


class OpenAIAgentsBridge:
    """Bridge wrapping an OpenAI Agents SDK ``Agent``.

    Implements ``ExternalAgentBridge`` while capturing agent transitions,
    tool calls, and handoffs to the journal via the ``AgentRecorder``.
    """

    COMMITTABLE_BOUNDARIES = {
        UnitKind.TOOL_CALL: CommitRule.BETWEEN_PHASES,
        UnitKind.MODEL_NODE: CommitRule.NON_COMMITTABLE,
        UnitKind.AGENT: CommitRule.BETWEEN_TURNS,
    }

    def __init__(
        self,
        agent: Any,
        *,
        run_config: Any = None,
        context: Any = None,
        use_previous_response_id: bool = True,
        max_turns: int | None = None,
        hooks: Any = None,
        mcp_servers: list[Any] | None = None,
    ) -> None:
        self._agent = agent
        self._original_agent = agent
        self._run_config = run_config
        self._context = context
        self._use_previous_response_id = use_previous_response_id
        self._max_turns = max_turns
        self._hooks = hooks
        self._mcp_servers = mcp_servers
        self._previous_response_id: str | None = None
        self._pending_interruption: str | None = None
        self._message_history: list[Any] = []
        self._last_output: Any = None

    # ── ExternalAgentBridge interface ─────────────────────────────

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        if Runner is None:
            raise ImportError(
                "openai-agents package is required: pip install 'easycat[openai-agents]'"
            )

        agent_cursor = ExecutionCursor(
            unit_id=f"agent-{uuid4().hex[:8]}",
            unit_kind=UnitKind.AGENT,
            display_name=getattr(self._agent, "name", "OpenAIAgent"),
            entered_at=time.monotonic_ns(),
            committable=False,
        )
        recorder.record_unit_entered(agent_cursor)
        yield AgentBridgeEvent(kind="cursor_entered", cursor=agent_cursor)

        saved_mcp_servers = getattr(self._agent, "mcp_servers", None)
        try:
            input_data = self._build_input(turn_input)
            kwargs = self._build_kwargs()
            if self._mcp_servers is not None and hasattr(self._agent, "mcp_servers"):
                self._agent.mcp_servers = list(self._mcp_servers)
            result = Runner.run_streamed(self._agent, input_data, **kwargs)
        except Exception as exc:
            if hasattr(self._agent, "mcp_servers"):
                self._agent.mcp_servers = saved_mcp_servers
            recorder.record_framework_error(ErrorInfo.from_exception(exc))
            recorder.record_unit_exited(agent_cursor, reason="error")
            raise

        accumulated = ""
        pending_tool_calls: dict[str, str] = {}
        interrupted = False
        cursor_exited = False

        try:
            async for event in result.stream_events():
                if cancel_token and cancel_token.is_cancelled:
                    if not interrupted:
                        interrupted = True
                    if pending_tool_calls:
                        if event.type == "run_item_stream_event":
                            bridge_ev = map_run_item(event.item, recorder, pending_tool_calls)
                            if bridge_ev is not None:
                                yield bridge_ev
                                if not pending_tool_calls:
                                    break
                        elif event.type == "raw_response_event":
                            bridge_ev = extract_tool_delta(event.data)
                            if bridge_ev is not None:
                                yield bridge_ev
                        continue
                    else:
                        break

                if event.type == "raw_response_event":
                    delta = extract_text_delta(event.data)
                    if delta:
                        accumulated += delta
                        yield AgentBridgeEvent(kind="text_delta", text=delta)
                    else:
                        bridge_ev = extract_tool_delta(event.data)
                        if bridge_ev is not None:
                            yield bridge_ev
                elif event.type == "run_item_stream_event":
                    bridge_ev = map_run_item(event.item, recorder, pending_tool_calls)
                    if bridge_ev is not None:
                        yield bridge_ev
        except Exception as exc:
            recorder.record_framework_error(ErrorInfo.from_exception(exc))
            recorder.record_unit_exited(agent_cursor, reason="error")
            cursor_exited = True
            raise
        finally:
            if hasattr(self._agent, "mcp_servers"):
                self._agent.mcp_servers = saved_mcp_servers
            self._message_history = result.to_input_list()
            if self._use_previous_response_id:
                self._previous_response_id = getattr(result, "last_response_id", None)
            if not cursor_exited:
                last_agent = getattr(result, "last_agent", None)
                if last_agent is not None and last_agent is not self._agent:
                    # Record handoff.
                    old_name = getattr(self._agent, "name", "unknown")
                    new_name = getattr(last_agent, "name", "unknown")
                    recorder.record_unit_exited(agent_cursor, reason="handoff")
                    recorder.record_framework_handoff(
                        from_unit=old_name,
                        to_unit=new_name,
                        reason="agent_handoff",
                    )
                    self._agent = last_agent
                    # Enter new agent cursor for the handoff target.
                    new_cursor = ExecutionCursor(
                        unit_id=f"agent-{uuid4().hex[:8]}",
                        unit_kind=UnitKind.AGENT,
                        display_name=new_name,
                        entered_at=time.monotonic_ns(),
                        committable=True,
                    )
                    recorder.record_unit_entered(new_cursor)
                    recorder.record_unit_exited(new_cursor.with_committable(True), reason=None)
                else:
                    recorder.record_unit_exited(agent_cursor.with_committable(True), reason=None)

        self._last_output = getattr(result, "final_output", None)
        yield AgentBridgeEvent(
            kind="done",
            text=accumulated,
            structured_output=self._last_output,
        )

    def snapshot_state(self) -> FrameworkStateSnapshot:
        return FrameworkStateSnapshot(
            fields={
                "agent": getattr(self._agent, "name", "unknown"),
                "previous_response_id": self._previous_response_id,
                "turn_count": len(self._message_history),
            },
            kind="openai_agents",
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

        # Step 1b: persist pre-mutation state snapshot.
        actual_pre_ref = plan.pre_state_ref
        if recorder is not None:
            actual_pre_ref = recorder.record_state_snapshot(
                plan.pre_state_ref,
                payload=self._serialize_framework_state(),
            )

        # Step 2: write FrameworkStateCommitted to the journal.
        if recorder is not None:
            try:
                recorder.record_state_committed(
                    mutation_kind=plan.mutation_kind,
                    pre_state_ref=actual_pre_ref,
                    post_state_ref=plan.post_state_ref,
                )
            except Exception:
                # Journal in degraded mode — skip mutation, runtime falls back.
                return

        # Step 3: apply the planned mutation.
        try:
            self._apply_planned_mutation(plan)
        except Exception as exc:
            # Step 4a: mutation failed — write InterruptionApplyFailed.
            if recorder is not None:
                recorder.record_interruption_apply_failed(
                    mutation_kind=plan.mutation_kind,
                    pre_state_ref=actual_pre_ref,
                    post_state_ref=plan.post_state_ref,
                    failure_error=ErrorInfo.from_exception(exc),
                )
            raise

        # Step 4b: success — persist post-mutation state and write boundary.
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

    def _serialize_framework_state(self) -> bytes:
        """Serialize message history for artifact storage."""
        try:
            return json.dumps(self._message_history, default=str).encode()
        except (TypeError, ValueError):
            return b"[]"

    def _plan_interruption(self, delivered_text: str, mode: CancellationMode) -> InterruptionPlan:
        replacement = delivered_text + "..." if delivered_text else ""
        pre_ref = f"openai-pre-{id(self._message_history):x}"
        post_ref = f"openai-post-{id(self._message_history):x}"
        return InterruptionPlan(
            mutation_kind="interrupt_truncate",
            pre_state_ref=pre_ref,
            post_state_ref=post_ref,
            framework_instructions={"replacement": replacement},
        )

    def _apply_planned_mutation(self, plan: InterruptionPlan) -> None:
        replacement = plan.framework_instructions["replacement"]
        for i in range(len(self._message_history) - 1, -1, -1):
            item = self._message_history[i]
            role = item.get("role") if isinstance(item, dict) else getattr(item, "role", None)
            if role == "assistant":
                if isinstance(item, dict) and "content" in item:
                    content = item["content"]
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "output_text":
                                parts_list = [
                                    p
                                    for p in content
                                    if isinstance(p, dict) and p.get("type") == "output_text"
                                ]
                                originals = [str(p.get("text", "")) for p in parts_list]
                                replacements = split_replacement_by_original_parts(
                                    originals, replacement
                                )
                                for p, r in zip(parts_list, replacements):
                                    p["text"] = r
                                break
                    elif isinstance(content, str):
                        item["content"] = replacement
                elif not isinstance(item, dict) and hasattr(item, "content"):
                    item.content = replacement
                break
        if self._use_previous_response_id and self._previous_response_id is not None:
            self._pending_interruption = (
                "[The user interrupted the assistant's response. "
                f'They approximately heard: "{replacement}"]'
            )

    def reset(self) -> None:
        self._agent = self._original_agent
        self._message_history.clear()
        self._previous_response_id = None
        self._pending_interruption = None
        self._last_output = None

    # ── History post-processing ───────────────────────────────────

    def replace_last_assistant_text(self, text: str) -> None:
        """Rewrite the last assistant entry in ``_message_history`` to ``text``.

        Called by the adapter shim after post-processing (e.g. Markdown
        stripping) so that subsequent turns condition on the cleaned text
        rather than the raw LLM output.
        """
        original: str | None = None
        for i in range(len(self._message_history) - 1, -1, -1):
            item = self._message_history[i]
            role = item.get("role") if isinstance(item, dict) else getattr(item, "role", None)
            if role != "assistant":
                continue
            if isinstance(item, dict) and "content" in item:
                content = item["content"]
                if isinstance(content, list):
                    parts_list = [
                        p
                        for p in content
                        if isinstance(p, dict) and p.get("type") == "output_text"
                    ]
                    if parts_list:
                        originals = [str(p.get("text", "")) for p in parts_list]
                        original = "".join(originals)
                        replacements = split_replacement_by_original_parts(originals, text)
                        for p, r in zip(parts_list, replacements):
                            p["text"] = r
                elif isinstance(content, str):
                    original = content
                    item["content"] = text
            elif not isinstance(item, dict) and hasattr(item, "content"):
                original = getattr(item, "content", None)
                item.content = text
            break

        # When chaining by response_id the server maintains its own
        # conversation and won't see local history edits.  Queue a
        # developer note so the next turn informs the model about what
        # the user actually heard.
        if (
            self._use_previous_response_id
            and self._previous_response_id is not None
            and original is not None
            and original != text
        ):
            note = (
                "[The assistant's last response was post-processed before delivery. "
                f'The user heard: "{text}"]'
            )
            if self._pending_interruption is not None:
                self._pending_interruption += "\n" + note
            else:
                self._pending_interruption = note

    def append_interruption_note(self, note: str) -> None:
        """Append an interruption note so the next turn sees it.

        Appends a ``developer``-role message to ``_message_history`` for
        the full-history code path, and also stores it as
        ``_pending_interruption`` so that the response-id chaining path
        in ``_build_input`` surfaces it on the next turn.
        """
        self._message_history.append({"role": "developer", "content": note})
        if self._use_previous_response_id:
            self._pending_interruption = note

    # ── Internal helpers ─────────────────────────────────────────

    def _build_input(self, turn_input: AgentTurnInput | str) -> Any:
        if isinstance(turn_input, AgentTurnInput):
            text = turn_input.text
            raw_context = turn_input.context
        else:
            text = str(turn_input)
            raw_context = []
        own_history = bool(self._message_history) or (
            self._use_previous_response_id and self._previous_response_id is not None
        )
        context = normalize_context_messages(raw_context, own_history=own_history)
        user_message = {"role": "user", "content": text}
        if self._use_previous_response_id and self._previous_response_id is not None:
            parts: list[dict[str, str]] = []
            parts.extend(context)
            if self._pending_interruption is not None:
                parts.append({"role": "developer", "content": self._pending_interruption})
                self._pending_interruption = None
            parts.append(user_message)
            return parts
        if context or self._message_history:
            return [*context, *self._message_history, user_message]
        return text

    def _build_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if self._run_config is not None:
            kwargs["run_config"] = self._run_config
        if self._context is not None:
            kwargs["context"] = self._context
        if self._use_previous_response_id:
            if self._previous_response_id is not None:
                kwargs["previous_response_id"] = self._previous_response_id
            kwargs["auto_previous_response_id"] = True
        if self._max_turns is not None:
            kwargs["max_turns"] = self._max_turns
        if self._hooks is not None:
            kwargs["hooks"] = self._hooks
        return kwargs
