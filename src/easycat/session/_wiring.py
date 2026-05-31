"""Typed late-binding wiring between a Session and its collaborators.

The Session collaborators (AudioRouter, STTCommitter, TTSScheduler,
CancelOrchestrator, TurnRunner) need to read live Session state —
the active turn, the running flag, the enable_* knobs, the agent /
provider getters, ``emit`` and so on.  Rather than passing each
collaborator its own pile of bare ``Callable[[], Any]`` lambdas
(re-derived inline in the constructor, in an order that was enforced
only by comments), Session builds a single :class:`SessionWiringContext`
once and hands the same typed object to every collaborator.

Two wins:

- The dependency surface is one named, type-checkable object instead of
  ~40 scattered lambdas, so a newcomer can read the wiring in one place.
- There is no construction-order footgun: the context holds *getters*
  that resolve live state when called, so a collaborator can reference
  (say) ``current_turn()`` before another collaborator that also reads
  the turn pointer has been constructed.

This module also holds :class:`_SessionTurnHandle`, the tiny
:class:`~easycat._turn_context.TurnHandle` adapter that forwards the
turn pointer / generation reads-and-writes onto Session — it is wiring
glue, not Session core.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from easycat._turn_context import TurnContext

if TYPE_CHECKING:
    from easycat.providers import STTProvider, TTSProvider
    from easycat.session._session import Session
    from easycat.session._types import Agent
    from easycat.session.actions import SessionActions


class _SessionTurnHandle:
    """Tiny adapter exposing :class:`TurnHandle` on a Session.

    Session is the single authority on the active turn pointer and turn
    generation; this adapter forwards :class:`TurnHandle` reads/writes
    onto the Session attributes so TurnRunner can use the protocol
    without holding a Session reference directly.
    """

    __slots__ = ("_session",)

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def current(self) -> TurnContext | None:
        return self._session._turn

    @property
    def generation(self) -> int:
        return self._session._turn_generation

    @property
    def no_turn(self) -> TurnContext:
        return self._session._no_turn

    def set(self, turn: TurnContext | None) -> None:
        self._session._turn = turn
        if turn is not None:
            self._session._turn_generation = turn.generation


@dataclass(frozen=True)
class SessionWiringContext:
    """Typed late-binding accessors collaborators read off the live Session.

    Built once from a Session and passed to every collaborator
    constructor in place of the per-collaborator lambdas.  Every field is
    a getter/setter/callback that resolves live state when called, so the
    object can be constructed before all collaborators exist.
    """

    # ── Turn pointer / correlation ───────────────────────────────
    current_turn: Callable[[], TurnContext | None]
    active_turn: Callable[[], TurnContext | None]
    correlation_ids: Callable[[], tuple[str | None, str | None]]
    with_correlation: Callable[[Any], Any]
    emit: Callable[[Any], Awaitable[None]]

    # ── Lifecycle flags ──────────────────────────────────────────
    is_running: Callable[[], bool]
    set_running: Callable[[bool], None]
    is_gated: Callable[[], bool]
    is_stt_active: Callable[[], bool]

    # ── Pipeline enable knobs (live values) ──────────────────────
    enable_noise_reduction: Callable[[], bool]
    enable_aec: Callable[[], bool]
    enable_vad: Callable[[], bool]
    auto_turn_from_stt_final: Callable[[], bool]

    # ── Provider / agent getters ─────────────────────────────────
    stt: Callable[[], STTProvider]
    tts: Callable[[], TTSProvider]
    agent: Callable[[], Agent]

    # ── Session-owned services ───────────────────────────────────
    session_actions: Callable[[], SessionActions | None]
    drain_session_actions: Callable[[], Awaitable[bool]]
    telephony_helpers_present: Callable[[], bool]
    caller_id_system_message: Callable[[], str | None]

    # ── Turn-state mutation ──────────────────────────────────────
    clear_turn: Callable[[], None]
    reset_turn_state: Callable[[], None]

    # ── Lifecycle verbs ──────────────────────────────────────────
    cancel_turn: Callable[..., Awaitable[None]]
    stop: Callable[[], Awaitable[None]]


def build_wiring(session: Session) -> SessionWiringContext:
    """Capture the live late-binding accessors off ``session``.

    The getters close over ``session`` so each call resolves the current
    value — the running flag, the active turn, the swapped-in agent, etc.
    ``stop`` is re-read each call so test patches via
    ``session.stop = AsyncMock(...)`` stay observable to the runner.
    """

    def _set_running(value: bool) -> None:
        session._is_running = value

    def _clear_turn() -> None:
        session._turn = None

    return SessionWiringContext(
        current_turn=lambda: session._turn,
        active_turn=session._active_turn,
        correlation_ids=lambda: (
            session.session_id,
            active.id if (active := session._active_turn()) else None,
        ),
        with_correlation=session._with_correlation,
        emit=session._emit,
        is_running=lambda: session._is_running,
        set_running=_set_running,
        is_gated=lambda: session._is_gated,
        is_stt_active=lambda: session._stt_committer.is_active,
        enable_noise_reduction=lambda: session._enable_noise_reduction,
        enable_aec=lambda: session._enable_aec,
        enable_vad=lambda: session._enable_vad,
        auto_turn_from_stt_final=lambda: session._auto_turn_from_stt_final,
        stt=lambda: session.stt,
        tts=lambda: session.tts,
        agent=lambda: session.agent,
        session_actions=lambda: session._session_actions,
        drain_session_actions=session._drain_session_actions,
        telephony_helpers_present=lambda: bool(session.telephony.helpers),
        caller_id_system_message=session._caller_id.system_message,
        clear_turn=_clear_turn,
        reset_turn_state=session._reset_turn_state,
        cancel_turn=session.cancel_turn,
        stop=lambda: session.stop(),
    )


__all__ = ["SessionWiringContext", "_SessionTurnHandle", "build_wiring"]
