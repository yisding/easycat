"""AgentRunner: wraps simple agents in the ExternalAgentBridge protocol.

``AgentRunner`` adapts a user-supplied callable/object that implements
the minimal :class:`Agent` protocol (``async run(text) -> str``) — or
another ``ExternalAgentBridge`` — so Session can drive it through the
unified ``invoke()`` / ``apply_interruption()`` / ``reset()`` surface.

When wrapping a non-bridge agent, ``AgentRunner`` owns its own chat
history list and yields a single ``text_delta`` + ``done`` pair from
``invoke()``.  When wrapping a bridge it delegates almost everything.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, TypeVar
from uuid import uuid4

from easycat._numeric import is_finite_number
from easycat.cancel import CancelToken
from easycat.integrations.agents._helpers import INTERRUPTION_NOTE, aclose_quietly
from easycat.integrations.agents.base import (
    NULL_RECORDER,
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
    CancellationMode,
    CommitRule,
    ExecutionCursor,
    ExternalAgentBridge,
    FrameworkStateSnapshot,
    NullAgentRecorder,
    UnitKind,
)
from easycat.runtime.records import ErrorInfo
from easycat.teardown_budgets import (
    AGENT_POST_DONE_STREAM_DRAIN_TIMEOUT_S as _POST_DONE_STREAM_DRAIN_TIMEOUT_S,
)
from easycat.timeouts import AgentTimeoutError

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def _require_agent_text(value: Any, *, source: str) -> str:
    """Enforce the plain-text portion of the agent/bridge protocol."""
    if not isinstance(value, str):
        raise TypeError(f"{source} must be str, got {type(value).__name__}")
    return value


def _bridge_event_text(event: Any, kind: Any) -> str:
    """Validate text only for text-bearing kinds; tool/handoff events from
    duck-typed bridges may legitimately carry ``text=None``."""
    if kind not in ("text_delta", "done"):
        return ""
    return _require_agent_text(
        getattr(event, "text", None),
        source=f"agent bridge {kind} event text",
    )


async def _await_with_timeout(awaitable: Awaitable[_T], timeout: float | None) -> _T:
    """Await in the caller task while enforcing an optional deadline.

    ``asyncio.wait_for`` schedules a coroutine as a child task even when it
    completes immediately. Agent streaming invokes this guard for every
    provider event, so that task hand-off becomes framework-owned latency on
    the path to first text. ``asyncio.timeout`` enforces the same cancellation
    deadline on the current task without adding a scheduler round trip.
    """
    if timeout is None:
        return await awaitable
    if timeout <= 0:
        # Preserve ``wait_for``'s established zero/negative-timeout behavior:
        # a newly-created coroutine is cancelled before it gets a loop turn.
        # ``asyncio.timeout(0)`` instead lets a coroutine that never suspends
        # complete successfully, which changes the public timeout contract.
        return await asyncio.wait_for(awaitable, timeout=timeout)
    async with asyncio.timeout(timeout):
        return await awaitable


async def close_stream_after_done(stream: AsyncIterator[Any]) -> None:
    """Let a terminal bridge stream finish promptly, then close if it keeps running.

    Some async-generator bridges perform cleanup immediately after yielding their
    terminal ``done`` event.  Closing them before one more iteration skips that
    cleanup, but blindly awaiting the next item can hang on misbehaving streams
    that continue waiting after ``done``.  Give the iterator one bounded chance
    to finish, then close it defensively.
    """

    try:
        await asyncio.wait_for(
            stream.__anext__(),
            timeout=_POST_DONE_STREAM_DRAIN_TIMEOUT_S,
        )
    except StopAsyncIteration:
        return
    except TimeoutError:
        pass
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001, S110 intentional boundary or best-effort cleanup
        pass

    aclose = getattr(stream, "aclose", None)
    if aclose is not None:
        with contextlib.suppress(Exception):
            await aclose()


@dataclass
class _BridgeToolDrain:
    """Route only lifecycle events for tool calls observed before cancellation."""

    pending_call_counts: Counter[str | None] = field(default_factory=Counter)

    def _finish_call(self, call_id: str | None) -> None:
        remaining = self.pending_call_counts[call_id] - 1
        if remaining > 0:
            self.pending_call_counts[call_id] = remaining
        else:
            self.pending_call_counts.pop(call_id, None)

    def route(
        self,
        event: AgentBridgeEvent,
        *,
        cancelled: bool,
    ) -> Literal["emit", "emit_stop", "drop", "stop"]:
        kind = getattr(event, "kind", None)
        call_id = getattr(event, "call_id", None)
        if not cancelled:
            if kind == "tool_started":
                self.pending_call_counts[call_id] += 1
            elif kind == "tool_result":
                self._finish_call(call_id)
            return "emit"
        if not self.pending_call_counts or kind == "done":
            return "stop"
        if kind == "tool_delta" and self.pending_call_counts[call_id] > 0:
            return "emit"
        if kind == "tool_result" and self.pending_call_counts[call_id] > 0:
            self._finish_call(call_id)
            return "emit_stop" if not self.pending_call_counts else "emit"
        return "drop"


async def _next_bridge_event(
    inner_iter: AsyncIterator[AgentBridgeEvent],
    *,
    deadline: float | None,
    timeout: float | None,
    cancel_token: CancelToken | None,
    tool_drain: _BridgeToolDrain,
) -> tuple[AgentBridgeEvent | None, bool]:
    """Read the next deliverable event, draining pre-cancel tool lifecycles."""
    while True:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AgentTimeoutError(timeout or 0)
            event = await _await_with_timeout(inner_iter.__anext__(), remaining)
        else:
            event = await inner_iter.__anext__()
        decision = tool_drain.route(
            event,
            cancelled=bool(cancel_token and cancel_token.is_cancelled),
        )
        if decision == "stop":
            return None, False
        if decision == "drop":
            continue
        return event, decision == "emit_stop"


# ── Configuration ───────────────────────────────────────────────────


@dataclass
class AgentRunnerConfig:
    """Configuration for AgentRunner."""

    timeout: float | None = 30.0
    # Start simple ``run(text) -> str`` agents as soon as STT produces a
    # final segment, overlapping model latency with endpoint confirmation.
    # Stateful ExternalAgentBridge implementations are excluded because the
    # bridge contract does not provide transactional rollback.
    preemptive_generation: bool = False
    # Bound restarts when a paused user resumes and produces a longer final
    # transcript before the endpoint is confirmed.
    preemptive_max_retries: int = 3

    def __post_init__(self) -> None:
        if self.timeout is not None and not is_finite_number(self.timeout):
            raise ValueError("AgentRunnerConfig.timeout must be a finite number or None")
        if self.timeout is not None:
            self.timeout = float(self.timeout)
        if not isinstance(self.preemptive_generation, bool):
            raise ValueError(  # noqa: TRY004 domain-specific validation error
                "AgentRunnerConfig.preemptive_generation must be a boolean"
            )
        if isinstance(self.preemptive_max_retries, bool) or not isinstance(
            self.preemptive_max_retries, int
        ):
            raise ValueError(  # noqa: TRY004 domain-specific validation error
                "AgentRunnerConfig.preemptive_max_retries must be an integer"
            )
        if self.preemptive_max_retries < 1:
            raise ValueError("AgentRunnerConfig.preemptive_max_retries must be >= 1")


@dataclass
class PreparedAgentResponse:
    """A simple-agent response held outside conversation history until commit."""

    input_text: str
    response: str
    started_at_ns: int = 0
    committed: bool = False


# ── AgentRunner ─────────────────────────────────────────────────────


class AgentRunner:
    """Adapts a user-supplied agent to the :class:`ExternalAgentBridge` protocol.

    Accepts either a simple object exposing ``async def run(text) -> str``
    or another ``ExternalAgentBridge``.  In the bridge case all calls are
    delegated.  Otherwise ``AgentRunner`` keeps its own chat history,
    applies timeouts and cancellation, and emits a single
    ``text_delta`` + ``done`` event pair from :meth:`invoke`.

    When wrapping a stateful bridge the runner's ``_history`` is only
    *advisory* — the inner bridge owns the authoritative conversation
    state.  The shadow history is mirrored from a turn only after that
    turn completes successfully, so it never claims a timed-out/errored
    turn that the inner bridge has already partially committed.
    """

    COMMITTABLE_BOUNDARIES: ClassVar[Mapping[UnitKind | str, CommitRule]] = {
        UnitKind.AGENT: CommitRule.BETWEEN_TURNS,
    }

    def __init__(
        self,
        agent: Any,
        config: AgentRunnerConfig | None = None,
    ) -> None:
        self._agent = agent
        self._config = config or AgentRunnerConfig()
        self._history: list[dict[str, str]] = []
        self._is_bridge = isinstance(agent, ExternalAgentBridge)

    # ── Properties ─────────────────────────────────────────────

    @property
    def history(self) -> list[Any]:
        """Current conversation history (copy)."""
        return [item.copy() for item in self._history]

    @property
    def is_bridge(self) -> bool:
        """Whether the wrapped agent is itself an ``ExternalAgentBridge``."""
        return self._is_bridge

    @property
    def is_passthrough_provider(self) -> bool:
        """Whether the wrapped agent explicitly marks itself as passthrough."""
        return bool(getattr(self._agent, "is_passthrough_provider", False))

    @property
    def supports_preemptive_generation(self) -> bool:
        """Whether this runner can prepare a response without mutating history."""
        return self._config.preemptive_generation and not self._is_bridge

    @property
    def preemptive_max_retries(self) -> int:
        """Maximum speculative attempts allowed for one voice turn."""
        return self._config.preemptive_max_retries

    @staticmethod
    def _validate_plain_turn_input(turn_input: AgentTurnInput) -> None:
        if turn_input.role != "user":
            raise ValueError(
                "system application prompts require an ExternalAgentBridge; "
                "plain async run(text) agents cannot represent them"
            )

    def version_info(self) -> dict[str, str]:
        """Expose wrapped bridge metadata to the session journal."""
        version_info = getattr(self._agent, "version_info", None)
        if callable(version_info):
            info = version_info()
            if isinstance(info, dict) and all(
                isinstance(key, str) and isinstance(value, str) for key, value in info.items()
            ):
                return info
        return {
            "provider": type(self._agent).__name__,
            "model": "unknown",
            "api_version": "unknown",
            "sdk_version": "unknown",
        }

    async def prepare_response(self, turn_input: AgentTurnInput) -> PreparedAgentResponse:
        """Run a simple agent without committing its response to chat history.

        The prepared response is safe to discard when speech resumes.  Wrapped
        framework bridges are deliberately rejected: they own durable state
        that EasyCat cannot roll back through the current bridge protocol.
        """
        if not self.supports_preemptive_generation:
            raise RuntimeError("agent does not support preemptive generation")
        self._validate_plain_turn_input(turn_input)

        started_at_ns = time.monotonic_ns()
        try:
            if self._config.timeout is not None:
                response = await asyncio.wait_for(
                    self._agent.run(turn_input.text),
                    timeout=self._config.timeout,
                )
            else:
                response = await self._agent.run(turn_input.text)
        except TimeoutError:
            raise AgentTimeoutError(self._config.timeout or 0) from None
        response = _require_agent_text(response, source="plain agent response")

        return PreparedAgentResponse(
            input_text=turn_input.text,
            response=response,
            started_at_ns=started_at_ns,
        )

    async def invoke_prepared(
        self,
        prepared: PreparedAgentResponse,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
        *,
        commit_guard: Callable[[], bool] | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        """Commit and emit a response previously produced by :meth:`prepare_response`."""
        if not self.supports_preemptive_generation:
            raise RuntimeError("agent does not support preemptive generation")
        if prepared.committed:
            raise RuntimeError("prepared agent response has already been committed")
        if (commit_guard is not None and not commit_guard()) or (
            cancel_token and cancel_token.is_cancelled
        ):
            return
        response = _require_agent_text(prepared.response, source="prepared agent response")

        cursor = ExecutionCursor(
            unit_id=f"runner-{uuid4().hex[:8]}",
            unit_kind=UnitKind.AGENT,
            display_name=type(self._agent).__name__,
            entered_at=prepared.started_at_ns or time.monotonic_ns(),
            committable=False,
        )
        recorder.record_unit_entered(cursor)
        prepared.committed = True
        self._history.append({"role": "user", "content": prepared.input_text})
        self._history.append({"role": "assistant", "content": response})
        recorder.record_unit_exited(cursor.with_committable(True), reason=None)

        yield AgentBridgeEvent(kind="text_delta", text=response)
        yield AgentBridgeEvent(kind="done", text=response)

    # ── ExternalAgentBridge interface ────────────────────────────

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
        *,
        commit_guard: Callable[[], bool] | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        """Run one turn, yielding bridge events as they occur."""
        if (commit_guard is not None and not commit_guard()) or (
            cancel_token and cancel_token.is_cancelled
        ):
            return

        stream = (
            self._invoke_bridge(turn_input, recorder, cancel_token, commit_guard)
            if self._is_bridge
            else self._invoke_simple(turn_input, recorder, cancel_token, commit_guard)
        )
        try:
            async for event in stream:
                yield event
        finally:
            # ``async for`` does not forward an early consumer ``aclose()`` into
            # the selected child generator. Close it explicitly so bridge
            # cancellation cleanup and simple-agent cursor rollback finish
            # before a follow-up interruption mutates state.
            await aclose_quietly(stream)

    async def _invoke_bridge(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None,
        commit_guard: Callable[[], bool] | None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        """Drive a wrapped stateful bridge and mirror only completed turns."""
        # Forward runner-managed history so stateless multi-turn bridges that
        # rely on turn_input.context stay stateful across turns.  Any context
        # the caller already set takes precedence, while bridges that own a
        # durable conversation chain can opt out via the optional
        # ``MANAGES_CONVERSATION_HISTORY`` capability.
        #
        # The inner bridge owns the authoritative turn state: it records
        # the user message and any partial assistant output into its own
        # durable store (e.g. checkpointer / message history) and keeps
        # that partial state intentionally on cancel/timeout.  We cannot
        # roll the inner bridge back, so we only mirror a turn into the
        # runner's *advisory* shadow ``_history`` after the inner turn has
        # completed successfully.
        bridge_input = turn_input
        manages_conversation_history = bool(
            getattr(self._agent, "MANAGES_CONVERSATION_HISTORY", False)
        )
        if not manages_conversation_history and not turn_input.context and self._history:
            bridge_input = AgentTurnInput(
                text=turn_input.text,
                context=[item.copy() for item in self._history],
                turn_id=turn_input.turn_id,
                role=turn_input.role,
            )
        accumulated = ""
        timeout = self._config.timeout
        deadline = time.monotonic() + timeout if timeout is not None else None
        inner_iter = self._agent.invoke(bridge_input, recorder, cancel_token)
        tool_drain = _BridgeToolDrain()
        try:
            while True:
                try:
                    event, stop_after = await _next_bridge_event(
                        inner_iter,
                        deadline=deadline,
                        timeout=timeout,
                        cancel_token=cancel_token,
                        tool_drain=tool_drain,
                    )
                except StopAsyncIteration as exc:
                    error = RuntimeError("Agent bridge ended before a terminal done event")
                    recorder.record_framework_error(ErrorInfo.from_exception(error))
                    raise error from exc
                except TimeoutError:
                    # Let the inner bridge keep its own partial state; the
                    # runner never recorded this turn, so its shadow history
                    # stays in sync without a manual rollback.
                    raise AgentTimeoutError(timeout or 0) from None
                if event is None:
                    return
                kind = getattr(event, "kind", None)
                text = _bridge_event_text(event, kind)
                if kind == "text_delta":
                    accumulated += text
                elif kind == "done":
                    await close_stream_after_done(inner_iter)
                    if commit_guard is None or commit_guard():
                        self._append_completed_turn(turn_input, text or accumulated)
                    yield event
                    return
                yield event
                if stop_after:
                    return
        finally:
            # Closing the runner mid-yield does not automatically close the
            # wrapped bridge. Finish its partial-turn cleanup synchronously so
            # a follow-up ``apply_interruption()`` cannot race it.
            await aclose_quietly(inner_iter)

    def _append_completed_turn(
        self,
        turn_input: AgentTurnInput,
        assistant_text: str,
    ) -> None:
        """Mirror one completed bridge turn into advisory runner history."""
        if turn_input.role == "system":
            return
        self._history.append({"role": "user", "content": turn_input.text})
        if assistant_text:
            self._history.append({"role": "assistant", "content": assistant_text})

    async def _invoke_simple(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None,
        commit_guard: Callable[[], bool] | None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        """Drive a simple ``run(text)`` agent with rollback-safe history."""
        if cancel_token and cancel_token.is_cancelled:
            return
        self._validate_plain_turn_input(turn_input)

        cursor: ExecutionCursor | None = None
        if not isinstance(recorder, NullAgentRecorder):
            cursor = ExecutionCursor(
                unit_id=f"runner-{uuid4().hex[:8]}",
                unit_kind=UnitKind.AGENT,
                display_name=type(self._agent).__name__,
                entered_at=time.monotonic_ns(),
                committable=False,
            )
            recorder.record_unit_entered(cursor)

        self._history.append({"role": "user", "content": turn_input.text})

        try:
            response = await _await_with_timeout(
                self._agent.run(turn_input.text), self._config.timeout
            )
            response = _require_agent_text(response, source="plain agent response")
        except TimeoutError:
            self._history.pop()
            self._close_simple_cursor(recorder, cursor, reason="timeout")
            raise AgentTimeoutError(self._config.timeout or 0)
        except Exception:
            self._history.pop()
            self._close_simple_cursor(recorder, cursor, reason="error")
            raise
        except BaseException:
            # A parent ``aclose()`` (barge-in) injects ``GeneratorExit`` /
            # ``CancelledError`` while ``run()`` is awaited; neither is an
            # ``Exception`` so the blocks above are skipped and the
            # still-open agent cursor would be left without a
            # ``unit_exited`` record, breaking the recorder's strict stack
            # invariant for the postmortem journal.  Close it defensively
            # before re-raising.
            self._history.pop()
            self._close_simple_cursor(recorder, cursor, safe=True)
            raise

        if commit_guard is not None and not commit_guard():
            self._history.pop()
            self._close_simple_cursor(recorder, cursor, reason="stale")
            return
        self._history.append({"role": "assistant", "content": response})
        self._close_simple_cursor(recorder, cursor, committable=True)

        if cancel_token and cancel_token.is_cancelled:
            # User barged in while run() was executing — history already
            # reflects the full response so apply_interruption can truncate
            # it later based on audio actually heard.  Skip event emission
            # so downstream TTS doesn't get text the user already cut off.
            return

        yield AgentBridgeEvent(kind="text_delta", text=response)
        yield AgentBridgeEvent(kind="done", text=response)

    @staticmethod
    def _close_simple_cursor(
        recorder: AgentRecorder,
        cursor: ExecutionCursor | None,
        *,
        reason: str | None = None,
        committable: bool = False,
        safe: bool = False,
    ) -> None:
        """Close an optional simple-agent cursor with the requested semantics."""
        if cursor is None:
            return
        if safe:
            recorder.safe_exit_cursor(cursor)
            return
        final_cursor = cursor.with_committable(True) if committable else cursor
        recorder.record_unit_exited(final_cursor, reason=reason)

    def snapshot_state(self) -> FrameworkStateSnapshot:
        if self._is_bridge:
            return self._agent.snapshot_state()
        return FrameworkStateSnapshot(
            fields={
                "agent": type(self._agent).__name__,
                "history_len": len(self._history),
            },
            kind="agent_runner",
        )

    def apply_interruption(
        self,
        delivered_text: str,
        mode: CancellationMode,
        recorder: AgentRecorder | None = None,
        caused_by_signal_id: str | None = None,
    ) -> None:
        # Match the truncation form used by every real bridge: an empty
        # delivered_text clears the assistant message to "" (not a bare
        # "..."), keeping interruption semantics consistent across the
        # apply_interruption contract.
        replacement = delivered_text + "..." if delivered_text else ""
        for i in range(len(self._history) - 1, -1, -1):
            if self._history[i].get("role") == "assistant":
                self._history[i] = {"role": "assistant", "content": replacement}
                break
        if self._is_bridge:
            self._agent.apply_interruption(
                delivered_text,
                mode,
                recorder=recorder,
                caused_by_signal_id=caused_by_signal_id,
            )

    def replace_last_assistant_text(self, text: str) -> None:
        for entry in reversed(self._history):
            if entry.get("role") == "assistant":
                entry["content"] = text
                break
        if self._is_bridge:
            self._agent.replace_last_assistant_text(text)

    def append_interruption_note(self, note: str) -> None:
        # Deduplicate: don't add a second note if one already follows
        # the last user message.
        already_present = False
        for entry in reversed(self._history):
            if entry["role"] == "user":
                break
            if entry == {"role": "system", "content": note}:
                already_present = True
                break
        if not already_present:
            self._history.append({"role": "system", "content": note})
        if self._is_bridge:
            self._agent.append_interruption_note(note)

    def reset(self) -> None:
        self._history.clear()
        if self._is_bridge:
            self._agent.reset()

    async def run(self, text: str) -> str:
        """Convenience: drive :meth:`invoke` and return the final text."""
        accumulated = ""
        async for event in self.invoke(AgentTurnInput.from_text(text), NULL_RECORDER):
            if event.kind == "text_delta":
                accumulated += event.text
            elif event.kind == "done" and event.text:
                accumulated = event.text
        return accumulated

    # ── Lifecycle ──────────────────────────────────────────────

    async def warmup(self) -> None:
        """Delegate warmup to the wrapped agent when it supports it.

        ``AgentRunner`` is the default wrapper Session builds around every
        agent, so without this method ``warmupable(AgentRunner)`` is ``None``
        and the inner bridge never gets a chance to prime its connection
        pool.  Forwards to the wrapped agent's ``warmup`` only when present;
        the bridge owns its own swallow-all contract.
        """
        fn = getattr(self._agent, "warmup", None)
        if fn is not None:
            await fn()

    async def rollback_warmup(self) -> None:
        """Release wrapped warmup resources without permanently closing the agent."""
        fn = getattr(self._agent, "rollback_warmup", None)
        if fn is not None:
            await fn()

    async def aclose(self) -> None:
        """Close the wrapped agent, releasing any held resources."""
        fn = getattr(self._agent, "aclose", None)
        if fn is not None:
            await fn()


__all__ = ["INTERRUPTION_NOTE", "AgentRunner", "AgentRunnerConfig"]
