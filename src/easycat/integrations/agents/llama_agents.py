"""LlamaAgents / LlamaIndex Workflows bridge.

Wraps the modern ``llama-index-workflows`` in-process ``Workflow`` API and
the optional ``llama-agents-client`` remote workflow server API.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal
from uuid import uuid4

from easycat.cancel import CancelToken
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


_TEXT_FIELDS = (
    "delta",
    "text_delta",
    "chunk",
    "msg",
    "prefix",
    "prompt",
    "question",
    "text",
    "content",
    "message",
    "response",
    "output",
    "result",
)


class LlamaAgentsBridge:
    """Bridge for LlamaAgents / LlamaIndex Workflows.

    Local mode wraps an in-process ``workflows.Workflow`` instance and calls
    ``workflow.run(...)``. Remote mode wraps a ``llama_agents.client.WorkflowClient``
    and streams events from a named workflow server workflow.

    ``turn_input.text`` is mapped into the workflow start event under
    ``input_key`` (default: ``"message"``). Context and turn metadata are
    also exposed to workflows as optional start-event fields.
    """

    COMMITTABLE_BOUNDARIES = {
        UnitKind.WORKFLOW_NODE: CommitRule.BETWEEN_NODES,
    }

    def __init__(
        self,
        workflow: Any | None = None,
        *,
        client: Any | None = None,
        base_url: str | None = None,
        workflow_name: str | None = None,
        input_key: str = "message",
        context_key: str | None = "context",
        turn_id_key: str | None = "turn_id",
        interruption_note_key: str | None = "easycat_interruption_note",
        preserve_context: bool = True,
        run_kwargs: dict[str, Any] | None = None,
        start_event_factory: Callable[[AgentTurnInput, dict[str, Any]], Any] | None = None,
        event_text_extractor: Callable[[Any], str | None] | None = None,
        human_response_event_factory: Callable[[AgentTurnInput], Any] | None = None,
        human_response_key: str = "response",
        human_response_step: str | None = None,
        display_name: str | None = None,
        include_internal_events: bool = False,
    ) -> None:
        remote_requested = client is not None or base_url is not None
        if workflow is not None and remote_requested:
            raise BridgeInputError("Pass either workflow= or client=/base_url=, not both")
        if workflow is None and not remote_requested:
            raise BridgeInputError("LlamaAgentsBridge requires workflow= or client=/base_url=")
        if client is not None and base_url is not None:
            raise BridgeInputError("Pass either client= or base_url=, not both")
        if remote_requested and not workflow_name:
            raise BridgeInputError("Remote LlamaAgentsBridge requires workflow_name=")

        if base_url is not None:
            try:
                from llama_agents.client import WorkflowClient
            except ImportError as exc:
                raise ImportError(
                    "llama-agents-client is required for remote LlamaAgentsBridge: "
                    "pip install 'easycat[llama-agents]'"
                ) from exc
            client = WorkflowClient(base_url=base_url)

        self._mode = "remote" if remote_requested else "local"
        self._workflow = workflow
        self._client = client
        self._workflow_name = workflow_name
        self._display_name = display_name or workflow_name or _workflow_name(workflow)
        self._input_key = input_key
        self._context_key = context_key
        self._turn_id_key = turn_id_key
        self._interruption_note_key = interruption_note_key
        self._preserve_context = preserve_context
        self._run_kwargs = dict(run_kwargs or {})
        self._start_event_factory = start_event_factory
        self._event_text_extractor = event_text_extractor
        self._human_response_event_factory = human_response_event_factory
        self._human_response_key = human_response_key
        self._human_response_step = human_response_step
        self._include_internal_events = include_internal_events

        self._ctx: Any = None
        self._remote_context: Any = None
        self._remote_handler_id: str | None = None
        self._remote_event_sequence: int | Literal["now"] = -1
        self._active_handler: Any = None
        self._active_handler_id: str | None = None
        self._pending_local_handler: Any = None
        self._pending_remote_handler_id: str | None = None
        self._pending_interruption_note: str | None = None
        self._last_output: Any = None
        self._last_output_text = ""
        self._run_count = 0

    # ── ExternalAgentBridge interface ─────────────────────────────

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        cursor = ExecutionCursor(
            unit_id=f"llama-workflow-{uuid4().hex[:8]}",
            unit_kind=UnitKind.WORKFLOW_NODE,
            display_name=self._display_name,
            entered_at=time.monotonic_ns(),
            committable=False,
        )
        recorder.record_unit_entered(cursor)

        accumulated = ""
        try:
            if self._mode == "remote":
                stream = self._invoke_remote(turn_input, cancel_token)
            else:
                stream = self._invoke_local(turn_input, cancel_token)
            async for event in stream:
                if event.kind == "text_delta":
                    accumulated += event.text
                yield event
        except Exception as exc:
            recorder.record_framework_error(ErrorInfo.from_exception(exc))
            recorder.record_unit_exited(cursor, reason="error")
            raise

        self._last_output_text = accumulated
        self._run_count += 1
        recorder.record_unit_exited(cursor.with_committable(True), reason=None)
        yield AgentBridgeEvent(
            kind="done",
            text=accumulated,
            structured_output=self._last_output,
        )

    def snapshot_state(self) -> FrameworkStateSnapshot:
        fields: dict[str, Any] = {
            "mode": self._mode,
            "workflow": self._display_name,
            "input_key": self._input_key,
            "preserve_context": self._preserve_context,
            "run_count": self._run_count,
            "active_handler_id": self._active_handler_id,
            "remote_handler_id": self._remote_handler_id,
            "remote_event_sequence": self._remote_event_sequence,
            "has_context": self._ctx is not None or self._remote_context is not None,
            "waiting_for_input": (
                self._pending_local_handler is not None
                or self._pending_remote_handler_id is not None
            ),
            "last_output_text": self._last_output_text,
        }
        return FrameworkStateSnapshot(fields=fields, kind="llama_agents")

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

    def replace_last_assistant_text(self, text: str) -> None:
        self._last_output_text = text
        for target in (self._workflow, self._client):
            fn = getattr(target, "replace_last_assistant_text", None)
            if callable(fn):
                try:
                    fn(text)
                except Exception:
                    logger.debug("LlamaAgents replace_last_assistant_text failed", exc_info=True)

    def append_interruption_note(self, note: str) -> None:
        self._pending_interruption_note = note
        for target in (self._workflow, self._client):
            fn = getattr(target, "append_interruption_note", None)
            if callable(fn):
                try:
                    fn(note)
                except Exception:
                    logger.debug("LlamaAgents append_interruption_note failed", exc_info=True)

    def reset(self) -> None:
        for target in (self._workflow, self._client):
            fn = getattr(target, "reset", None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    logger.debug("LlamaAgents reset delegate failed", exc_info=True)
        self._ctx = None
        self._remote_context = None
        self._remote_handler_id = None
        self._remote_event_sequence = -1
        self._active_handler = None
        self._active_handler_id = None
        self._pending_local_handler = None
        self._pending_remote_handler_id = None
        self._pending_interruption_note = None
        self._last_output = None
        self._last_output_text = ""
        self._run_count = 0

    # ── Local workflow mode ───────────────────────────────────────

    async def _invoke_local(
        self,
        turn_input: AgentTurnInput,
        cancel_token: CancelToken | None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        assert self._workflow is not None

        handler = self._pending_local_handler
        if handler is not None:
            self._pending_local_handler = None
            self._send_local_human_response(handler, turn_input)
        else:
            kwargs = dict(self._run_kwargs)
            start_payload = self._build_start_payload(turn_input)
            if self._start_event_factory is not None:
                kwargs["start_event"] = self._start_event_factory(turn_input, start_payload)
            else:
                kwargs.update(start_payload)
            if self._preserve_context and self._ctx is not None:
                kwargs["ctx"] = self._ctx

            handler = self._workflow.run(**kwargs)
        self._active_handler = handler
        self._active_handler_id = _string_or_none(getattr(handler, "run_id", None))

        streamed_text = False
        cancelled = False
        try:
            events = self._stream_local_events(handler)
            if events is not None:
                async for workflow_event in _aiter_with_cancellation(events, cancel_token):
                    if _is_input_required_event(workflow_event):
                        self._pending_local_handler = handler
                        self._last_output = workflow_event
                        text = self._extract_event_text(workflow_event)
                        if text:
                            yield AgentBridgeEvent(kind="text_delta", text=text)
                        return
                    if _is_stop_event(workflow_event):
                        continue
                    delta = self._extract_event_text(workflow_event)
                    if delta:
                        streamed_text = True
                        yield AgentBridgeEvent(kind="text_delta", text=delta)

            if cancel_token is not None and cancel_token.is_cancelled:
                cancelled = True
                await self._cancel_local_handler(handler)

            if cancelled:
                self._last_output = await self._await_handler_best_effort(handler)
                return

            result = await handler if inspect.isawaitable(handler) else handler
            self._last_output = result
            if not streamed_text:
                text = _extract_output_text(result)
                if text:
                    yield AgentBridgeEvent(kind="text_delta", text=text)
        finally:
            self._ctx = getattr(handler, "ctx", self._ctx)
            self._active_handler = None
            self._active_handler_id = None

    def _stream_local_events(self, handler: Any) -> AsyncIterator[Any] | None:
        stream_events = getattr(handler, "stream_events", None)
        if not callable(stream_events):
            return None
        try:
            params = inspect.signature(stream_events).parameters
        except (TypeError, ValueError):
            params = {}
        if "expose_internal" in params:
            return stream_events(expose_internal=self._include_internal_events)
        return stream_events()

    def _send_local_human_response(self, handler: Any, turn_input: AgentTurnInput) -> None:
        ctx = getattr(handler, "ctx", None)
        send_event = getattr(ctx, "send_event", None)
        if not callable(send_event):
            raise BridgeInputError("Paused Llama workflow handler has no ctx.send_event()")
        event = self._build_human_response_event(turn_input)
        send_event(event, step=self._human_response_step)

    async def _cancel_local_handler(self, handler: Any) -> None:
        cancel_run = getattr(handler, "cancel_run", None)
        if callable(cancel_run):
            result = cancel_run()
            if inspect.isawaitable(result):
                await result
            return
        cancel = getattr(handler, "cancel", None)
        if callable(cancel):
            cancel()

    async def _await_handler_best_effort(self, handler: Any) -> Any:
        if not inspect.isawaitable(handler):
            return None
        try:
            return await handler
        except Exception:
            return None

    # ── Remote WorkflowClient mode ────────────────────────────────

    async def _invoke_remote(
        self,
        turn_input: AgentTurnInput,
        cancel_token: CancelToken | None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        assert self._client is not None
        assert self._workflow_name is not None

        handler_id = self._pending_remote_handler_id
        # We reuse an existing remote handler when resuming a HITL pause, or
        # when preserve_context keeps the prior handler alive. In both cases
        # the server's event log keeps accumulating, so streaming from -1
        # would replay earlier turns -- carry the cursor forward instead.
        reusing_handler = handler_id is not None or (
            self._preserve_context and self._remote_handler_id is not None
        )
        after_sequence = self._remote_event_sequence if reusing_handler else -1
        if handler_id is not None:
            self._pending_remote_handler_id = None
            await self._send_remote_human_response(handler_id, turn_input)
        else:
            start_payload = self._build_start_payload(turn_input)
            start_event: Any
            if self._start_event_factory is not None:
                start_event = self._start_event_factory(turn_input, start_payload)
            else:
                start_event = _start_event_from_payload(start_payload)

            handler_data = await self._client.run_workflow_nowait(
                self._workflow_name,
                handler_id=self._remote_handler_id if self._preserve_context else None,
                start_event=start_event,
                context=self._remote_context if self._preserve_context else None,
            )
            handler_id = _string_or_none(getattr(handler_data, "handler_id", None))
            if handler_id is None:
                raise BridgeInputError(
                    "WorkflowClient.run_workflow_nowait() did not return handler_id"
                )

        self._remote_handler_id = handler_id
        self._active_handler_id = handler_id

        streamed_text = False
        cancelled = False
        event_stream = None
        try:
            event_stream = self._client.get_workflow_events(
                handler_id,
                include_internal_events=self._include_internal_events,
                after_sequence=after_sequence,
            )
            async for envelope in _aiter_with_cancellation(event_stream, cancel_token):
                self._remote_event_sequence = getattr(
                    event_stream, "last_sequence", self._remote_event_sequence
                )
                workflow_event = _load_remote_event(envelope)
                if _is_input_required_event(workflow_event) or _is_input_required_event(envelope):
                    self._pending_remote_handler_id = handler_id
                    self._last_output = workflow_event
                    delta = self._extract_event_text(workflow_event)
                    if delta is None and workflow_event is not envelope:
                        delta = self._extract_event_text(envelope)
                    if delta:
                        yield AgentBridgeEvent(kind="text_delta", text=delta)
                    return
                if _is_stop_event(workflow_event) or _is_stop_event(envelope):
                    continue
                delta = self._extract_event_text(workflow_event)
                if delta is None and workflow_event is not envelope:
                    delta = self._extract_event_text(envelope)
                if delta:
                    streamed_text = True
                    yield AgentBridgeEvent(kind="text_delta", text=delta)

            if cancel_token is not None and cancel_token.is_cancelled:
                cancelled = True
                await self._cancel_remote_handler(handler_id)
        finally:
            # The real WorkflowClient spins up a background SSE reader for
            # get_workflow_events. Close it on every exit path -- cancellation,
            # a HITL pause (early return above), or normal completion -- so a
            # paused conversation does not leak a stream/task while the next
            # user turn opens another one.
            if event_stream is not None:
                aclose = getattr(event_stream, "aclose", None)
                if callable(aclose):
                    with contextlib.suppress(Exception):
                        await aclose()

        result_data = await self._client.get_handler(handler_id)
        self._remote_context = getattr(result_data, "context", self._remote_context)
        self._last_output = getattr(result_data, "result", None)
        if not self._preserve_context:
            # With preserve_context the handler (and its event log) survives
            # into the next turn, so keep the cursor to avoid replaying this
            # turn. Otherwise each turn gets a fresh handler -- reset to -1.
            self._remote_event_sequence = -1
        if not cancelled:
            _raise_if_remote_failed(result_data, self._workflow_name)
        if not streamed_text and not cancelled:
            text = _extract_output_text(self._last_output)
            if text:
                yield AgentBridgeEvent(kind="text_delta", text=text)
        self._active_handler_id = None

    async def _cancel_remote_handler(self, handler_id: str) -> None:
        cancel_handler = getattr(self._client, "cancel_handler", None)
        if callable(cancel_handler):
            result = cancel_handler(handler_id)
            if inspect.isawaitable(result):
                await result

    async def _send_remote_human_response(
        self,
        handler_id: str,
        turn_input: AgentTurnInput,
    ) -> None:
        send_event = getattr(self._client, "send_event", None)
        if not callable(send_event):
            raise BridgeInputError("WorkflowClient does not support send_event()")
        result = send_event(
            handler_id,
            self._build_human_response_event(turn_input),
            step=self._human_response_step,
        )
        if inspect.isawaitable(result):
            await result

    # ── Interruption helpers ──────────────────────────────────────

    def _serialize_framework_state(self) -> bytes:
        state = {
            "mode": self._mode,
            "workflow": self._display_name,
            "run_count": self._run_count,
            "active_handler_id": self._active_handler_id,
            "remote_handler_id": self._remote_handler_id,
            "remote_event_sequence": self._remote_event_sequence,
            "pending_interruption_note": self._pending_interruption_note,
            "waiting_for_input": (
                self._pending_local_handler is not None
                or self._pending_remote_handler_id is not None
            ),
            "last_output_text": self._last_output_text,
            "context": _jsonable_context(self._ctx),
            "remote_context": _jsonable_context(self._remote_context),
        }
        try:
            return json.dumps(state, default=str).encode("utf-8")
        except (TypeError, ValueError):
            return b"{}"

    def _plan_interruption(self, delivered_text: str, mode: CancellationMode) -> InterruptionPlan:
        pre_ref = f"llama-pre-{id(self):x}"
        post_ref = f"llama-post-{id(self):x}"
        mutation_kind = "interrupt_llama_workflow"
        return InterruptionPlan(
            mutation_kind=mutation_kind,
            pre_state_ref=pre_ref,
            post_state_ref=post_ref,
            framework_instructions={
                "delivered_text": delivered_text,
                "replacement": delivered_text + "..." if delivered_text else "",
                "mode": mode.value,
            },
        )

    def _apply_planned_mutation(self, plan: InterruptionPlan) -> None:
        replacement = plan.framework_instructions["replacement"]
        delivered_text = plan.framework_instructions["delivered_text"]
        mode = CancellationMode(plan.framework_instructions["mode"])
        self._last_output_text = replacement

        target = self._workflow if self._mode == "local" else self._client
        fn = getattr(target, "apply_interruption", None)
        if callable(fn):
            fn(delivered_text, mode)

    # ── Helpers ───────────────────────────────────────────────────

    def _build_start_payload(self, turn_input: AgentTurnInput) -> dict[str, Any]:
        payload: dict[str, Any] = {self._input_key: turn_input.text}
        if self._context_key is not None:
            payload[self._context_key] = turn_input.context
        if self._turn_id_key is not None and turn_input.turn_id is not None:
            payload[self._turn_id_key] = turn_input.turn_id
        if self._interruption_note_key is not None and self._pending_interruption_note:
            payload[self._interruption_note_key] = self._pending_interruption_note
            self._pending_interruption_note = None
        return payload

    def _extract_event_text(self, event: Any) -> str | None:
        if self._event_text_extractor is not None:
            value = self._event_text_extractor(event)
            return None if value is None else str(value)
        return _extract_text_field(event)

    def _build_human_response_event(self, turn_input: AgentTurnInput) -> Any:
        if self._human_response_event_factory is not None:
            return self._human_response_event_factory(turn_input)
        return _human_response_event({self._human_response_key: turn_input.text})


def _workflow_name(workflow: Any) -> str:
    if workflow is None:
        return "LlamaAgentsWorkflow"
    name = getattr(workflow, "workflow_name", None)
    if isinstance(name, str) and name:
        return name
    return type(workflow).__name__


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


async def _aiter_with_cancellation(
    source: AsyncIterator[Any],
    cancel_token: CancelToken | None,
) -> AsyncIterator[Any]:
    """Yield from ``source``, stopping promptly when ``cancel_token`` fires.

    A plain ``async for`` only observes cancellation *between* items, so a
    barge-in while the workflow is busy on a long step (and the event stream
    is idle waiting for the next item) would not be seen until the next
    event is emitted -- often the final ``StopEvent``, by which point the
    work has already finished.  Racing each ``__anext__()`` against
    ``cancel_token.wait()`` lets the caller react while the stream is still
    blocked.  Cancellation takes priority: a just-arrived item is dropped if
    the token is already set, mirroring a top-of-loop cancel check.
    """
    if cancel_token is None:
        async for item in source:
            yield item
        return

    iterator = source.__aiter__()
    cancel_wait: asyncio.Task[None] = asyncio.ensure_future(cancel_token.wait())
    stopped_early = False
    try:
        while not cancel_token.is_cancelled:
            next_item: asyncio.Task[Any] = asyncio.ensure_future(iterator.__anext__())
            await asyncio.wait(
                (next_item, cancel_wait),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_token.is_cancelled:
                stopped_early = True
                if not next_item.done():
                    next_item.cancel()
                with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration, Exception):
                    await next_item
                return
            try:
                yield next_item.result()
            except StopAsyncIteration:
                return
        stopped_early = True
    finally:
        cancel_wait.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cancel_wait
        if stopped_early:
            aclose = getattr(iterator, "aclose", None)
            if callable(aclose):
                with contextlib.suppress(Exception):
                    await aclose()


_REMOTE_FAILURE_STATUSES = frozenset(
    {"failed", "failure", "error", "errored", "cancelled", "canceled"}
)


def _raise_if_remote_failed(handler_data: Any, workflow_name: str | None) -> None:
    status = getattr(handler_data, "status", None)
    status_str = status.value if hasattr(status, "value") else status
    if not isinstance(status_str, str):
        return
    if status_str.lower() not in _REMOTE_FAILURE_STATUSES:
        return
    error = getattr(handler_data, "error", None)
    detail = str(error) if error else status_str
    name = workflow_name or "remote workflow"
    raise RuntimeError(f"Remote LlamaAgents workflow {name!r} {status_str}: {detail}")


def _start_event_from_payload(payload: dict[str, Any]) -> Any:
    try:
        from workflows.events import StartEvent

        return StartEvent(**payload)
    except ImportError:
        pass
    try:
        from llama_index.core.workflow import StartEvent

        return StartEvent(**payload)
    except ImportError:
        return payload


def _load_remote_event(envelope: Any) -> Any:
    load_event = getattr(envelope, "load_event", None)
    if callable(load_event):
        try:
            return load_event()
        except Exception:
            logger.debug("Failed to load LlamaAgents remote event", exc_info=True)
    return envelope


def _is_stop_event(event: Any) -> bool:
    for path in (
        ("workflows.events", "StopEvent"),
        ("llama_index.core.workflow", "StopEvent"),
    ):
        try:
            module = __import__(path[0], fromlist=[path[1]])
            stop_event = getattr(module, path[1])
            if isinstance(event, stop_event):
                return True
        except ImportError:
            pass

    cls_name = type(event).__name__
    if cls_name in {
        "StopEvent",
        "WorkflowTimedOutEvent",
        "WorkflowCancelledEvent",
        "WorkflowFailedEvent",
        "IdleReleasedEvent",
    }:
        return True
    event_type = getattr(event, "type", None)
    if isinstance(event_type, str):
        leaf = event_type.rsplit(".", 1)[-1]
        return leaf in {
            "StopEvent",
            "WorkflowTimedOutEvent",
            "WorkflowCancelledEvent",
            "WorkflowFailedEvent",
            "IdleReleasedEvent",
        }
    return False


def _is_input_required_event(event: Any) -> bool:
    for path in (
        ("workflows.events", "InputRequiredEvent"),
        ("llama_index.core.workflow", "InputRequiredEvent"),
    ):
        try:
            module = __import__(path[0], fromlist=[path[1]])
            input_required_event = getattr(module, path[1])
            if isinstance(event, input_required_event):
                return True
        except ImportError:
            pass

    cls_name = type(event).__name__
    if cls_name == "InputRequiredEvent":
        return True
    event_type = getattr(event, "type", None)
    if isinstance(event_type, str):
        return event_type.rsplit(".", 1)[-1] == "InputRequiredEvent"
    types = getattr(event, "types", None)
    if isinstance(types, list):
        return "InputRequiredEvent" in {str(t).rsplit(".", 1)[-1] for t in types}
    return False


def _human_response_event(payload: dict[str, Any]) -> Any:
    try:
        from workflows.events import HumanResponseEvent

        return HumanResponseEvent(**payload)
    except ImportError:
        pass
    try:
        from llama_index.core.workflow import HumanResponseEvent

        return HumanResponseEvent(**payload)
    except ImportError:
        return payload


def _extract_text_field(value: Any) -> str | None:
    data = _event_mapping(value)
    for key in _TEXT_FIELDS:
        if key in data:
            text = data[key]
            if isinstance(text, str):
                return text
            if isinstance(text, dict):
                nested_text = _extract_text_field(text)
                if nested_text is not None:
                    return nested_text
            elif text is not None and not isinstance(text, (list, tuple, set)):
                return str(text)

    nested = data.get("value")
    if isinstance(nested, dict):
        return _extract_text_field(nested)
    return None


def _event_mapping(event: Any) -> dict[str, Any]:
    if isinstance(event, dict):
        data = dict(event)
        extra = data.get("_data")
        if isinstance(extra, dict):
            data.update(extra)
        return data
    model_dump = getattr(event, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
            if isinstance(dumped, dict):
                data = dict(dumped)
                extra = data.get("_data")
                if isinstance(extra, dict):
                    data.update(extra)
                return data
        except Exception:
            pass

    data: dict[str, Any] = {}
    for key in (*_TEXT_FIELDS, "value", "result"):
        try:
            if hasattr(event, key):
                data[key] = getattr(event, key)
        except Exception:
            continue
    return data


def _extract_output_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    text = _extract_text_field(result)
    if text is not None:
        return text
    if isinstance(result, dict):
        for key in ("result", "value"):
            if key in result:
                return _extract_output_text(result[key])
    return str(result) if result is not None else ""


def _jsonable_context(ctx: Any) -> Any:
    if ctx is None:
        return None
    to_dict = getattr(ctx, "to_dict", None)
    if callable(to_dict):
        try:
            return to_dict()
        except Exception:
            return type(ctx).__name__
    return ctx


def is_llama_workflow_instance(agent: Any) -> bool:
    """Return True when ``agent`` is an in-process Llama Workflow instance."""
    if isinstance(agent, type):
        return False
    for module_name in (
        "workflows",
        "llama_agents.workflows",
        "llama_index.core.workflow",
    ):
        try:
            module = __import__(module_name, fromlist=["Workflow"])
            workflow_cls = getattr(module, "Workflow")
        except (ImportError, AttributeError):
            continue
        if isinstance(agent, workflow_cls):
            return True
    return False


__all__ = ["LlamaAgentsBridge", "is_llama_workflow_instance"]
