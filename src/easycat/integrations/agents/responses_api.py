"""RemoteResponsesAPIBridge — remote agent bridge using the OpenAI Responses API.

Speaks the ``/v1/responses`` HTTP+SSE protocol to a remote agent server,
translating streamed events into :class:`AgentBridgeEvent` instances.
Implements the full :class:`ExternalAgentBridge` protocol including
N-1 chain interruption and four-step atomic mutation writes.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from easycat.cancel import CancelToken
from easycat.integrations.agents._helpers import INTERRUPTION_NOTE
from easycat.integrations.agents._responses_api_events import (
    parse_sse_line,
    translate_sse_event,
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

logger = logging.getLogger(__name__)


class RemoteResponsesAPIBridge:
    """Bridge wrapping the OpenAI Responses API over HTTP+SSE.

    Implements :class:`ExternalAgentBridge` for remote agent execution.

    Parameters
    ----------
    base_url:
        Base URL of the Responses API server (e.g. ``https://api.openai.com``).
    model:
        Model identifier to include in requests.
    api_key:
        Bearer token for authentication.  Falls back to the
        ``EASYCAT_REMOTE_AGENT_API_KEY`` environment variable.
    timeout:
        HTTP request timeout in seconds.
    metadata:
        Optional metadata dict to include in every request.
    """

    COMMITTABLE_BOUNDARIES: dict[UnitKind | str, CommitRule] = {
        UnitKind.AGENT: CommitRule.BETWEEN_TURNS,
    }

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        timeout: float = 120.0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key or os.environ.get("EASYCAT_REMOTE_AGENT_API_KEY", "")
        self._timeout = timeout
        self._metadata = metadata or {}

        self._client = httpx.AsyncClient(timeout=timeout)
        self._client_closed = False

        # Conversation chaining state.
        self._last_completed_response_id: str | None = None
        self._response_count: int = 0

        # Interruption replay state.
        self._replay_items: list[dict[str, Any]] | None = None
        self._pending_interruption_note: str | None = None
        self._last_accumulated_items: list[dict[str, Any]] = []
        self._last_user_text: str | None = None
        self._interrupted_response_id: str | None = None
        self._pending_turn_metadata: dict[str, str] | None = None

    # ── ExternalAgentBridge interface ─────────────────────────────

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        agent_cursor = ExecutionCursor(
            unit_id=f"remote-{uuid4().hex[:8]}",
            unit_kind=UnitKind.AGENT,
            display_name=f"ResponsesAPI({self._model})",
            entered_at=time.monotonic_ns(),
            committable=False,
        )
        recorder.record_unit_entered(agent_cursor)
        yield AgentBridgeEvent(kind="cursor_entered", cursor=agent_cursor)

        self._last_user_text = turn_input.text

        body = self._build_request_body(turn_input)
        headers = self._build_headers()
        url = f"{self._base_url}/v1/responses"

        accumulated = ""
        accumulated_items: list[dict[str, Any]] = []
        pending_tool_calls: set[str] = set()
        pending_tool_names: dict[str, str] = {}
        interrupted = False
        response_id: str | None = None

        try:
            async with self._client.stream("POST", url, json=body, headers=headers) as response:
                response.raise_for_status()

                async for line in response.aiter_lines():
                    parsed = parse_sse_line(line)
                    if parsed is None:
                        continue

                    event_type, data = parsed

                    # Extract response_id from any event that carries it.
                    if "response" in data and isinstance(data["response"], dict):
                        rid = data["response"].get("id")
                        if rid:
                            response_id = rid

                    # Accumulate output items for potential replay.
                    if event_type == "response.output_item.done":
                        item = data.get("item", {})
                        if item:
                            accumulated_items.append(item)

                    # Handle cancellation.
                    if cancel_token and cancel_token.is_cancelled:
                        if not interrupted:
                            interrupted = True
                        if cancel_token.is_cancelled:
                            if pending_tool_calls:
                                # Drain: keep processing until tools complete.
                                bridge_ev = translate_sse_event(
                                    event_type, data, recorder, pending_tool_names
                                )
                                if bridge_ev is not None:
                                    if bridge_ev.kind == "tool_result":
                                        pending_tool_calls.discard(bridge_ev.call_id)
                                    yield bridge_ev
                                    if not pending_tool_calls:
                                        break
                                continue
                            else:
                                # Immediate stop.
                                break

                    # Normal event processing.
                    if event_type == "response.completed":
                        resp_data = data.get("response", {})
                        if resp_data.get("id"):
                            response_id = resp_data["id"]
                        break

                    if event_type == "response.failed":
                        error_info = data.get("response", {}).get("error", {})
                        error_msg = str(error_info.get("message", "unknown error"))
                        recorder.record_framework_error(
                            ErrorInfo(
                                type="ResponsesAPIError",
                                message=error_msg,
                            )
                        )
                        # Raise so Session treats this as an agent error.
                        # The except blocks below handle unit_exited recording.
                        raise RuntimeError(f"Responses API failed: {error_msg}")

                    bridge_ev = translate_sse_event(event_type, data, recorder, pending_tool_names)
                    if bridge_ev is not None:
                        if bridge_ev.kind == "text_delta":
                            accumulated += bridge_ev.text
                        elif bridge_ev.kind == "tool_started":
                            pending_tool_calls.add(bridge_ev.call_id)
                        elif bridge_ev.kind == "tool_result":
                            pending_tool_calls.discard(bridge_ev.call_id)
                        yield bridge_ev

        except httpx.HTTPStatusError as exc:
            recorder.record_framework_error(ErrorInfo.from_exception(exc))
            recorder.record_unit_exited(agent_cursor, reason="error")
            raise
        except Exception as exc:
            recorder.record_framework_error(ErrorInfo.from_exception(exc))
            recorder.record_unit_exited(agent_cursor, reason="error")
            raise

        # On successful (non-interrupted) completion, update chain state.
        if not interrupted and response_id:
            self._last_completed_response_id = response_id
            self._response_count += 1

        # Track the interrupted response ID for per-turn metadata.
        if interrupted and response_id:
            self._interrupted_response_id = response_id
        elif not interrupted:
            self._interrupted_response_id = None

        # Store accumulated items for potential interruption replay.
        self._last_accumulated_items = accumulated_items

        # Clear replay state since this turn succeeded (or was interrupted
        # but the caller will call apply_interruption separately).
        self._replay_items = None
        self._pending_interruption_note = None
        self._pending_turn_metadata = None

        recorder.record_unit_exited(agent_cursor.with_committable(True), reason=None)
        yield AgentBridgeEvent(
            kind="done",
            text=accumulated,
        )

    def snapshot_state(self) -> FrameworkStateSnapshot:
        parsed = urlparse(self._base_url)
        return FrameworkStateSnapshot(
            fields={
                "response_count": self._response_count,
                "last_completed_response_id": self._last_completed_response_id,
                "base_url_host": parsed.hostname or "",
                "model": self._model,
            },
            kind="remote_responses_api",
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
                # Journal in degraded mode -- skip mutation, runtime falls back.
                return

        # Step 3: apply the planned mutation.
        try:
            self._apply_planned_mutation(plan)
        except Exception as exc:
            # Step 4a: mutation failed -- write InterruptionApplyFailed.
            if recorder is not None:
                recorder.record_interruption_apply_failed(
                    mutation_kind=plan.mutation_kind,
                    pre_state_ref=actual_pre_ref,
                    post_state_ref=plan.post_state_ref,
                    failure_error=ErrorInfo.from_exception(exc),
                )
            raise

        # Step 4b: success -- persist post-mutation state and write boundary.
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

        # Store per-turn metadata for the next request so a server that
        # supports the EasyCat extension can use richer interruption semantics.
        turn_meta: dict[str, str] = {
            "easycat.delivered_text": delivered_text,
            "easycat.cancellation_mode": mode.value if hasattr(mode, "value") else str(mode),
        }
        if self._interrupted_response_id:
            turn_meta["easycat.interrupted_response_id"] = self._interrupted_response_id
        self._pending_turn_metadata = turn_meta

    def _serialize_framework_state(self) -> bytes:
        """Serialize bridge state for artifact storage."""
        try:
            return json.dumps(
                {
                    "accumulated_items": self._last_accumulated_items,
                    "user_text": self._last_user_text,
                    "interrupted_response_id": self._interrupted_response_id,
                },
                default=str,
            ).encode()
        except (TypeError, ValueError):
            return b"{}"

    def _plan_interruption(self, delivered_text: str, mode: CancellationMode) -> InterruptionPlan:
        truncated = delivered_text + "..." if delivered_text else ""
        pre_ref = f"responses-pre-{id(self):x}"
        post_ref = f"responses-post-{id(self):x}"
        return InterruptionPlan(
            mutation_kind="interrupt_n1_chain",
            pre_state_ref=pre_ref,
            post_state_ref=post_ref,
            framework_instructions={
                "delivered_text": delivered_text,
                "truncated_text": truncated,
                "accumulated_items": getattr(self, "_last_accumulated_items", []),
                "user_text": getattr(self, "_last_user_text", None),
            },
        )

    def _apply_planned_mutation(self, plan: InterruptionPlan) -> None:
        """Apply N-1 chain interruption.

        Reconstructs the interrupted turn as input items for the next
        ``invoke()`` call:
        - Original user message
        - Any completed tool calls from the interrupted turn
        - Truncated assistant text
        - A system note about the interruption

        Does NOT update ``_last_completed_response_id`` -- the next
        invoke chains from N-1 (the last fully-completed response).
        """
        instructions = plan.framework_instructions
        truncated = instructions.get("truncated_text", "")
        accumulated_items = instructions.get("accumulated_items", [])
        user_text = instructions.get("user_text")

        replay: list[dict[str, Any]] = []

        # Add the user message from the interrupted turn so the model
        # sees the question it was answering when interrupted.
        if user_text:
            replay.append({"role": "user", "content": user_text})

        # Add completed tool calls from interrupted turn.
        for item in accumulated_items:
            item_type = item.get("type", "")
            if item_type == "function_call":
                replay.append(
                    {
                        "type": "function_call",
                        "name": item.get("name", ""),
                        "call_id": item.get("call_id", item.get("id", "")),
                        "arguments": item.get("arguments", ""),
                    }
                )
            elif item_type == "function_call_output":
                replay.append(
                    {
                        "type": "function_call_output",
                        "call_id": item.get("call_id", item.get("id", "")),
                        "output": str(item.get("output", "")),
                    }
                )

        # Add truncated assistant text.
        if truncated:
            replay.append(
                {
                    "role": "assistant",
                    "content": truncated,
                }
            )

        self._replay_items = replay if replay else None
        self._pending_interruption_note = INTERRUPTION_NOTE

    async def aclose(self) -> None:
        """Close the underlying HTTP client, releasing connection pools."""
        self._client_closed = True
        await self._client.aclose()

    def __del__(self) -> None:
        if not self._client_closed:
            logger.warning(
                "RemoteResponsesAPIBridge was garbage-collected without aclose(). "
                "Call aclose() explicitly or use Session.stop() to avoid connection leaks."
            )

    def reset(self) -> None:
        self._last_completed_response_id = None
        self._response_count = 0
        self._replay_items = None
        self._pending_interruption_note = None
        self._last_accumulated_items = []
        self._last_user_text = None
        self._interrupted_response_id = None
        self._pending_turn_metadata = None

    # ── History post-processing ───────────────────────────────────

    def replace_last_assistant_text(self, text: str) -> None:
        """Queue a developer note describing the post-processed text.

        The Responses API chains by ``previous_response_id`` — local state
        cannot rewrite an already-committed response.  Instead we queue a
        developer message so the next turn sees what the user actually
        heard.  No-op when there is no prior completed response.
        """
        if self._last_completed_response_id is None:
            return
        note = (
            "[The assistant's last response was post-processed before delivery. "
            f'The user heard: "{text}"]'
        )
        if self._pending_interruption_note is not None:
            self._pending_interruption_note += "\n" + note
        else:
            self._pending_interruption_note = note

    def append_interruption_note(self, note: str) -> None:
        """Queue an interruption note for the next request."""
        if self._pending_interruption_note is not None:
            self._pending_interruption_note += "\n" + note
        else:
            self._pending_interruption_note = note

    # ── Internal helpers ─────────────────────────────────────────

    def _build_request_body(self, turn_input: AgentTurnInput) -> dict[str, Any]:
        """Build the JSON body for a ``POST /v1/responses`` request."""
        input_items: list[dict[str, Any]] = []

        # Seeded context (system prompt, prior messages) from the caller.
        if turn_input.context:
            input_items.extend(turn_input.context)

        # If we have replay items from an interrupted turn, prepend them.
        # Note: we do NOT clear _replay_items / _pending_interruption_note
        # here — they are cleared in invoke() only after the request
        # succeeds, so a transient HTTP failure doesn't lose them.
        if self._replay_items:
            input_items.extend(self._replay_items)

        # Add interruption note if pending.
        if self._pending_interruption_note:
            input_items.append(
                {
                    "role": "developer",
                    "content": self._pending_interruption_note,
                }
            )

        # Add the current user message.
        input_items.append(
            {
                "role": "user",
                "content": turn_input.text,
            }
        )

        body: dict[str, Any] = {
            "model": self._model,
            "input": input_items,
            "stream": True,
        }

        if self._last_completed_response_id:
            body["previous_response_id"] = self._last_completed_response_id

        if self._metadata or self._pending_turn_metadata:
            merged = {**self._metadata}
            if self._pending_turn_metadata:
                merged.update(self._pending_turn_metadata)
            body["metadata"] = merged

        return body

    def _build_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers
