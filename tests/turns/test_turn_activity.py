"""Contracts for TurnManager-owned activity generations."""

from __future__ import annotations

import pytest

from easycat.cancel import CancelToken
from easycat.events import EventBus
from easycat.turn_manager import TurnManager, TurnManagerState


def test_application_turn_transition_publishes_state_with_activity_generation() -> None:
    manager = TurnManager(EventBus())
    idle = manager.capture_activity()

    manager.begin_application_turn("application-turn", CancelToken())

    processing = manager.capture_activity()
    assert not idle.is_current()
    assert processing.is_current()
    assert processing.value is manager.state is TurnManagerState.PROCESSING
    assert processing.generation == 1


def test_reset_invalidates_activity_even_when_manager_is_already_idle() -> None:
    manager = TurnManager(EventBus())
    initial = manager.capture_activity()

    manager.reset()
    first_reset = manager.capture_activity()
    manager.reset()
    second_reset = manager.capture_activity()

    assert not initial.is_current()
    assert not first_reset.is_current()
    assert second_reset.is_current()
    assert second_reset.value is manager.state is TurnManagerState.IDLE
    assert second_reset.generation == 2


def test_private_state_compatibility_setter_routes_through_activity_owner() -> None:
    manager = TurnManager(EventBus())
    before = manager.capture_activity()

    manager._state = TurnManagerState.USER_PAUSED

    current = manager.capture_activity()
    assert not before.is_current()
    assert current.is_current()
    assert current.value is manager._state is manager.state is TurnManagerState.USER_PAUSED


@pytest.mark.asyncio
async def test_bot_stopped_idle_transition_invalidates_bot_activity() -> None:
    manager = TurnManager(EventBus())
    manager._state = TurnManagerState.BOT_SPEAKING
    speaking = manager.capture_activity()

    await manager.bot_stopped_speaking()

    idle = manager.capture_activity()
    assert not speaking.is_current()
    assert idle.is_current()
    assert idle.value is manager.state is TurnManagerState.IDLE


def test_reset_preserve_token_invalidates_activity_without_cancelling_token() -> None:
    manager = TurnManager(EventBus())
    token = CancelToken()
    manager.begin_application_turn("retained-turn", token)
    activity = manager.capture_activity()

    manager.reset(preserve_token=True)

    assert not activity.is_current()
    assert manager.state is TurnManagerState.IDLE
    assert not token.is_cancelled
