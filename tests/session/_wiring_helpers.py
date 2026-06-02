"""Test helper: build a :class:`SessionWiringContext` for direct-construction fixtures.

The session collaborators (AudioRouter, STTCommitter, TTSScheduler,
CancelOrchestrator) take a single ``wiring`` object instead of a pile of
individual callables.  Unit tests that construct a collaborator directly
(without a host Session) use :func:`make_wiring` to assemble a context
with no-op defaults, overriding only the accessors a given test cares
about.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from easycat._turn_context import TurnContext
from easycat.session._wiring import SessionWiringContext


async def _noop_emit(_event: Any) -> None:
    return None


async def _noop_drain() -> bool:
    return False


async def _noop_async(*_args: Any, **_kwargs: Any) -> None:
    return None


def make_wiring(
    *,
    current_turn: Callable[[], TurnContext | None] | None = None,
    correlation_ids: Callable[[], tuple[str | None, str | None]] | None = None,
    with_correlation: Callable[[Any], Any] | None = None,
    emit: Callable[[Any], Awaitable[None]] | None = None,
    is_running: Callable[[], bool] | None = None,
    set_running: Callable[[bool], None] | None = None,
    is_gated: Callable[[], bool] | None = None,
    is_stt_active: Callable[[], bool] | None = None,
    enable_noise_reduction: Callable[[], bool] | None = None,
    enable_aec: Callable[[], bool] | None = None,
    enable_vad: Callable[[], bool] | None = None,
    auto_turn_from_stt_final: Callable[[], bool] | None = None,
    stt: Callable[[], Any] | None = None,
    tts: Callable[[], Any] | None = None,
    agent: Callable[[], Any] | None = None,
    stt_track_label: Callable[[], str | None] | None = None,
    session_actions: Callable[[], Any] | None = None,
    drain_session_actions: Callable[[], Awaitable[bool]] | None = None,
    telephony_helpers_present: Callable[[], bool] | None = None,
    caller_id_system_message: Callable[[], str | None] | None = None,
    clear_turn: Callable[[], None] | None = None,
    reset_turn_state: Callable[[], None] | None = None,
    cancel_turn: Callable[..., Awaitable[None]] | None = None,
    stop: Callable[[], Awaitable[None]] | None = None,
) -> SessionWiringContext:
    """Return a wiring context with no-op defaults; override what a test needs."""
    return SessionWiringContext(
        current_turn=current_turn or (lambda: None),
        correlation_ids=correlation_ids or (lambda: (None, None)),
        with_correlation=with_correlation or (lambda event: event),
        emit=emit or _noop_emit,
        is_running=is_running or (lambda: True),
        set_running=set_running or (lambda _v: None),
        is_gated=is_gated or (lambda: False),
        is_stt_active=is_stt_active or (lambda: False),
        enable_noise_reduction=enable_noise_reduction or (lambda: False),
        enable_aec=enable_aec or (lambda: False),
        enable_vad=enable_vad or (lambda: False),
        auto_turn_from_stt_final=auto_turn_from_stt_final or (lambda: False),
        stt=stt or (lambda: None),
        tts=tts or (lambda: None),
        agent=agent or (lambda: None),
        stt_track_label=stt_track_label or (lambda: None),
        session_actions=session_actions or (lambda: None),
        drain_session_actions=drain_session_actions or _noop_drain,
        telephony_helpers_present=telephony_helpers_present or (lambda: False),
        caller_id_system_message=caller_id_system_message or (lambda: None),
        clear_turn=clear_turn or (lambda: None),
        reset_turn_state=reset_turn_state or (lambda: None),
        cancel_turn=cancel_turn or _noop_async,
        stop=stop or _noop_async,
    )
