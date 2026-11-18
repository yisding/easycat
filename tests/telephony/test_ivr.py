"""Tests for IVR navigator."""

from __future__ import annotations

import asyncio
import gc
import logging
import math
import weakref
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from easycat._dtmf_tones import DTMFToneConfig, DTMFTonePlayback
from easycat.events import EventBus, STTFinal
from easycat.session._media_output import SessionMediaOutput
from easycat.telephony.ivr import (
    DTMFDelivery,
    InBandDTMFDelivery,
    IVRAction,
    IVRActionType,
    IVRDTMFSender,
    IVRNavigator,
    IVRNavigatorConfig,
    classify_ivr_prompt,
    detect_human_after_ivr,
)


class TestIVRNavigatorConfig:
    def test_defaults(self) -> None:
        cfg = IVRNavigatorConfig()
        assert cfg.max_depth == 10
        assert cfg.prompt_timeout_s == 15.0

    def test_configurable_max_depth(self) -> None:
        cfg = IVRNavigatorConfig(max_depth=5)
        assert cfg.max_depth == 5

    @pytest.mark.parametrize("max_depth", [0, -1])
    def test_rejects_non_positive_max_depth(self, max_depth: int) -> None:
        with pytest.raises(ValueError, match="max_depth"):
            IVRNavigatorConfig(max_depth=max_depth)

    @pytest.mark.parametrize("field", ["prompt_timeout_s", "agent_timeout_s"])
    def test_rejects_non_positive_timeouts(self, field: str) -> None:
        with pytest.raises(ValueError, match=field):
            IVRNavigatorConfig(**{field: 0.0})
        with pytest.raises(ValueError, match=field):
            IVRNavigatorConfig(**{field: -0.1})

    @pytest.mark.parametrize("field", ["prompt_timeout_s", "agent_timeout_s"])
    @pytest.mark.parametrize("value", [math.nan, math.inf])
    def test_rejects_non_finite_timeouts(self, field: str, value: float) -> None:
        with pytest.raises(ValueError, match=field):
            IVRNavigatorConfig(**{field: value})

    @pytest.mark.parametrize("field", ["agent_retry_delay_s", "hold_silence_threshold_s"])
    def test_rejects_negative_duration_knobs(self, field: str) -> None:
        with pytest.raises(ValueError, match=field):
            IVRNavigatorConfig(**{field: -0.1})

    @pytest.mark.parametrize("field", ["agent_retry_delay_s", "hold_silence_threshold_s"])
    @pytest.mark.parametrize("value", [math.nan, math.inf])
    def test_rejects_non_finite_duration_knobs(self, field: str, value: float) -> None:
        with pytest.raises(ValueError, match=field):
            IVRNavigatorConfig(**{field: value})

    @pytest.mark.parametrize(
        "field",
        [
            "prompt_timeout_s",
            "agent_timeout_s",
            "agent_retry_delay_s",
            "hold_silence_threshold_s",
        ],
    )
    def test_rejects_integers_too_large_for_float_with_value_error(self, field: str) -> None:
        # ``math.isfinite`` converts to a C double and raises OverflowError for
        # ints above ~1.8e308; config validation must raise its documented
        # ValueError instead (see ``easycat._numeric.is_finite_number``).
        with pytest.raises(ValueError, match=field):
            IVRNavigatorConfig(**{field: 10**400})


class TestIVRNavigator:
    def test_start_stop_lifecycle(self) -> None:
        bus = EventBus()
        nav = IVRNavigator(bus)
        nav.start()
        assert nav._started is True
        nav.stop()
        assert nav._started is False

    @pytest.mark.asyncio
    async def test_receives_stt_final_during_ivr_state(self) -> None:
        bus = EventBus()
        received_prompts: list[str] = []

        async def mock_agent(ctx: dict) -> dict:
            received_prompts.append(ctx["prompt"])
            return {"action": "wait"}

        nav = IVRNavigator(bus, agent_callback=mock_agent)
        nav.start()
        nav.activate()
        try:
            await bus.emit(STTFinal(text="press 1 for sales"))
            assert received_prompts == ["press 1 for sales"]
        finally:
            nav.stop()

    @pytest.mark.asyncio
    async def test_ignores_stt_when_not_active(self) -> None:
        bus = EventBus()
        received_prompts: list[str] = []

        async def mock_agent(ctx: dict) -> dict:
            received_prompts.append(ctx["prompt"])
            return {"action": "wait"}

        nav = IVRNavigator(bus, agent_callback=mock_agent)
        nav.start()
        # Not activated
        try:
            await bus.emit(STTFinal(text="press 1 for sales"))
            assert received_prompts == []
        finally:
            nav.stop()

    def test_activate_deactivate(self) -> None:
        bus = EventBus()
        nav = IVRNavigator(bus)
        inactive = nav._activation_epoch.capture()
        assert nav._active is False
        nav.activate()
        active = nav._activation_epoch.capture()
        assert nav._active is True
        assert not inactive.is_current()
        nav.activate()
        assert active.is_current()
        nav.deactivate()
        assert nav._active is False
        assert not active.is_current()


class TestIVRAgentDecision:
    @pytest.mark.asyncio
    async def test_deactivate_discards_blocked_agent_decision(self) -> None:
        bus = EventBus()
        actions: list[IVRAction] = []
        bus.subscribe(IVRAction, actions.append)
        callback_started = asyncio.Event()
        release_callback = asyncio.Event()
        delivery = MagicMock(spec=DTMFDelivery)
        delivery.send_dtmf_with_retry = AsyncMock(return_value=True)

        async def blocked_agent(ctx: dict) -> dict:
            callback_started.set()
            await release_callback.wait()
            return {"action": "dtmf", "digits": "9"}

        nav = IVRNavigator(bus, agent_callback=blocked_agent, dtmf_delivery=delivery)
        nav.start()
        nav.activate()
        emit_task = asyncio.create_task(bus.emit(STTFinal(text="Press 9 for support")))
        try:
            await asyncio.wait_for(callback_started.wait(), timeout=1.0)
            nav.deactivate()
            release_callback.set()
            await asyncio.wait_for(emit_task, timeout=1.0)

            assert actions == []
            assert nav.menu_depth == 0
            assert nav.history == []
            assert nav._prompt_timeout_task is None
            delivery.send_dtmf_with_retry.assert_not_awaited()
        finally:
            release_callback.set()
            if not emit_task.done():
                await emit_task
            nav.stop()

    @pytest.mark.asyncio
    async def test_reactivate_does_not_adopt_blocked_prior_activation_decision(self) -> None:
        bus = EventBus()
        actions: list[IVRAction] = []
        bus.subscribe(IVRAction, actions.append)
        callback_started = asyncio.Event()
        release_callback = asyncio.Event()
        delivery = MagicMock(spec=DTMFDelivery)
        delivery.send_dtmf_with_retry = AsyncMock(return_value=True)

        async def blocked_agent(ctx: dict) -> dict:
            callback_started.set()
            await release_callback.wait()
            return {"action": "dtmf", "digits": "9"}

        nav = IVRNavigator(bus, agent_callback=blocked_agent, dtmf_delivery=delivery)
        nav.start()
        nav.activate()
        emit_task = asyncio.create_task(bus.emit(STTFinal(text="Press 9 for support")))
        try:
            await asyncio.wait_for(callback_started.wait(), timeout=1.0)
            nav.deactivate()
            nav.activate()
            release_callback.set()
            await asyncio.wait_for(emit_task, timeout=1.0)

            assert nav._active is True
            assert actions == []
            assert nav.menu_depth == 0
            assert nav.history == []
            assert nav._prompt_timeout_task is None
            delivery.send_dtmf_with_retry.assert_not_awaited()
        finally:
            release_callback.set()
            if not emit_task.done():
                await emit_task
            nav.stop()

    @pytest.mark.asyncio
    async def test_agent_returns_dtmf_action(self) -> None:
        bus = EventBus()
        actions: list[IVRAction] = []
        bus.subscribe(IVRAction, actions.append)

        async def mock_agent(ctx: dict) -> dict:
            return {"action": "dtmf", "digits": "1"}

        nav = IVRNavigator(bus, agent_callback=mock_agent)
        nav.start()
        nav.activate()
        try:
            await bus.emit(STTFinal(text="Press 1 for sales"))
            assert len(actions) == 1
            assert actions[0].type == IVRActionType.DTMF
            assert actions[0].digits == "1"
        finally:
            nav.stop()

    @pytest.mark.asyncio
    async def test_agent_returns_speak_action(self) -> None:
        bus = EventBus()
        actions: list[IVRAction] = []
        bus.subscribe(IVRAction, actions.append)

        async def mock_agent(ctx: dict) -> dict:
            return {"action": "speak", "text": "billing"}

        nav = IVRNavigator(bus, agent_callback=mock_agent)
        nav.start()
        nav.activate()
        try:
            await bus.emit(STTFinal(text="Say billing or sales"))
            assert len(actions) == 1
            assert actions[0].type == IVRActionType.SPEAK
            assert actions[0].text == "billing"
        finally:
            nav.stop()

    @pytest.mark.asyncio
    async def test_agent_returns_wait_action(self) -> None:
        bus = EventBus()
        actions: list[IVRAction] = []
        bus.subscribe(IVRAction, actions.append)

        async def mock_agent(ctx: dict) -> dict:
            return {"action": "wait"}

        nav = IVRNavigator(bus, agent_callback=mock_agent)
        nav.start()
        nav.activate()
        try:
            await bus.emit(STTFinal(text="Please hold"))
            # "wait" doesn't emit an action immediately.
            assert len(actions) == 0
        finally:
            nav.stop()

    @pytest.mark.asyncio
    async def test_agent_returns_hangup_action(self) -> None:
        bus = EventBus()
        actions: list[IVRAction] = []
        bus.subscribe(IVRAction, actions.append)

        async def mock_agent(ctx: dict) -> dict:
            return {"action": "hangup"}

        nav = IVRNavigator(bus, agent_callback=mock_agent)
        nav.start()
        nav.activate()
        try:
            await bus.emit(STTFinal(text="invalid menu"))
            assert len(actions) == 1
            assert actions[0].type == IVRActionType.HANGUP
        finally:
            nav.stop()

    @pytest.mark.asyncio
    async def test_malformed_agent_result_waits_without_advancing(self) -> None:
        bus = EventBus()
        actions: list[IVRAction] = []
        bus.subscribe(IVRAction, actions.append)

        async def mock_agent(ctx: dict) -> Any:
            return None

        nav = IVRNavigator(bus, agent_callback=mock_agent)
        nav.start()
        nav.activate()
        try:
            await bus.emit(STTFinal(text="Press 1 for sales"))
            assert actions == []
            assert nav.menu_depth == 0
            assert nav._prompt_timeout_task is not None
        finally:
            nav.stop()

    @pytest.mark.asyncio
    async def test_returned_exception_is_a_malformed_result_not_a_crash(self) -> None:
        bus = EventBus()
        actions: list[IVRAction] = []
        bus.subscribe(IVRAction, actions.append)
        call_count = 0

        async def mock_agent(ctx: dict) -> Any:
            nonlocal call_count
            call_count += 1
            return ValueError("returned rather than raised")

        nav = IVRNavigator(
            bus,
            agent_callback=mock_agent,
            config=IVRNavigatorConfig(agent_retry_delay_s=0.0),
        )
        nav.start()
        nav.activate()
        try:
            await bus.emit(STTFinal(text="Press 1 for sales"))
            assert call_count == 1
            assert actions == []
            assert nav.menu_depth == 0
            assert nav._prompt_timeout_task is not None
        finally:
            nav.stop()

    @pytest.mark.asyncio
    async def test_agent_timeout_retries_prompt(self) -> None:
        bus = EventBus()
        actions: list[IVRAction] = []
        bus.subscribe(IVRAction, actions.append)
        call_count = 0
        never_released = asyncio.Event()

        async def slow_then_fast_agent(ctx: dict) -> dict:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                await never_released.wait()
            return {"action": "dtmf", "digits": "1"}

        cfg = IVRNavigatorConfig(agent_timeout_s=0.05)
        nav = IVRNavigator(bus, agent_callback=slow_then_fast_agent, config=cfg)
        nav.start()
        nav.activate()
        try:
            await bus.emit(STTFinal(text="Press 1 for sales"))
            # First call times out, retry succeeds.
            assert call_count == 2
            dtmf_actions = [a for a in actions if a.type == IVRActionType.DTMF]
            assert len(dtmf_actions) == 1
        finally:
            nav.stop()

    @pytest.mark.asyncio
    async def test_agent_persistent_crash_escalates_to_hangup(self) -> None:
        bus = EventBus()
        actions: list[IVRAction] = []
        bus.subscribe(IVRAction, actions.append)
        call_count = 0

        async def crashing_agent(ctx: dict) -> dict:
            nonlocal call_count
            call_count += 1
            raise TypeError("malformed callback")

        cfg = IVRNavigatorConfig(agent_retry_delay_s=0.0)
        nav = IVRNavigator(bus, agent_callback=crashing_agent, config=cfg)
        nav.start()
        nav.activate()
        try:
            await bus.emit(STTFinal(text="Press 1 for sales"))
            # First crash retries once; the retry crashes too → escalate.
            assert call_count == 2
            hangups = [a for a in actions if a.type == IVRActionType.HANGUP]
            assert len(hangups) == 1
            assert nav._active is False
        finally:
            nav.stop()

    @pytest.mark.asyncio
    async def test_agent_crash_then_recover(self) -> None:
        bus = EventBus()
        actions: list[IVRAction] = []
        bus.subscribe(IVRAction, actions.append)
        call_count = 0

        async def flaky_agent(ctx: dict) -> dict:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise KeyError("transient")
            return {"action": "dtmf", "digits": "1"}

        cfg = IVRNavigatorConfig(agent_retry_delay_s=0.0)
        nav = IVRNavigator(bus, agent_callback=flaky_agent, config=cfg)
        nav.start()
        nav.activate()
        try:
            await bus.emit(STTFinal(text="Press 1 for sales"))
            # First attempt crashes, retry succeeds → DTMF, no hangup.
            assert call_count == 2
            dtmf_actions = [a for a in actions if a.type == IVRActionType.DTMF]
            assert len(dtmf_actions) == 1
            hangups = [a for a in actions if a.type == IVRActionType.HANGUP]
            assert len(hangups) == 0
        finally:
            nav.stop()

    @pytest.mark.asyncio
    async def test_agent_crash_then_timeout_is_transient(self) -> None:
        """A crash followed by a slow retry is transient, like timeout-then-timeout.

        The first-attempt crash and the first-attempt timeout retry tails must
        treat an identical retry ``TimeoutError`` the same way: re-arm the prompt
        timeout and wait rather than escalating to hangup.
        """
        bus = EventBus()
        actions: list[IVRAction] = []
        bus.subscribe(IVRAction, actions.append)
        call_count = 0

        async def crash_then_slow_agent(ctx: dict) -> dict:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise KeyError("transient crash")
            await asyncio.sleep(1.0)  # exceed agent_timeout_s on the retry
            return {"action": "dtmf", "digits": "1"}

        cfg = IVRNavigatorConfig(agent_timeout_s=0.05, agent_retry_delay_s=0.0)
        nav = IVRNavigator(bus, agent_callback=crash_then_slow_agent, config=cfg)
        nav.start()
        nav.activate()
        try:
            await bus.emit(STTFinal(text="Press 1 for sales"))
            # First crash, retry times out → transient: re-arm, no hangup.
            assert call_count == 2
            hangups = [a for a in actions if a.type == IVRActionType.HANGUP]
            assert len(hangups) == 0
            assert nav._active is True
            assert nav._prompt_timeout_task is not None
        finally:
            nav.stop()

    @pytest.mark.asyncio
    async def test_agent_receives_full_context(self) -> None:
        bus = EventBus()
        captured_contexts: list[dict] = []

        async def mock_agent(ctx: dict) -> dict:
            captured_contexts.append(ctx)
            return {"action": "dtmf", "digits": "1"}

        nav = IVRNavigator(bus, agent_callback=mock_agent)
        nav.start()
        nav.activate()
        try:
            await bus.emit(STTFinal(text="Press 1 for sales"))
            ctx = captured_contexts[0]
            assert "prompt" in ctx
            assert "menu_depth" in ctx
            assert "history" in ctx
            assert ctx["prompt"] == "Press 1 for sales"
            assert ctx["menu_depth"] == 0
        finally:
            nav.stop()


class TestIVRNavigation:
    @pytest.mark.asyncio
    async def test_single_level_navigation(self) -> None:
        bus = EventBus()
        actions: list[IVRAction] = []
        bus.subscribe(IVRAction, actions.append)

        async def mock_agent(ctx: dict) -> dict:
            return {"action": "dtmf", "digits": "1"}

        nav = IVRNavigator(bus, agent_callback=mock_agent)
        nav.start()
        nav.activate()
        try:
            await bus.emit(STTFinal(text="Press 1 for sales"))
            assert len(actions) == 1
            assert actions[0].digits == "1"
        finally:
            nav.stop()

    @pytest.mark.asyncio
    async def test_multi_level_navigation(self) -> None:
        bus = EventBus()
        actions: list[IVRAction] = []
        bus.subscribe(IVRAction, actions.append)

        call_count = 0

        async def mock_agent(ctx: dict) -> dict:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"action": "dtmf", "digits": "1"}
            return {"action": "dtmf", "digits": "3"}

        nav = IVRNavigator(bus, agent_callback=mock_agent)
        nav.start()
        nav.activate()
        try:
            await bus.emit(STTFinal(text="Press 1 for sales"))
            await bus.emit(STTFinal(text="Press 3 for returns"))
            assert len(actions) == 2
            assert actions[0].digits == "1"
            assert actions[1].digits == "3"
        finally:
            nav.stop()

    @pytest.mark.asyncio
    async def test_menu_depth_tracked(self) -> None:
        bus = EventBus()

        async def mock_agent(ctx: dict) -> dict:
            return {"action": "dtmf", "digits": "1"}

        nav = IVRNavigator(bus, agent_callback=mock_agent)
        nav.start()
        nav.activate()
        try:
            await bus.emit(STTFinal(text="Press 1"))
            await bus.emit(STTFinal(text="Press 2"))
            assert nav.menu_depth == 2
        finally:
            nav.stop()

    @pytest.mark.asyncio
    async def test_navigation_history_stored(self) -> None:
        bus = EventBus()

        async def mock_agent(ctx: dict) -> dict:
            return {"action": "dtmf", "digits": "1"}

        nav = IVRNavigator(bus, agent_callback=mock_agent)
        nav.start()
        nav.activate()
        try:
            await bus.emit(STTFinal(text="Press 1 for sales"))
            history = nav.history
            assert len(history) == 1
            assert history[0][0] == "Press 1 for sales"
            assert history[0][1]["action"] == "dtmf"
        finally:
            nav.stop()

    @pytest.mark.asyncio
    async def test_max_depth_exceeded(self) -> None:
        bus = EventBus()
        actions: list[IVRAction] = []
        bus.subscribe(IVRAction, actions.append)

        async def mock_agent(ctx: dict) -> dict:
            return {"action": "dtmf", "digits": "1"}

        cfg = IVRNavigatorConfig(max_depth=2)
        nav = IVRNavigator(bus, agent_callback=mock_agent, config=cfg)
        nav.start()
        nav.activate()
        try:
            await bus.emit(STTFinal(text="Level 1"))
            await bus.emit(STTFinal(text="Level 2"))
            await bus.emit(STTFinal(text="Level 3"))
            # Third navigation exceeds max_depth=2 → hangup emitted
            hangups = [a for a in actions if a.type == IVRActionType.HANGUP]
            assert len(hangups) >= 1
        finally:
            nav.stop()

    @pytest.mark.asyncio
    async def test_ivr_timeout_reprompt(self) -> None:
        bus = EventBus()
        actions: list[IVRAction] = []
        bus.subscribe(IVRAction, actions.append)

        async def mock_agent(ctx: dict) -> dict:
            return {"action": "wait"}

        cfg = IVRNavigatorConfig(prompt_timeout_s=0.05)
        nav = IVRNavigator(bus, agent_callback=mock_agent, config=cfg)
        nav.start()
        nav.activate()
        try:
            await bus.emit(STTFinal(text="Press 1 for sales"))
            await asyncio.sleep(0.3)
            # Timeout should emit a WAIT action.
            waits = [a for a in actions if a.type == IVRActionType.WAIT]
            assert len(waits) >= 1
        finally:
            nav.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_owned_prompt_timer(self) -> None:
        bus = EventBus()

        async def mock_agent(_ctx: dict) -> dict:
            return {"action": "wait"}

        nav = IVRNavigator(
            bus,
            agent_callback=mock_agent,
            config=IVRNavigatorConfig(prompt_timeout_s=5.0),
        )
        nav.start()
        nav.activate()
        await bus.emit(STTFinal(text="Press 1 for sales"))

        timer = nav._prompt_timeout_task
        assert timer is not None
        assert nav._timer_tasks.tasks() == (timer,)

        nav.stop()
        await asyncio.sleep(0)

        assert timer.cancelled()
        assert nav._timer_tasks.empty
        assert nav._prompt_timeout_task is None


class TestIVRDetection:
    def test_detects_ivr_prompt_with_numbers(self) -> None:
        assert classify_ivr_prompt("Press 1 for sales, 2 for support") is True

    def test_detects_speech_ivr(self) -> None:
        assert classify_ivr_prompt("Say billing or sales") is True

    def test_human_speech_not_ivr(self) -> None:
        assert classify_ivr_prompt("Hello, how can I help you?") is False

    def test_auto_attendant_extension_prompt(self) -> None:
        assert classify_ivr_prompt("If you know your party's extension, dial it now") is True

    def test_pbx_call_confirmation_prompt(self) -> None:
        assert classify_ivr_prompt("You have a call. Press 1 to accept") is True

    def test_early_media_not_classified_as_ivr(self) -> None:
        assert classify_ivr_prompt("This call may be monitored for quality") is False

    def test_early_media_hold_message_ignored(self) -> None:
        assert classify_ivr_prompt("Please hold while we connect your call") is False

    @pytest.mark.parametrize(
        "text",
        [
            "Thank you for calling Acme. This call may be recorded. Press 1 for sales.",
            "Your call is important to us. For billing, press 3.",
            "Please hold while we connect you. If you know your party's extension, dial it now.",
        ],
    )
    def test_early_media_phrase_with_ivr_cue_is_still_ivr(self, text: str) -> None:
        assert classify_ivr_prompt(text) is True

    def test_hold_music_detection(self) -> None:
        bus = EventBus()
        nav = IVRNavigator(bus, config=IVRNavigatorConfig(hold_silence_threshold_s=5.0))
        nav.activate()
        assert not nav.in_hold
        nav.notify_silence(6.0)
        assert nav.in_hold

    def test_transfer_to_human_detected(self) -> None:
        assert detect_human_after_ivr("Hi, this is John, how can I help you?") is True
        assert detect_human_after_ivr("Press 1 for sales") is False

    def test_hunt_group_variable_ring_time(self) -> None:
        """Long ring time through hunt group should not prematurely classify."""
        bus = EventBus()
        nav = IVRNavigator(bus, config=IVRNavigatorConfig(hold_silence_threshold_s=30.0))
        nav.activate()
        # 25 seconds of silence is below the 30s threshold.
        nav.notify_silence(25.0)
        assert not nav.in_hold


# ── DTMF delivery ────────────────────────────────────────────────


class TestIVRDTMFDelivery:
    @pytest.mark.asyncio
    async def test_dtmf_sent_via_rest_api_not_websocket(self) -> None:
        mock_client = MagicMock()
        delivery = DTMFDelivery(
            twilio_client=mock_client, call_sid="CA123", inter_digit_delay=False
        )
        result = await delivery.send_dtmf("1")
        assert result is True
        mock_client.calls("CA123").update.assert_called_once()
        twiml_arg = mock_client.calls("CA123").update.call_args.kwargs["twiml"]
        assert '<Play digits="1"/>' in twiml_arg

    @pytest.mark.asyncio
    async def test_dtmf_inter_digit_delay(self) -> None:
        mock_client = MagicMock()
        delivery = DTMFDelivery(
            twilio_client=mock_client, call_sid="CA123", inter_digit_delay=True
        )
        result = await delivery.send_dtmf("123")
        assert result is True
        twiml_arg = mock_client.calls("CA123").update.call_args.kwargs["twiml"]
        assert '<Play digits="1W2W3"/>' in twiml_arg

    @pytest.mark.asyncio
    async def test_dtmf_verify_option(self) -> None:
        """Verify option exists on DTMFDelivery config."""
        delivery = DTMFDelivery(verify=True, call_sid="CA123")
        assert delivery._verify is True

    @pytest.mark.asyncio
    async def test_dtmf_rejects_invalid_characters(self) -> None:
        """Digits containing non-DTMF characters are rejected to prevent TwiML injection."""
        mock_client = MagicMock()
        delivery = DTMFDelivery(
            twilio_client=mock_client, call_sid="CA123", inter_digit_delay=False
        )
        result = await delivery.send_dtmf('1"/><Say>hacked</Say><Play digits="2')
        assert result is False
        mock_client.calls("CA123").update.assert_not_called()

    @pytest.mark.asyncio
    async def test_dtmf_rejects_empty_digits(self) -> None:
        mock_client = MagicMock()
        delivery = DTMFDelivery(
            twilio_client=mock_client, call_sid="CA123", inter_digit_delay=False
        )
        result = await delivery.send_dtmf("")
        assert result is False
        mock_client.calls("CA123").update.assert_not_called()

    @pytest.mark.asyncio
    async def test_dtmf_accepts_valid_characters(self) -> None:
        mock_client = MagicMock()
        delivery = DTMFDelivery(
            twilio_client=mock_client, call_sid="CA123", inter_digit_delay=False
        )
        result = await delivery.send_dtmf("*#0123456789wW")
        assert result is True

    @pytest.mark.asyncio
    async def test_dtmf_delivery_failure_fallback(self) -> None:
        bus = EventBus()
        actions: list[IVRAction] = []
        bus.subscribe(IVRAction, actions.append)

        mock_client = MagicMock()
        mock_client.calls("CA123").update.side_effect = RuntimeError("network error")

        delivery = DTMFDelivery(
            twilio_client=mock_client, call_sid="CA123", inter_digit_delay=False
        )

        async def mock_agent(ctx: dict) -> dict:
            return {"action": "dtmf", "digits": "1"}

        nav = IVRNavigator(bus, agent_callback=mock_agent, dtmf_delivery=delivery)
        nav.start()
        nav.activate()
        try:
            await bus.emit(STTFinal(text="Press 1 for sales"))
            # DTMF delivery failed → should have fallback SPEAK action.
            speak_actions = [a for a in actions if a.type == IVRActionType.SPEAK]
            assert len(speak_actions) >= 1
        finally:
            nav.stop()


class TestSenderAsyncContract:
    async def test_navigator_names_a_non_awaitable_send_dtmf_with_retry(self) -> None:
        class _SyncSender:
            def send_dtmf_with_retry(self, digits: str, *, should_continue: Any = None) -> bool:
                return True

            async def send_speech(self, text: str) -> bool:
                return True

        bus = EventBus(handler_error_policy="raise")

        async def press_one(ctx: dict) -> dict:
            return {"action": "dtmf", "digits": "1"}

        nav = IVRNavigator(bus, agent_callback=press_one, dtmf_delivery=_SyncSender())
        nav.start()
        nav.activate()
        try:
            with pytest.raises(TypeError, match=r"send_dtmf_with_retry\(\) must be async"):
                await bus.emit(STTFinal(text="Press 1 for sales"))
        finally:
            nav.stop()

    async def test_a_wrapper_returning_a_coroutine_still_works(self) -> None:
        sent: list[str] = []

        class _WrappedSender:
            def send_dtmf_with_retry(self, digits: str, *, should_continue: Any = None) -> Any:
                async def run() -> bool:
                    sent.append(digits)
                    return True

                return run()

            async def send_speech(self, text: str) -> bool:
                return True

        bus = EventBus(handler_error_policy="raise")
        actions: list[IVRAction] = []
        bus.subscribe(IVRAction, actions.append)

        async def press_one(ctx: dict) -> dict:
            return {"action": "dtmf", "digits": "1"}

        nav = IVRNavigator(bus, agent_callback=press_one, dtmf_delivery=_WrappedSender())
        nav.start()
        nav.activate()
        try:
            await bus.emit(STTFinal(text="Press 1 for sales"))
        finally:
            nav.stop()

        assert sent == ["1"]
        assert not [a for a in actions if a.type == IVRActionType.SPEAK]


class _FakeMediaOutput:
    """Stand-in for ``SessionMediaOutput`` recording tone and speech calls."""

    def __init__(self, outcome: str | None = "played", *, error: Exception | None = None) -> None:
        self._outcome = outcome
        self._error = error
        self.tone_calls: list[tuple[str, dict[str, Any]]] = []
        self.speech: list[str] = []
        self.speech_error: Exception | None = None

    async def play_dtmf_tones(self, digits: str, **kwargs: Any) -> DTMFTonePlayback:
        self.tone_calls.append((digits, kwargs))
        if self._error is not None:
            raise self._error
        done: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        if self._outcome is not None:
            done.set_result(self._outcome)
        return DTMFTonePlayback(key_count=len(digits), duration_ms=1, mark_name=None, _done=done)

    async def synthesize_bypass(self, text: str) -> None:
        if self.speech_error is not None:
            raise self.speech_error
        self.speech.append(text)


def _bound(output: _FakeMediaOutput, **kwargs: Any) -> InBandDTMFDelivery:
    delivery = InBandDTMFDelivery(**kwargs)
    delivery.bind_output(output)  # type: ignore[arg-type]
    return delivery


class TestInBandDTMFDelivery:
    def test_both_senders_satisfy_the_protocol(self) -> None:
        assert isinstance(DTMFDelivery(), IVRDTMFSender)
        assert isinstance(InBandDTMFDelivery(), IVRDTMFSender)

    @pytest.mark.parametrize(
        ("kwargs", "error"),
        [
            ({"tone_config": {"tone_ms": 80}}, TypeError),
            ({"inter_digit_delay_ms": 1.5}, TypeError),
            ({"inter_digit_delay_ms": True}, TypeError),
            ({"inter_digit_delay_ms": -1}, ValueError),
            ({"wait_for_playback": "yes"}, TypeError),
            ({"ack_timeout_margin_s": 0}, ValueError),
            ({"ack_timeout_margin_s": math.nan}, ValueError),
        ],
    )
    def test_constructor_validation(self, kwargs: dict[str, Any], error: type[Exception]) -> None:
        with pytest.raises(error):
            InBandDTMFDelivery(**kwargs)

    def test_clone_copies_settings_but_not_the_binding(self) -> None:
        tones = DTMFToneConfig(tone_ms=60)
        original = _bound(
            _FakeMediaOutput(outcome=None),
            tone_config=tones,
            inter_digit_delay_ms=200,
            wait_for_playback=True,
            ack_timeout_margin_s=2.5,
        )

        copy = original.clone()

        assert copy is not original
        assert original.bound is True
        assert copy.bound is False
        assert copy._tone_config is tones
        assert copy._inter_digit_delay_ms == 200
        assert copy._wait_for_playback is True
        assert copy._ack_timeout_margin_s == 2.5

    async def test_clone_keeps_a_subclass_and_its_overrides(self) -> None:
        class _StarRejectingDelivery(InBandDTMFDelivery):
            def __init__(self, *, blocked: str, **kwargs: Any) -> None:
                super().__init__(**kwargs)
                self.blocked = blocked

            async def send_dtmf_with_retry(
                self, digits: str, *, should_continue: Any = None
            ) -> bool:
                if self.blocked in digits:
                    return False
                return await super().send_dtmf_with_retry(digits, should_continue=should_continue)

        template = _StarRejectingDelivery(blocked="*", inter_digit_delay_ms=200)
        template.bind_output(_FakeMediaOutput())  # type: ignore[arg-type]

        copy = template.clone()

        assert type(copy) is _StarRejectingDelivery
        assert copy.blocked == "*" and copy._inter_digit_delay_ms == 200
        assert copy.bound is False and template.bound is True
        output = _FakeMediaOutput()
        copy.bind_output(output)  # type: ignore[arg-type]
        assert await copy.send_dtmf_with_retry("*1") is False
        assert await copy.send_dtmf_with_retry("1") is True
        assert [digits for digits, _ in output.tone_calls] == ["1"]

    async def test_accepted_tones_return_true_without_waiting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        waits: list[float | None] = []

        async def spy_wait(self: DTMFTonePlayback, timeout: float | None = None) -> str:
            waits.append(timeout)
            return "played"

        monkeypatch.setattr(DTMFTonePlayback, "wait", spy_wait)
        tones = DTMFToneConfig(tone_ms=60)
        output = _FakeMediaOutput(outcome=None)
        delivery = _bound(output, tone_config=tones, inter_digit_delay_ms=200)

        assert await delivery.send_dtmf("1#") is True
        assert output.tone_calls == [
            ("1#", {"inter_digit_delay_ms": 200, "tone_config": tones, "should_continue": None}),
        ]
        assert waits == [], "the default sender must not wait for playback"

    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [("played", True), ("estimated", True), ("timeout", True), ("disconnected", False)],
    )
    async def test_waiting_maps_playback_outcomes(self, outcome: str, expected: bool) -> None:
        delivery = _bound(_FakeMediaOutput(outcome), wait_for_playback=True)

        assert await delivery.send_dtmf("2") is expected

    async def test_unconfirmed_wait_counts_as_sent(self) -> None:
        delivery = _bound(
            _FakeMediaOutput(outcome=None), wait_for_playback=True, ack_timeout_margin_s=0.01
        )

        assert await delivery.send_dtmf("3") is True

    async def test_output_errors_return_false_without_logging_digits(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        delivery = _bound(
            _FakeMediaOutput(error=RuntimeError("transport rejected DTMF audio for 4321"))
        )

        with caplog.at_level(logging.WARNING, logger="easycat.telephony.ivr"):
            assert await delivery.send_dtmf("4321") is False

        assert any("RuntimeError" in message for message in caplog.messages)
        assert not any("4321" in message for message in caplog.messages)
        assert "4321" not in caplog.text

    @pytest.mark.parametrize("digits", ['1"/><Say>hacked</Say>', "", "a", "١٢"])
    async def test_invalid_digits_never_reach_the_output(self, digits: str) -> None:
        output = _FakeMediaOutput()
        delivery = _bound(output)

        assert await delivery.send_dtmf(digits) is False
        assert output.tone_calls == []

    async def test_unbound_delivery_returns_false(self) -> None:
        delivery = InBandDTMFDelivery()

        assert delivery.bound is False
        assert await delivery.send_dtmf("1") is False
        assert await delivery.send_speech("one") is False

    async def test_send_speech_uses_session_tts(self) -> None:
        output = _FakeMediaOutput()
        delivery = _bound(output)

        assert await delivery.send_speech("sales") is True
        assert output.speech == ["sales"]

        output.speech_error = RuntimeError("tts down")
        assert await delivery.send_speech("support") is False

    async def test_unbind_stops_delivery(self) -> None:
        output = _FakeMediaOutput()
        delivery = _bound(output)
        delivery.bind_output(None)

        assert await delivery.send_dtmf("1") is False
        assert output.tone_calls == []

    async def test_collected_session_returns_false(self) -> None:
        class _Session:
            async def play_dtmf_tones(self, digits: str, **kwargs: Any) -> DTMFTonePlayback:
                raise AssertionError("collected session must not be reached")

        session = _Session()
        output = SessionMediaOutput(session)  # type: ignore[arg-type]
        delivery = InBandDTMFDelivery()
        delivery.bind_output(output)
        ref = weakref.ref(session)
        del session
        gc.collect()

        assert ref() is None
        assert await delivery.send_dtmf("1") is False
        assert await delivery.send_speech("hello") is False

    @pytest.mark.parametrize(
        "output",
        [
            # The stream ended while the accepted tones were playing.
            _FakeMediaOutput("disconnected"),
            # A later tone frame was rejected after earlier frames were queued.
            _FakeMediaOutput(error=RuntimeError("transport rejected DTMF audio")),
        ],
        ids=["disconnected", "partial-send"],
    )
    async def test_failed_send_is_not_retried_then_speaks(self, output: _FakeMediaOutput) -> None:
        """A failed in-band send may have played a prefix; never resend it."""
        bus = EventBus()
        actions: list[IVRAction] = []
        bus.subscribe(IVRAction, actions.append)
        delivery = _bound(output, wait_for_playback=True)

        async def agent(ctx: dict) -> dict:
            return {"action": "dtmf", "digits": "7"}

        nav = IVRNavigator(bus, agent_callback=agent, dtmf_delivery=delivery)
        nav.start()
        nav.activate()
        try:
            await bus.emit(STTFinal(text="Press 7 for billing"))
        finally:
            nav.stop()

        assert [digits for digits, _ in output.tone_calls] == ["7"]
        speak = [a for a in actions if a.type == IVRActionType.SPEAK]
        assert len(speak) == 1 and speak[0].text == "7"

    async def test_navigator_advances_through_two_menus_in_band(self) -> None:
        bus = EventBus()
        output = _FakeMediaOutput(outcome=None)
        delivery = _bound(output)
        answers = iter(["1", "3"])

        async def agent(ctx: dict) -> dict:
            return {"action": "dtmf", "digits": next(answers)}

        nav = IVRNavigator(bus, agent_callback=agent, dtmf_delivery=delivery)
        nav.start()
        nav.activate()
        try:
            await bus.emit(STTFinal(text="Press 1 for sales"))
            await bus.emit(STTFinal(text="Press 3 for orders"))
        finally:
            nav.stop()

        assert nav.menu_depth == 2
        assert [digits for digits, _ in output.tone_calls] == ["1", "3"]
