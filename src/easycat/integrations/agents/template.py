"""BridgeTemplate — starter base class for new agent bridges.

The :class:`~easycat.integrations.agents.base.ExternalAgentBridge`
protocol is six methods, and the shipped bridges run 1,000+ lines
because they translate large framework event vocabularies.  The
*boilerplate*, however, is identical everywhere.  ``BridgeTemplate``
owns it so a new bridge author implements exactly three things:

1. :meth:`stream_events` — translate one framework turn into
   :class:`~easycat.integrations.agents.base.AgentBridgeEvent` items
   (event streaming).
2. :meth:`_plan_interruption` / :meth:`_apply_planned_mutation` —
   interruption planning and its (pure) application.
3. :meth:`snapshot_state` — a JSON-safe snapshot of framework state.

Everything else is inherited:

- the :meth:`invoke` cursor lifecycle, including the ``BaseException``
  / ``safe_exit_cursor`` cleanup arm that keeps the recorder's strict
  enter/exit stack invariant intact when ``AgentRunner`` cancels a
  timed-out turn;
- the public :meth:`apply_interruption`, which delegates the four-step
  atomic write ordering to
  :func:`~easycat.integrations.agents.base.run_interruption_journal_protocol`;
- a default ``COMMITTABLE_BOUNDARIES`` mapping;
- :meth:`_serialize_framework_state`, a secret-scrubbed JSON
  serialization of :meth:`snapshot_state` for artifact storage;
- safe no-op history-mutation methods
  (:meth:`replace_last_assistant_text`, :meth:`append_interruption_note`,
  :meth:`reset`).

Minimal example::

    from easycat.integrations.agents.base import (
        AgentBridgeEvent, CancellationMode, FrameworkStateSnapshot,
        InterruptionPlan,
    )
    from easycat.integrations.agents.template import BridgeTemplate

    class MyFrameworkBridge(BridgeTemplate):
        def __init__(self, agent) -> None:
            super().__init__(display_name=type(agent).__name__)
            self._agent = agent

        async def stream_events(self, turn_input, recorder, cancel_token):
            async for chunk in self._agent.stream(turn_input.text):
                yield AgentBridgeEvent(kind="text_delta", text=chunk)

        def snapshot_state(self) -> FrameworkStateSnapshot:
            return FrameworkStateSnapshot(
                fields={"history_len": len(self._agent.history)},
                kind="my_framework",
            )

        def _plan_interruption(self, delivered_text, mode):
            return InterruptionPlan(
                mutation_kind="interrupt_truncate",
                pre_state_ref=f"my-pre-{id(self._agent):x}",
                post_state_ref=f"my-post-{id(self._agent):x}",
                framework_instructions={"delivered_text": delivered_text},
            )

        def _apply_planned_mutation(self, plan) -> None:
            self._agent.truncate(plan.framework_instructions["delivered_text"])

Pair a finished bridge with
:func:`~easycat.integrations.agents._factory.register_agent_detector`
so ``EasyConfig(agent=my_framework_obj)`` picks it up automatically.
``GenericWorkflowBridge`` is built on this template and is the
in-tree reference implementation.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Mapping
from typing import Any, ClassVar
from uuid import uuid4

from easycat.cancel import CancelToken
from easycat.integrations.agents._helpers import aclose_quietly
from easycat.integrations.agents._text_stream import AgentTextStream
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

logger = logging.getLogger(__name__)


class BridgeTemplate:
    """Concrete base class owning the boilerplate every bridge repeats.

    Subclasses implement :meth:`stream_events`,
    :meth:`_plan_interruption` / :meth:`_apply_planned_mutation`, and
    :meth:`snapshot_state`; everything else (cursor lifecycle, the
    interruption journal protocol, scrubbed state serialization, safe
    no-op mutation methods) is inherited.  Matches the ``STTBase`` /
    ``TTSBase`` precedent: protocol for the contract, base class for
    the shared plumbing.

    Optional extension surface: bridges that consume session-level
    ``mcp_servers`` / ``model`` / ``api_key`` settings may additionally
    implement ``configure_runtime`` — see the *configure_runtime
    contract* section of
    ``docs/teaching/14-bring-your-own-agent/README.md``.  It is not a
    protocol member (and not defined here) because
    ``ExternalAgentBridge`` is ``@runtime_checkable`` and most bridges
    legitimately omit it.
    """

    #: Default committable-boundary map; override per framework.
    COMMITTABLE_BOUNDARIES: ClassVar[Mapping[UnitKind | str, CommitRule]] = {
        UnitKind.WORKFLOW_NODE: CommitRule.BETWEEN_TURNS,
    }

    #: ``unit_kind`` stamped on the per-turn cursor; override per framework.
    TURN_UNIT_KIND: ClassVar[UnitKind | str] = UnitKind.WORKFLOW_NODE

    #: Prefix for generated per-turn cursor ids (``<prefix>-<hex8>``).
    TURN_UNIT_ID_PREFIX: ClassVar[str] = "turn"

    def __init__(self, *, display_name: str | None = None) -> None:
        self._display_name = display_name or type(self).__name__
        #: Last structured output of a turn; subclasses set this during
        #: :meth:`stream_events` and the inherited ``done`` event carries it.
        self._last_output: Any = None

    # ── Author surface (subclasses must implement) ────────────────

    def stream_events(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        """Translate one framework turn into bridge events.

        Implement as an ``async def`` generator (or return an async
        iterator).  Yield ``text_delta`` / ``tool_*`` events as they
        occur; do **not** yield the final ``done`` event — the template
        emits it with the accumulated text and ``self._last_output``.
        """
        raise NotImplementedError(f"{type(self).__name__} must implement stream_events()")

    def snapshot_state(self) -> FrameworkStateSnapshot:
        """Return a JSON-safe snapshot of the current framework state."""
        raise NotImplementedError(f"{type(self).__name__} must implement snapshot_state()")

    def _plan_interruption(self, delivered_text: str, mode: CancellationMode) -> InterruptionPlan:
        """Describe the intended interruption mutation without applying it."""
        raise NotImplementedError(f"{type(self).__name__} must implement _plan_interruption()")

    def _apply_planned_mutation(self, plan: InterruptionPlan) -> None:
        """Apply a previously planned mutation to framework state."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement _apply_planned_mutation()"
        )

    # ── Optional hooks ────────────────────────────────────────────

    def on_turn_start(self, turn_input: AgentTurnInput, recorder: AgentRecorder) -> None:
        """Per-turn setup hook, called after the turn cursor is entered."""

    def check_interruption_supported(self) -> None:
        """Raise to reject :meth:`apply_interruption` before planning.

        Default accepts.  Override to raise (e.g.
        ``ShallowModeInterruptionError``) when the bridge cannot honor
        a mid-turn interruption.
        """

    def _serialize_framework_state(self) -> bytes:
        """Serialize framework state for artifact storage, scrubbed.

        Default: JSON-encode :meth:`snapshot_state` fields after
        recursively redacting secret-looking nested keys and sensitive
        string values. Top-level keys that look like credentials are
        still dropped entirely. Artifacts end up in debug bundles that
        can be shared, so secrets must never be dumped. Override when
        richer (still scrubbed!) state is available.
        """
        from easycat.runtime.safe_defaults import _is_secret_name
        from easycat.validation.redaction import redact_value

        try:
            fields = self.snapshot_state().fields
            scrubbed = {
                k: redact_value(v, str(k))
                for k, v in fields.items()
                if not _is_secret_name(str(k))
            }
            return json.dumps(scrubbed, default=str).encode()
        except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
            return b"{}"

    # ── Inherited boilerplate: invoke lifecycle ───────────────────

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        cursor = ExecutionCursor(
            unit_id=f"{self.TURN_UNIT_ID_PREFIX}-{uuid4().hex[:8]}",
            unit_kind=self.TURN_UNIT_KIND,
            display_name=self._display_name,
            entered_at=time.monotonic_ns(),
            committable=False,
        )
        accumulated = AgentTextStream()
        # ``turn_cursor`` centralizes the enter → error → BaseException →
        # clean-exit ordering.  The ``BaseException`` arm matters because the
        # default ``AgentRunner`` enforces its timeout by cancelling the pending
        # ``__anext__()`` (and calling ``aclose()``), injecting
        # ``asyncio.CancelledError`` / ``GeneratorExit`` here; the cm closes the
        # still-open turn cursor defensively before re-raising.
        with recorder.turn_cursor(cursor):
            self._last_output = None
            self.on_turn_start(turn_input, recorder)
            stream = self.stream_events(turn_input, recorder, cancel_token)
            try:
                async for ev in stream:
                    accumulated.apply(ev)
                    yield ev
            finally:
                # ``async for`` does not forward an early consumer
                # ``aclose()`` (barge-in ``GeneratorExit``) into the delegated
                # author generator; close it explicitly so its cleanup runs
                # synchronously before a follow-up ``apply_interruption()``.
                await aclose_quietly(stream)

        yield AgentBridgeEvent(
            kind="done",
            text=accumulated.text,
            structured_output=self._last_output,
        )

    # ── Inherited boilerplate: interruption protocol ──────────────

    def apply_interruption(
        self,
        delivered_text: str,
        mode: CancellationMode,
        recorder: AgentRecorder | None = None,
        caused_by_signal_id: str | None = None,
    ) -> None:
        """Plan, journal, and apply an interruption mutation.

        Runs the four-step atomic write ordering (plan →
        ``FrameworkStateCommitted`` → apply → paired success/failure)
        via :func:`run_interruption_journal_protocol`.
        """
        self.check_interruption_supported()
        plan = self._plan_interruption(delivered_text, mode)
        run_interruption_journal_protocol(
            plan,
            mode,
            recorder,
            caused_by_signal_id,
            serialize_state=self._serialize_framework_state,
            apply_mutation=self._apply_planned_mutation,
        )

    # ── Inherited boilerplate: safe no-op mutation methods ────────

    def replace_last_assistant_text(self, text: str) -> None:
        """No-op by default; override if the bridge keeps its own history."""

    def append_interruption_note(self, note: str) -> None:
        """No-op by default; override if the bridge keeps its own history."""

    def reset(self) -> None:
        """Clear template-held state; extend to clear framework state."""
        self._last_output = None
