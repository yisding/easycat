from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.event_logging import EventLoggingConfig, EventTraceLogger
from easycat.events import (
    EventBus,
    Interruption,
    STTPartial,
    ToolCallDelta,
    ToolCallResult,
    ToolCallStarted,
    TTSAudio,
)


def test_event_logging_config_defaults_for_easy_runs() -> None:
    config = EventLoggingConfig()
    assert config.enabled is True
    assert config.include_partials is False
    assert config.auto_configure_root_logger is True


def test_event_trace_logger_auto_configures_root_logger_when_missing() -> None:
    bus = EventBus()
    tracer = EventTraceLogger(bus, EventLoggingConfig(enabled=True))

    with (
        patch("logging.getLogger") as get_logger_mock,
        patch("logging.basicConfig") as basic_config,
    ):
        fake_root = type("Root", (), {"handlers": []})()

        def _get_logger(name: str | None = None):
            if name is None:
                return fake_root
            return logging.Logger(name)

        get_logger_mock.side_effect = _get_logger

        tracer.start()
        tracer.stop()

    basic_config.assert_called_once()


def test_event_trace_logger_does_not_reconfigure_existing_root_logger() -> None:
    bus = EventBus()
    tracer = EventTraceLogger(bus, EventLoggingConfig(enabled=True))

    with (
        patch("logging.getLogger") as get_logger_mock,
        patch("logging.basicConfig") as basic_config,
    ):
        fake_root = type("Root", (), {"handlers": [object()]})()

        def _get_logger(name: str | None = None):
            if name is None:
                return fake_root
            return logging.Logger(name)

        get_logger_mock.side_effect = _get_logger

        tracer.start()
        tracer.stop()

    basic_config.assert_not_called()


@pytest.mark.asyncio
async def test_event_trace_logger_logs_barge_in_and_asr(caplog: pytest.LogCaptureFixture):
    bus = EventBus()
    tracer = EventTraceLogger(bus, EventLoggingConfig(enabled=True, include_partials=True))
    tracer.start()

    with caplog.at_level(logging.INFO, logger="easycat.event_trace"):
        await bus.emit(Interruption(session_id="s1", turn_id="t1"))
        await bus.emit(STTPartial(text="hello there", session_id="s1", turn_id="t1"))

    tracer.stop()

    messages = [record.getMessage() for record in caplog.records]
    assert any("Interruption" in message for message in messages)
    assert any("STTPartial" in message and 'text="hello there"' in message for message in messages)
    assert any('session_id="s1"' in message and 'turn_id="t1"' in message for message in messages)
    assert messages[0].startswith("#0001")
    assert messages[1].startswith("#0002")


@pytest.mark.asyncio
async def test_event_trace_logger_respects_audio_flag(caplog: pytest.LogCaptureFixture):
    bus = EventBus()
    chunk = AudioChunk(data=b"\x00\x00\x01\x01", format=PCM16_MONO_16K)

    disabled = EventTraceLogger(bus, EventLoggingConfig(enabled=True, include_audio_events=False))
    disabled.start()
    with caplog.at_level(logging.INFO, logger="easycat.event_trace"):
        await bus.emit(TTSAudio(chunk=chunk))
    disabled.stop()
    assert not any("TTSAudio" in record.getMessage() for record in caplog.records)

    caplog.clear()

    enabled = EventTraceLogger(bus, EventLoggingConfig(enabled=True, include_audio_events=True))
    enabled.start()
    with caplog.at_level(logging.INFO, logger="easycat.event_trace"):
        await bus.emit(TTSAudio(chunk=chunk))
    enabled.stop()
    assert any("TTSAudio" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_event_trace_logger_can_hide_text(caplog: pytest.LogCaptureFixture):
    bus = EventBus()
    tracer = EventTraceLogger(
        bus,
        EventLoggingConfig(enabled=True, include_partials=True, include_text=False),
    )
    tracer.start()

    with caplog.at_level(logging.INFO, logger="easycat.event_trace"):
        await bus.emit(STTPartial(text="private text"))

    tracer.stop()

    assert any("text_chars=12" in record.getMessage() for record in caplog.records)
    assert not any("private text" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_event_trace_logger_can_hide_tool_text(caplog: pytest.LogCaptureFixture):
    bus = EventBus()
    tracer = EventTraceLogger(
        bus,
        EventLoggingConfig(enabled=True, include_text=False),
    )
    tracer.start()

    with caplog.at_level(logging.INFO, logger="easycat.event_trace"):
        await bus.emit(ToolCallDelta(call_id="c1", delta="secret input"))
        await bus.emit(ToolCallResult(call_id="c1", result="secret output"))

    tracer.stop()

    messages = [record.getMessage() for record in caplog.records]
    assert any("ToolCallDelta" in message and "delta_chars=12" in message for message in messages)
    assert any(
        "ToolCallResult" in message and "result_chars=13" in message for message in messages
    )
    assert not any("secret input" in message for message in messages)
    assert not any("secret output" in message for message in messages)


@pytest.mark.asyncio
async def test_event_trace_logger_json_mode_and_ring_buffer(caplog: pytest.LogCaptureFixture):
    bus = EventBus()
    tracer = EventTraceLogger(
        bus,
        EventLoggingConfig(
            enabled=True, include_partials=True, json_mode=True, ring_buffer_size=2
        ),
    )
    tracer.start()

    with caplog.at_level(logging.INFO, logger="easycat.event_trace"):
        await bus.emit(Interruption(session_id="s1", turn_id="t1"))
        await bus.emit(
            ToolCallStarted(tool_name="lookup", call_id="c1", session_id="s1", turn_id="t1")
        )
        await bus.emit(STTPartial(text="hidden", session_id="s1", turn_id="t1"))

    tracer.stop()

    payload = json.loads(caplog.records[0].getMessage())
    assert payload["event_type"] == "Interruption"
    assert payload["session_id"] == "s1"
    recent = tracer.snapshot_recent_events()
    assert len(recent) == 2
    assert recent[-1]["event_type"] == "STTPartial"


@pytest.mark.asyncio
async def test_event_trace_logger_sampling_and_rate_limit(caplog: pytest.LogCaptureFixture):
    bus = EventBus()
    tracer = EventTraceLogger(
        bus,
        EventLoggingConfig(
            enabled=True,
            include_partials=True,
            sample_rates={"STTPartial": 0.5},
            min_interval_s={"Interruption": 999.0},
        ),
    )
    tracer.start()

    with caplog.at_level(logging.INFO, logger="easycat.event_trace"):
        await bus.emit(STTPartial(text="a"))
        await bus.emit(STTPartial(text="b"))
        await bus.emit(STTPartial(text="c"))
        await bus.emit(STTPartial(text="d"))
        await bus.emit(Interruption())
        await bus.emit(Interruption())

    tracer.stop()

    partial_logs = [r for r in caplog.records if "STTPartial" in r.getMessage()]
    interruption_logs = [r for r in caplog.records if "Interruption" in r.getMessage()]
    assert len(partial_logs) == 2
    assert len(interruption_logs) == 1


@pytest.mark.asyncio
async def test_start_resets_rate_limit_state(caplog: pytest.LogCaptureFixture):
    """Stopping and restarting EventTraceLogger should clear run-local state."""
    bus = EventBus()
    tracer = EventTraceLogger(
        bus,
        EventLoggingConfig(
            enabled=True,
            min_interval_s={"Interruption": 999.0},
        ),
    )
    tracer.start()

    with caplog.at_level(logging.INFO, logger="easycat.event_trace"):
        await bus.emit(Interruption())

    first_run = [r for r in caplog.records if "Interruption" in r.getMessage()]
    assert len(first_run) == 1
    assert len(tracer.snapshot_recent_events()) == 1

    tracer.stop()
    caplog.clear()
    tracer.start()
    assert tracer.snapshot_recent_events() == []

    with caplog.at_level(logging.INFO, logger="easycat.event_trace"):
        await bus.emit(Interruption())

    tracer.stop()

    second_run = [r for r in caplog.records if "Interruption" in r.getMessage()]
    assert len(second_run) == 1
    assert len(tracer.snapshot_recent_events()) == 1
    assert tracer.snapshot_recent_events()[-1]["event_index"] == 1
