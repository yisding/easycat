"""Tests for SessionActions queue and session action handling."""

from __future__ import annotations

from easycat.session.actions import SessionAction, SessionActions, SessionActionType


class TestSessionActions:
    """Unit tests for the SessionActions action queue."""

    def test_end_call_enqueues(self):
        actions = SessionActions()
        actions.end_call(reason="goodbye")
        assert actions.has_pending
        drained = actions.drain()
        assert len(drained) == 1
        assert drained[0].type == SessionActionType.END_CALL
        assert drained[0].data == {"reason": "goodbye"}

    def test_end_call_default_reason(self):
        actions = SessionActions()
        actions.end_call()
        drained = actions.drain()
        assert drained[0].data == {"reason": ""}

    def test_transfer_call_enqueues(self):
        actions = SessionActions()
        actions.transfer_call("+15551234567", caller_id="+15559876543")
        drained = actions.drain()
        assert len(drained) == 1
        assert drained[0].type == SessionActionType.TRANSFER_CALL
        assert drained[0].data["target"] == "+15551234567"
        assert drained[0].data["caller_id"] == "+15559876543"

    def test_custom_action(self):
        actions = SessionActions()
        actions.request("play_hold_music", track="jazz")
        drained = actions.drain()
        assert len(drained) == 1
        assert drained[0].type == SessionActionType.CUSTOM
        assert drained[0].data["action_type"] == "play_hold_music"
        assert drained[0].data["track"] == "jazz"

    def test_drain_clears_queue(self):
        actions = SessionActions()
        actions.end_call()
        actions.transfer_call("billing")
        assert actions.has_pending
        drained = actions.drain()
        assert len(drained) == 2
        assert not actions.has_pending
        assert actions.drain() == []

    def test_clear_discards_actions(self):
        actions = SessionActions()
        actions.end_call()
        actions.transfer_call("support")
        assert actions.has_pending
        actions.clear()
        assert not actions.has_pending
        assert actions.drain() == []

    def test_has_pending_false_when_empty(self):
        actions = SessionActions()
        assert not actions.has_pending

    def test_multiple_actions_preserve_order(self):
        actions = SessionActions()
        actions.transfer_call("billing")
        actions.end_call(reason="done")
        drained = actions.drain()
        assert drained[0].type == SessionActionType.TRANSFER_CALL
        assert drained[1].type == SessionActionType.END_CALL

    def test_session_action_frozen(self):
        action = SessionAction(type=SessionActionType.END_CALL, data={"reason": "bye"})
        assert action.type == SessionActionType.END_CALL
        assert action.data == {"reason": "bye"}

    # ── SEND_DTMF ───────────────────────────────────────────

    def test_send_dtmf_enqueues(self):
        actions = SessionActions()
        actions.send_dtmf("1234#")
        drained = actions.drain()
        assert len(drained) == 1
        assert drained[0].type == SessionActionType.SEND_DTMF
        assert drained[0].data == {"digits": "1234#"}

    # ── no_interrupt flag ────────────────────────────────────

    def test_end_call_no_interrupt_default_true(self):
        actions = SessionActions()
        actions.end_call()
        assert actions.no_interrupt is True
        drained = actions.drain()
        assert drained[0].no_interrupt is True

    def test_end_call_no_interrupt_false(self):
        actions = SessionActions()
        actions.end_call(no_interrupt=False)
        assert actions.no_interrupt is False

    def test_transfer_call_no_interrupt_default_true(self):
        actions = SessionActions()
        actions.transfer_call("+15551234567")
        assert actions.no_interrupt is True
        drained = actions.drain()
        assert drained[0].no_interrupt is True

    def test_transfer_call_no_interrupt_false(self):
        actions = SessionActions()
        actions.transfer_call("+15551234567", no_interrupt=False)
        assert actions.no_interrupt is False

    def test_send_dtmf_no_interrupt_default_false(self):
        actions = SessionActions()
        actions.send_dtmf("123")
        assert actions.no_interrupt is False

    def test_custom_action_no_interrupt_default_false(self):
        actions = SessionActions()
        actions.request("custom_thing")
        assert actions.no_interrupt is False

    def test_no_interrupt_any_action(self):
        """no_interrupt is True if ANY queued action has the flag."""
        actions = SessionActions()
        actions.send_dtmf("1")  # no_interrupt=False
        assert actions.no_interrupt is False
        actions.end_call()  # no_interrupt=True
        assert actions.no_interrupt is True

    def test_no_interrupt_cleared_after_drain(self):
        actions = SessionActions()
        actions.end_call()
        assert actions.no_interrupt is True
        actions.drain()
        assert actions.no_interrupt is False
