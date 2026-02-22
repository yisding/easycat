from __future__ import annotations

import json
import logging

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.event_logging import EventLoggingConfig, EventTraceLogger
from easycat.events import EventBus, Interruption, STTPartial, ToolCallStarted, TTSAudio


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
async def test_event_trace_logger_json_mode_and_ring_buffer(caplog: pytest.LogCaptureFixture):
    bus = EventBus()
    tracer = EventTraceLogger(
        bus, EventLoggingConfig(enabled=True, json_mode=True, ring_buffer_size=2)
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
    """Stopping and restarting EventTraceLogger should clear throttling state."""
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

    tracer.stop()
    caplog.clear()
    tracer.start()

    with caplog.at_level(logging.INFO, logger="easycat.event_trace"):
        await bus.emit(Interruption())

    tracer.stop()

    second_run = [r for r in caplog.records if "Interruption" in r.getMessage()]
    assert len(second_run) == 1
