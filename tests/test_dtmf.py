"""Tests for DTMF parsing and aggregation (Tasks 6.1, 6.3)."""

from __future__ import annotations

import asyncio
import json

from easycat.events import DTMF, DTMFAggregated, EventBus
from easycat.telephony.dtmf import (
    DTMFAggregator,
    DTMFAggregatorConfig,
    emit_twilio_dtmf,
    parse_twilio_dtmf_message,
)

# ── Task 6.1: Twilio Media Streams DTMF parsing ──────────────────


class TestParseTwilioDtmfMessage:
    """Tests for parse_twilio_dtmf_message."""

    def test_valid_dtmf_digit(self) -> None:
        msg = json.dumps(
            {
                "event": "dtmf",
                "streamSid": "MZ123",
                "dtmf": {"digit": "5", "track": "inbound"},
            }
        )
        result = parse_twilio_dtmf_message(msg)
        assert result is not None
        assert result.digit == "5"

    def test_all_valid_digits(self) -> None:
        for digit in "0123456789*#ABCD":
            msg = {"event": "dtmf", "dtmf": {"digit": digit}}
            result = parse_twilio_dtmf_message(msg)
            assert result is not None
            assert result.digit == digit

    def test_lowercase_digit_normalized(self) -> None:
        msg = {"event": "dtmf", "dtmf": {"digit": "a"}}
        result = parse_twilio_dtmf_message(msg)
        assert result is not None
        assert result.digit == "A"

    def test_non_dtmf_event_returns_none(self) -> None:
        msg = {"event": "media", "media": {"payload": "base64..."}}
        assert parse_twilio_dtmf_message(msg) is None

    def test_missing_dtmf_payload_returns_none(self) -> None:
        msg = {"event": "dtmf"}
        assert parse_twilio_dtmf_message(msg) is None

    def test_invalid_digit_returns_none(self) -> None:
        msg = {"event": "dtmf", "dtmf": {"digit": "X"}}
        assert parse_twilio_dtmf_message(msg) is None

    def test_empty_digit_returns_none(self) -> None:
        msg = {"event": "dtmf", "dtmf": {"digit": ""}}
        assert parse_twilio_dtmf_message(msg) is None

    def test_multi_char_digit_returns_none(self) -> None:
        msg = {"event": "dtmf", "dtmf": {"digit": "12"}}
        assert parse_twilio_dtmf_message(msg) is None

    def test_invalid_json_returns_none(self) -> None:
        assert parse_twilio_dtmf_message("not json") is None

    def test_non_dict_returns_none(self) -> None:
        assert parse_twilio_dtmf_message("[1, 2, 3]") is None

    def test_dict_input(self) -> None:
        msg = {"event": "dtmf", "dtmf": {"digit": "9"}}
        result = parse_twilio_dtmf_message(msg)
        assert result is not None
        assert result.digit == "9"

    def test_dtmf_payload_not_dict_returns_none(self) -> None:
        msg = {"event": "dtmf", "dtmf": "5"}
        assert parse_twilio_dtmf_message(msg) is None


class TestEmitTwilioDtmf:
    """Tests for emit_twilio_dtmf convenience function."""

    async def test_emits_valid_dtmf(self) -> None:
        bus = EventBus()
        received: list[DTMF] = []
        bus.subscribe(DTMF, lambda e: received.append(e))

        msg = {"event": "dtmf", "dtmf": {"digit": "7"}}
        result = await emit_twilio_dtmf(msg, bus)
        assert result is not None
        assert result.digit == "7"
        assert len(received) == 1
        assert received[0].digit == "7"

    async def test_skips_non_dtmf(self) -> None:
        bus = EventBus()
        received: list[DTMF] = []
        bus.subscribe(DTMF, lambda e: received.append(e))

        msg = {"event": "media"}
        result = await emit_twilio_dtmf(msg, bus)
        assert result is None
        assert len(received) == 0


# ── Task 6.3: DTMF Aggregator ────────────────────────────────────


class TestDTMFAggregator:
    """Tests for DTMFAggregator."""

    async def test_timeout_triggers_aggregation(self) -> None:
        bus = EventBus()
        aggregated: list[DTMFAggregated] = []
        bus.subscribe(DTMFAggregated, lambda e: aggregated.append(e))

        config = DTMFAggregatorConfig(timeout_ms=100)
        agg = DTMFAggregator(bus, config)
        agg.start()

        try:
            await bus.emit(DTMF(digit="1"))
            await bus.emit(DTMF(digit="2"))
            await bus.emit(DTMF(digit="3"))

            # Buffer should have digits
            assert agg.buffer == "123"

            # Wait for timeout to fire
            await asyncio.sleep(0.2)

            assert len(aggregated) == 1
            assert aggregated[0].sequence == "123"
            assert agg.buffer == ""
        finally:
            agg.stop()

    async def test_terminator_triggers_immediate_emit(self) -> None:
        bus = EventBus()
        aggregated: list[DTMFAggregated] = []
        bus.subscribe(DTMFAggregated, lambda e: aggregated.append(e))

        config = DTMFAggregatorConfig(terminators=frozenset({"#"}), timeout_ms=5000)
        agg = DTMFAggregator(bus, config)
        agg.start()

        try:
            await bus.emit(DTMF(digit="1"))
            await bus.emit(DTMF(digit="2"))
            await bus.emit(DTMF(digit="#"))

            # Should have emitted immediately (no need to wait for timeout)
            assert len(aggregated) == 1
            assert aggregated[0].sequence == "12#"
        finally:
            agg.stop()

    async def test_max_length_triggers_emit(self) -> None:
        bus = EventBus()
        aggregated: list[DTMFAggregated] = []
        bus.subscribe(DTMFAggregated, lambda e: aggregated.append(e))

        config = DTMFAggregatorConfig(max_length=4, timeout_ms=5000)
        agg = DTMFAggregator(bus, config)
        agg.start()

        try:
            for digit in "1234":
                await bus.emit(DTMF(digit=digit))

            assert len(aggregated) == 1
            assert aggregated[0].sequence == "1234"
        finally:
            agg.stop()

    async def test_no_digits_no_event(self) -> None:
        bus = EventBus()
        aggregated: list[DTMFAggregated] = []
        bus.subscribe(DTMFAggregated, lambda e: aggregated.append(e))

        config = DTMFAggregatorConfig(timeout_ms=50)
        agg = DTMFAggregator(bus, config)
        agg.start()

        try:
            await asyncio.sleep(0.1)
            assert len(aggregated) == 0
        finally:
            agg.stop()

    async def test_star_terminator(self) -> None:
        bus = EventBus()
        aggregated: list[DTMFAggregated] = []
        bus.subscribe(DTMFAggregated, lambda e: aggregated.append(e))

        config = DTMFAggregatorConfig(terminators=frozenset({"*", "#"}))
        agg = DTMFAggregator(bus, config)
        agg.start()

        try:
            await bus.emit(DTMF(digit="5"))
            await bus.emit(DTMF(digit="*"))

            assert len(aggregated) == 1
            assert aggregated[0].sequence == "5*"
        finally:
            agg.stop()

    async def test_resets_after_emit(self) -> None:
        bus = EventBus()
        aggregated: list[DTMFAggregated] = []
        bus.subscribe(DTMFAggregated, lambda e: aggregated.append(e))

        config = DTMFAggregatorConfig(terminators=frozenset({"#"}), timeout_ms=5000)
        agg = DTMFAggregator(bus, config)
        agg.start()

        try:
            # First sequence
            await bus.emit(DTMF(digit="1"))
            await bus.emit(DTMF(digit="#"))

            # Second sequence
            await bus.emit(DTMF(digit="2"))
            await bus.emit(DTMF(digit="3"))
            await bus.emit(DTMF(digit="#"))

            assert len(aggregated) == 2
            assert aggregated[0].sequence == "1#"
            assert aggregated[1].sequence == "23#"
        finally:
            agg.stop()

    async def test_stop_cancels_timer(self) -> None:
        bus = EventBus()
        aggregated: list[DTMFAggregated] = []
        bus.subscribe(DTMFAggregated, lambda e: aggregated.append(e))

        config = DTMFAggregatorConfig(timeout_ms=100)
        agg = DTMFAggregator(bus, config)
        agg.start()

        await bus.emit(DTMF(digit="1"))
        agg.stop()

        await asyncio.sleep(0.2)
        # Timer was cancelled, so no aggregated event
        assert len(aggregated) == 0

    async def test_stop_clears_buffer(self) -> None:
        bus = EventBus()
        config = DTMFAggregatorConfig(timeout_ms=5000)
        agg = DTMFAggregator(bus, config)
        agg.start()

        await bus.emit(DTMF(digit="1"))
        assert agg.buffer == "1"

        agg.stop()
        assert agg.buffer == ""
