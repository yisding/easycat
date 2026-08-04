"""Contracts for canonical Session turn-identity ownership."""

from __future__ import annotations

import pytest

from easycat._turn_context import TurnContext
from easycat.cancel import CancelToken
from easycat.session._session import Session
from easycat.session._turn_lifecycle import TurnLifecycle
from easycat.turn_manager import TurnManagerState
from tests.session._session_core_helpers import _full_config


def test_publish_replaces_identity_and_dual_writes_legacy_generation() -> None:
    lifecycle = TurnLifecycle()
    empty = lifecycle.capture_identity()
    first = TurnContext("turn-1", CancelToken())
    second = TurnContext("turn-2", CancelToken())

    first_lease = lifecycle.publish_identity(first)

    assert not empty.is_current()
    assert first_lease.is_current()
    assert first_lease.value is first
    assert first_lease.generation == first.generation == lifecycle.generation == 1

    second_lease = lifecycle.publish_identity(second)

    assert not first_lease.is_current()
    assert second_lease.is_current()
    assert second_lease.value is second
    assert second_lease.generation == second.generation == lifecycle.generation == 2
    assert lifecycle.current is second


def test_clear_invalidates_identity_even_when_already_empty() -> None:
    lifecycle = TurnLifecycle()
    initial = lifecycle.capture_identity()

    first_clear = lifecycle.clear_identity()
    second_clear = lifecycle.clear_identity()

    assert not initial.is_current()
    assert not first_clear.is_current()
    assert second_clear.is_current()
    assert second_clear.value is None
    assert second_clear.generation == lifecycle.generation == 2


def test_session_private_compatibility_properties_route_through_identity_owner() -> None:
    session = Session(_full_config())
    initial = session._turn_lifecycle.capture_identity()
    turn = TurnContext("compat-turn", CancelToken())

    session._turn = turn

    published = session._turn_lifecycle.capture_identity()
    assert not initial.is_current()
    assert published.value is turn
    assert session.current_turn is turn
    assert session._turn_generation == turn.generation == published.generation
    session._turn_generation = turn.generation
    with pytest.raises(AssertionError):
        session._turn_generation = turn.generation + 1

    session._turn = None

    assert session.current_turn is None
    assert not published.is_current()


def test_session_begin_turn_publishes_through_identity_owner() -> None:
    session = Session(_full_config())
    before = session._turn_lifecycle.capture_identity()

    turn = session.begin_turn("published-turn")

    lease = session._turn_lifecycle.capture_identity()
    assert not before.is_current()
    assert lease.value is turn
    assert lease.generation == turn.generation == session._turn_generation


def test_manager_activity_reset_does_not_stale_retained_session_identity() -> None:
    session = Session(_full_config())
    turn = session.begin_turn("gated-replay-turn")
    session._turn_manager.begin_application_turn(turn.id, turn.cancel_token)
    identity = session._turn_lifecycle.capture_identity()
    activity = session._turn_manager.capture_activity()

    session._turn_manager.reset(preserve_token=True)

    assert session._turn_manager.state is TurnManagerState.IDLE
    assert session.current_turn is turn
    assert identity.is_current()
    assert not activity.is_current()
    assert not turn.cancel_token.is_cancelled
