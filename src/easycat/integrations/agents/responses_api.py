"""RemoteResponsesAPIBridge — remote agent bridge using the OpenAI Responses API.

Speaks the ``/v1/responses`` HTTP+SSE protocol to a remote agent server,
translating streamed events into :class:`AgentBridgeEvent` instances.
Implements the full :class:`ExternalAgentBridge` protocol including
N-1 chain interruption and four-step atomic mutation writes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from typing import Any, ClassVar
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from easycat.cancel import CancelToken
from easycat.integrations.agents._context import normalize_context_messages
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
    run_interruption_journal_protocol,
)
from easycat.runtime._event_tasks import RuntimeTaskScope
from easycat.runtime.records import ErrorInfo
from easycat.teardown_budgets import (
    REMOTE_RESPONSES_COMPLETED_STREAM_DRAIN_TIMEOUT_S as _COMPLETED_STREAM_DRAIN_TIMEOUT_S,
)

logger = logging.getLogger(__name__)

_STREAM_RACE_TASK = "responses_stream_race"
_STREAM_RACE_COHORT = "responses-stream-race"


class _ResponseStreamCancelled(Exception):
    """Internal control flow for cooperative cancellation of an idle SSE read."""


def _response_id_from_event(data: Mapping[str, Any]) -> str | None:
    """Return a usable response ID from one SSE event."""
    response = data.get("response")
    if not isinstance(response, Mapping):
        return None
    response_id = response.get("id")
    if not isinstance(response_id, str) or not response_id.strip():
        return None
    return response_id


def _response_failure_message(data: Mapping[str, Any]) -> str:
    """Extract a safe failure message from an untrusted SSE payload."""
    response = data.get("response")
    if not isinstance(response, Mapping):
        return "unknown error"
    error = response.get("error")
    if not isinstance(error, Mapping):
        return "unknown error"
    message = error.get("message")
    return message if isinstance(message, str) else "unknown error"


def _raise_protocol_error(recorder: AgentRecorder, message: str) -> None:
    recorder.record_framework_error(
        ErrorInfo(
            type="ResponsesAPIError",
            message=message,
        )
    )
    raise RuntimeError(message)


def _update_tool_lifecycle(
    event: AgentBridgeEvent,
    recorder: AgentRecorder,
    pending_tool_calls: set[str],
    seen_tool_call_ids: set[str],
) -> None:
    """Validate and update the exact start → delta/result tool lifecycle."""
    if event.kind not in {"tool_started", "tool_delta", "tool_result"}:
        return

    call_id = event.call_id
    if not isinstance(call_id, str) or not call_id.strip():
        _raise_protocol_error(
            recorder,
            f"Responses API {event.kind} arrived without a nonblank call_id",
        )

    if event.kind == "tool_started":
        if call_id in seen_tool_call_ids:
            _raise_protocol_error(
                recorder,
                f"Responses API duplicate tool_started for call_id {call_id!r}",
            )
        seen_tool_call_ids.add(call_id)
        pending_tool_calls.add(call_id)
        return

    if call_id not in pending_tool_calls:
        _raise_protocol_error(
            recorder,
            f"Responses API orphan {event.kind} for call_id {call_id!r}",
        )
    if event.kind == "tool_result":
        pending_tool_calls.remove(call_id)


async def _wait_for_cancel(cancel_token: CancelToken) -> None:
    await cancel_token.wait()


async def _aiter_lines_with_cancellation(
    source: AsyncIterator[str],
    cancel_token: CancelToken | None,
    *,
    should_drain: Callable[[], bool],
) -> AsyncGenerator[str, None]:
    """Yield SSE lines while waking an idle read when cancellation fires.

    Once a tool call has started, ``should_drain`` keeps the stream alive until
    the corresponding tool result arrives. Otherwise cancellation wins a race
    with the next line read. Every task and the source iterator are cleaned up
    on cooperative cancellation, hard task cancellation, or consumer close.
    """
    iterator = source.__aiter__()
    exhausted = False
    cancel_wait: asyncio.Task[None] | None = None
    next_line: asyncio.Task[str] | None = None
    race_tasks = RuntimeTaskScope(
        owner_label="responses-api-stream-race",
        member_name=_STREAM_RACE_TASK,
        cohort=_STREAM_RACE_COHORT,
        logger=logger,
        failure_message="Responses API stream race task failed",
        drop_if_closed=False,
    )

    if cancel_token is not None:
        cancel_wait = race_tasks.create_task(
            _wait_for_cancel(cancel_token),
            task_name="easycat-responses-stream-cancel",
        )
        assert cancel_wait is not None

    try:
        while True:
            if cancel_token is not None and cancel_token.is_cancelled:
                if not should_drain():
                    raise _ResponseStreamCancelled
                try:
                    line = await iterator.__anext__()
                except StopAsyncIteration:
                    exhausted = True
                    return
                yield line
                continue

            if cancel_wait is None:
                try:
                    line = await iterator.__anext__()
                except StopAsyncIteration:
                    exhausted = True
                    return
                yield line
                continue

            next_line = race_tasks.create_awaitable_task(
                iterator.__anext__(),
                task_name="easycat-responses-stream-next",
            )
            assert next_line is not None
            await asyncio.wait(
                (next_line, cancel_wait),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_token is not None and cancel_token.is_cancelled:
                if not should_drain():
                    raise _ResponseStreamCancelled
                try:
                    line = await next_line
                except StopAsyncIteration:
                    exhausted = True
                    return
            else:
                try:
                    line = await next_line
                except StopAsyncIteration:
                    exhausted = True
                    return
            race_tasks.discard_task(next_line)
            next_line = None
            yield line
    finally:
        try:
            if next_line is not None:
                if not next_line.done():
                    next_line.cancel()
                with contextlib.suppress(
                    asyncio.CancelledError,
                    StopAsyncIteration,
                    Exception,
                ):
                    await next_line
                race_tasks.discard_task(next_line)
            if cancel_wait is not None:
                cancel_wait.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_wait
                race_tasks.discard_task(cancel_wait)
        finally:
            try:
                await race_tasks.release_standalone_if_empty()
            finally:
                if not exhausted:
                    aclose = getattr(iterator, "aclose", None)
                    if aclose is not None:
                        with contextlib.suppress(Exception):
                            await aclose()


async def _drain_completed_stream(lines: AsyncIterator[str]) -> None:
    """Bound normal terminal cleanup while giving the HTTP stream a chance to reach EOF."""
    try:
        async with asyncio.timeout(_COMPLETED_STREAM_DRAIN_TIMEOUT_S):
            async for _ in lines:
                pass
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001, S110 intentional boundary or best-effort cleanup
        pass


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
    reasoning_effort:
        Optional Responses API reasoning effort. Leave unset to preserve the
        selected model's default; latency-sensitive callers can pass ``"none"``.
    """

    COMMITTABLE_BOUNDARIES: ClassVar[Mapping[UnitKind | str, CommitRule]] = {
        UnitKind.AGENT: CommitRule.BETWEEN_TURNS,
    }
    MANAGES_CONVERSATION_HISTORY: ClassVar[bool] = True

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        timeout: float = 120.0,
        metadata: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key or os.environ.get("EASYCAT_REMOTE_AGENT_API_KEY", "")
        self._timeout = timeout
        self._metadata = metadata or {}
        self._reasoning_effort = reasoning_effort

        self._client = httpx.AsyncClient(timeout=timeout)
        self._client_closed = False

        # Conversation chaining state.
        self._last_completed_response_id: str | None = None
        self._completed_response_ids: list[str] = []
        self._response_count: int = 0

        # Interruption replay state.
        self._replay_items: list[dict[str, Any]] | None = None
        self._pending_interruption_note: str | None = None
        self._pending_assistant_history_items: list[dict[str, Any]] = []
        self._last_accumulated_items: list[dict[str, Any]] = []
        self._last_user_text: str | None = None
        self._last_turn_response_id: str | None = None
        self._last_turn_replay_items: list[dict[str, Any]] = []
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
        self._last_user_text = turn_input.text
        self._last_turn_response_id = None
        self._last_accumulated_items = []
        self._last_turn_replay_items = [
            dict(item) for item in self._replay_items or () if isinstance(item, Mapping)
        ]
        self._interrupted_response_id = None

        body = self._build_request_body(turn_input)
        headers = self._build_headers()
        url = f"{self._base_url}/v1/responses"

        accumulated = ""
        accumulated_items: list[dict[str, Any]] = []
        pending_tool_calls: set[str] = set()
        seen_tool_call_ids: set[str] = set()
        pending_tool_names: dict[str, str] = {}
        interrupted = False
        completed = False
        response_id: str | None = None
        # Keep the public interruption snapshot live as items arrive so a hard
        # async-generator close cannot expose the prior turn's tool history.
        self._last_accumulated_items = accumulated_items

        # ``turn_cursor`` centralizes enter → error → BaseException → clean
        # exit.  ``httpx.HTTPStatusError`` and the ``response.failed``
        # ``RuntimeError`` are both ``Exception`` so the cm's single ``except
        # Exception`` arm subsumes them; the ``BaseException`` arm closes the
        # cursor on ``AgentRunner`` timeout / barge-in cancellation.  The
        # post-loop chain-state updates stay inside the ``with`` block so the
        # cm's clean ``unit_exited`` still fires last.
        with recorder.turn_cursor(agent_cursor):
            async with self._client.stream("POST", url, json=body, headers=headers) as response:
                response.raise_for_status()

                lines = _aiter_lines_with_cancellation(
                    response.aiter_lines(),
                    cancel_token,
                    should_drain=lambda: bool(pending_tool_calls),
                )
                try:
                    async with contextlib.aclosing(lines):
                        async for line in lines:
                            parsed = parse_sse_line(line)
                            if parsed is None:
                                continue

                            event_type, data = parsed

                            # Extract response_id from any event that carries it.
                            event_response_id = _response_id_from_event(data)
                            if event_response_id is not None:
                                response_id = event_response_id
                                self._last_turn_response_id = event_response_id

                            # A server-side failure is authoritative even after
                            # local cancellation.  In particular, tool-drain
                            # mode must not translate-and-ignore the terminal
                            # event and then report the interrupted turn as
                            # successful.
                            if event_type == "response.failed":
                                error_msg = _response_failure_message(data)
                                recorder.record_framework_error(
                                    ErrorInfo(
                                        type="ResponsesAPIError",
                                        message=error_msg,
                                    )
                                )
                                # Raise so Session treats this as an agent error.
                                # The recorder context handles unit-exit recording.
                                raise RuntimeError(f"Responses API failed: {error_msg}")

                            # A completed response cannot still owe a tool
                            # result.  Accepting this terminal would commit an
                            # incomplete chain, and cancellation previously
                            # made that malformed boundary especially easy to
                            # hide.
                            if event_type == "response.completed":
                                if event_response_id is None:
                                    _raise_protocol_error(
                                        recorder,
                                        "Responses API response.completed arrived without "
                                        "a nonblank string response id",
                                    )
                                if pending_tool_calls:
                                    _raise_protocol_error(
                                        recorder,
                                        "Responses API response.completed arrived with "
                                        "pending tool calls",
                                    )

                            # Handle cancellation.
                            if cancel_token and cancel_token.is_cancelled:
                                interrupted = True
                                if pending_tool_calls:
                                    # Drain: keep processing until tools complete.
                                    bridge_ev = translate_sse_event(
                                        event_type,
                                        data,
                                        recorder,
                                        pending_tool_names,
                                    )
                                    if bridge_ev is not None:
                                        _update_tool_lifecycle(
                                            bridge_ev,
                                            recorder,
                                            pending_tool_calls,
                                            seen_tool_call_ids,
                                        )
                                    if event_type == "response.output_item.done":
                                        item = data.get("item")
                                        if isinstance(item, Mapping):
                                            accumulated_items.append(dict(item))
                                    if bridge_ev is not None:
                                        yield bridge_ev
                                        if not pending_tool_calls:
                                            break
                                    continue
                                # Immediate stop.
                                break

                            # Normal event processing.
                            if event_type == "response.completed":
                                completed = True
                                break

                            bridge_ev = translate_sse_event(
                                event_type,
                                data,
                                recorder,
                                pending_tool_names,
                            )
                            if bridge_ev is not None:
                                _update_tool_lifecycle(
                                    bridge_ev,
                                    recorder,
                                    pending_tool_calls,
                                    seen_tool_call_ids,
                                )
                                if bridge_ev.kind == "text_delta":
                                    accumulated += bridge_ev.text
                            if event_type == "response.output_item.done":
                                item = data.get("item")
                                if isinstance(item, Mapping):
                                    accumulated_items.append(dict(item))
                            if bridge_ev is not None:
                                yield bridge_ev
                        if completed:
                            await _drain_completed_stream(lines)
                except _ResponseStreamCancelled:
                    interrupted = True

            if pending_tool_calls:
                pending = ", ".join(sorted(repr(call_id) for call_id in pending_tool_calls))
                _raise_protocol_error(
                    recorder,
                    f"Responses API stream ended with pending tool calls: {pending}",
                )
            if not interrupted and not completed:
                error_msg = "Responses API stream ended before response.completed"
                recorder.record_framework_error(
                    ErrorInfo(
                        type="ResponsesAPIError",
                        message=error_msg,
                    )
                )
                raise RuntimeError(error_msg)

            # On successful (non-interrupted) completion, update chain state.
            if completed and response_id:
                self._last_completed_response_id = response_id
                self._completed_response_ids.append(response_id)
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
            self._pending_assistant_history_items = []
            self._pending_turn_metadata = None

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
        applied = run_interruption_journal_protocol(
            plan,
            mode,
            recorder,
            caused_by_signal_id,
            serialize_state=self._serialize_framework_state,
            apply_mutation=self._apply_planned_mutation,
        )
        if not applied:
            return

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
                    "completed_response_ids": self._completed_response_ids,
                    "last_turn_response_id": self._last_turn_response_id,
                    "last_turn_replay_items": self._last_turn_replay_items,
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
                "response_id": getattr(self, "_last_turn_response_id", None),
                "replay_prefix_items": getattr(self, "_last_turn_replay_items", []),
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

        If the interrupted response had already completed generation, roll the
        chain pointer back to its predecessor. The next invoke then chains from
        N-1 while replaying the truncated N turn.
        """
        instructions = plan.framework_instructions
        truncated = instructions.get("truncated_text", "")
        accumulated_items = instructions.get("accumulated_items", [])
        user_text = instructions.get("user_text")
        response_id = instructions.get("response_id")
        replay_prefix_items = instructions.get("replay_prefix_items", [])

        if (
            isinstance(response_id, str)
            and self._completed_response_ids
            and self._completed_response_ids[-1] == response_id
        ):
            self._completed_response_ids.pop()
            self._last_completed_response_id = (
                self._completed_response_ids[-1] if self._completed_response_ids else None
            )
            self._interrupted_response_id = response_id

        replay: list[dict[str, Any]] = []

        # If this turn consumed replay from an earlier interruption, preserve
        # that prefix whenever the current response is non-durable or rolled
        # back after playback interruption.
        for item in replay_prefix_items:
            if isinstance(item, Mapping):
                replay.append(dict(item))

        # Add the user message from the interrupted turn so the model
        # sees the question it was answering when interrupted.
        if user_text:
            replay.append({"role": "user", "content": user_text})

        # Add completed tool calls from interrupted turn.
        for item in accumulated_items:
            if not isinstance(item, Mapping):
                continue
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
        self._pending_assistant_history_items = []

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
        self._completed_response_ids = []
        self._response_count = 0
        self._replay_items = None
        self._pending_interruption_note = None
        self._pending_assistant_history_items = []
        self._last_accumulated_items = []
        self._last_user_text = None
        self._last_turn_response_id = None
        self._last_turn_replay_items = []
        self._interrupted_response_id = None
        self._pending_turn_metadata = None

    def configure_runtime(
        self,
        *,
        mcp_servers: list[str] | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> None:
        """Apply session-level model / api_key overrides.

        Only non-empty values overwrite the construction-time settings so
        an unset session field never blanks a value the bridge was built
        with.  This bridge does not consume ``mcp_servers``.
        """
        if model:
            self._model = model
        if api_key:
            self._api_key = api_key

    # ── History post-processing ───────────────────────────────────

    def replace_last_assistant_text(self, text: str) -> None:
        """Queue an assistant-role correction for post-processed text.

        The Responses API chains by ``previous_response_id`` — local state
        cannot rewrite an already-committed response.  Surface the text the
        user actually heard as assistant history on the next turn instead of
        promoting model/user-influenced text into a developer instruction.
        No-op when there is no prior completed response.
        """
        if self._last_completed_response_id is None:
            return
        self._pending_assistant_history_items.append(
            {
                "role": "assistant",
                "content": (
                    "[The assistant's last response was post-processed before "
                    f'delivery. The user heard: "{text}"]'
                ),
            }
        )

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

        # Seed prior messages only before a remote chain exists. Once chained
        # (or replaying an interrupted turn), retain transient instructions but
        # drop caller user/assistant history that the remote state already owns.
        input_items.extend(
            normalize_context_messages(
                turn_input.context,
                own_history=bool(self._last_completed_response_id or self._replay_items),
            )
        )

        # If we have replay items from an interrupted turn, prepend them.
        # Note: we do NOT clear _replay_items / _pending_interruption_note
        # here — they are cleared in invoke() only after the request
        # succeeds, so a transient HTTP failure doesn't lose them.
        if self._replay_items:
            input_items.extend(self._replay_items)

        # Surface post-processed assistant text as assistant history.  The
        # content is derived from prior model output and can be influenced by
        # the remote user, so it must not be sent with developer priority.
        if self._pending_assistant_history_items:
            input_items.extend(self._pending_assistant_history_items)

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
        if self._reasoning_effort is not None:
            body["reasoning"] = {"effort": self._reasoning_effort}

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
