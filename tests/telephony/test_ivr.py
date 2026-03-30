"""Tests for IVR navigator."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from easycat.events import EventBus, STTFinal
from easycat.telephony.ivr import (
    DTMFDelivery,
    IVRAction,
    IVRActionType,
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
        assert nav._active is False
        nav.activate()
        assert nav._active is True
        nav.deactivate()
        assert nav._active is False


class TestIVRAgentDecision:
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
    async def test_agent_timeout_retries_prompt(self) -> None:
        bus = EventBus()
        actions: list[IVRAction] = []
        bus.subscribe(IVRAction, actions.append)
        call_count = 0

        async def slow_then_fast_agent(ctx: dict) -> dict:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                await asyncio.sleep(10)  # Will be interrupted by timeout.
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
