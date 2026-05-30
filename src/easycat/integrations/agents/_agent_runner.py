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
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from easycat.cancel import CancelToken
from easycat.integrations.agents._helpers import INTERRUPTION_NOTE
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
    UnitKind,
)
from easycat.timeouts import AgentTimeoutError

logger = logging.getLogger(__name__)


# ── Configuration ───────────────────────────────────────────────────


@dataclass
class AgentRunnerConfig:
    """Configuration for AgentRunner."""

    timeout: float | None = 30.0


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

    COMMITTABLE_BOUNDARIES: dict[UnitKind | str, CommitRule] = {
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
        return list(self._history)

    @property
    def is_bridge(self) -> bool:
        """Whether the wrapped agent is itself an ``ExternalAgentBridge``."""
        return self._is_bridge

    # ── ExternalAgentBridge interface ────────────────────────────

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        """Run one turn, yielding bridge events as they occur."""
        if self._is_bridge:
            # Forward runner-managed history so multi-turn bridges that rely
            # on turn_input.context stay stateful across turns.  Any context
            # the caller already set takes precedence.
            #
            # The inner bridge owns the authoritative turn state: it records
            # the user message and any partial assistant output into its own
            # durable store (e.g. checkpointer / message history) and keeps
            # that partial state intentionally on cancel/timeout.  We cannot
            # roll the inner bridge back, so we only mirror a turn into the
            # runner's *advisory* shadow ``_history`` after the inner turn has
            # completed successfully.  This keeps the shadow list and the
            # bridge's real history from drifting apart on timeout/error
            # (which previously caused the next turn to double-feed context).
            bridge_input = turn_input
            if not turn_input.context and self._history:
                bridge_input = AgentTurnInput(
                    text=turn_input.text,
                    context=list(self._history),
                    turn_id=turn_input.turn_id,
                )
            accumulated = ""
            done_text = ""
            timeout = self._config.timeout
            deadline = time.monotonic() + timeout if timeout is not None else None
            inner_iter = self._agent.invoke(bridge_input, recorder, cancel_token)
            timed_out = False
            while True:
                try:
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            timed_out = True
                            break
                        event = await asyncio.wait_for(inner_iter.__anext__(), timeout=remaining)
                    else:
                        event = await inner_iter.__anext__()
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    timed_out = True
                    break
                kind = getattr(event, "kind", None)
                text = getattr(event, "text", "") or ""
                if kind == "text_delta":
                    accumulated += text
                elif kind == "done":
                    done_text = text
                yield event
            if timed_out:
                # Let the inner bridge keep its own partial state; the runner
                # never recorded this turn, so its shadow history stays in
                # sync without a manual rollback.
                aclose = getattr(inner_iter, "aclose", None)
                if aclose is not None:
                    try:
                        await aclose()
                    except Exception:
                        pass
                raise AgentTimeoutError(timeout or 0)
            # Mirror the completed turn into the advisory shadow history.
            self._history.append({"role": "user", "content": turn_input.text})
            final_text = done_text or accumulated
            if final_text:
                self._history.append({"role": "assistant", "content": final_text})
            return

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
            if self._config.timeout is not None:
                response = await asyncio.wait_for(
                    self._agent.run(turn_input.text),
                    timeout=self._config.timeout,
                )
            else:
                response = await self._agent.run(turn_input.text)
        except TimeoutError:
            self._history.pop()
            recorder.record_unit_exited(cursor, reason="timeout")
            raise AgentTimeoutError(self._config.timeout or 0)
        except Exception:
            self._history.pop()
            recorder.record_unit_exited(cursor, reason="error")
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
            recorder.safe_exit_cursor(cursor)
            raise

        self._history.append({"role": "assistant", "content": response})
        recorder.record_unit_exited(cursor.with_committable(True), reason=None)

        if cancel_token and cancel_token.is_cancelled:
            # User barged in while run() was executing — history already
            # reflects the full response so apply_interruption can truncate
            # it later based on audio actually heard.  Skip event emission
            # so downstream TTS doesn't get text the user already cut off.
            return

        yield AgentBridgeEvent(kind="text_delta", text=response)
        yield AgentBridgeEvent(kind="done", text=response)

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

    async def aclose(self) -> None:
        """Close the wrapped agent, releasing any held resources."""
        fn = getattr(self._agent, "aclose", None)
        if fn is not None:
            await fn()


__all__ = ["AgentRunner", "AgentRunnerConfig", "INTERRUPTION_NOTE"]
