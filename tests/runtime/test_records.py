"""Tests for runtime record types."""

from __future__ import annotations

import dataclasses

import pytest

from easycat.runtime.records import (
    BufferOverflow,
    ControlSignalRecord,
    ErrorInfo,
    FrameworkTransitionRecord,
    JournalDegraded,
    JournalRecord,
    JournalRecordKind,
    RecoveredSessionMarker,
    TimingInfo,
)


class TestJournalRecord:
    def test_frozen(self):
        rec = JournalRecord(sequence=1, session_id="s1")
        with pytest.raises(dataclasses.FrozenInstanceError):
            rec.sequence = 2  # type: ignore[misc]

    def test_defaults(self):
        rec = JournalRecord(sequence=1, session_id="s1")
        assert rec.kind == JournalRecordKind.EVENT
        assert rec.name == ""
        assert rec.turn_id is None
        assert rec.data == {}
        assert rec.error is None
        assert rec.tags == frozenset()

    def test_with_data(self):
        rec = JournalRecord(
            sequence=5,
            session_id="s1",
            kind=JournalRecordKind.METRIC,
            name="stt_latency_ms",
            data={"value_ms": 42.0},
            tags=frozenset({"latency"}),
        )
        assert rec.kind == JournalRecordKind.METRIC
        assert rec.data["value_ms"] == 42.0
        assert "latency" in rec.tags


class TestTimingInfo:
    def test_defaults(self):
        t = TimingInfo()
        assert t.wall_ns == 0
        assert t.mono_ns == 0

    def test_frozen(self):
        t = TimingInfo(wall_ns=1, mono_ns=2)
        with pytest.raises(dataclasses.FrozenInstanceError):
            t.wall_ns = 99  # type: ignore[misc]


class TestErrorInfo:
    def test_defaults(self):
        e = ErrorInfo()
        assert e.type == ""
        assert e.message == ""
        assert e.traceback is None

    def test_with_values(self):
        e = ErrorInfo(type="ValueError", message="bad input", traceback="line 1\nline 2")
        assert e.type == "ValueError"
        assert e.traceback is not None


class TestSentinelRecords:
    def test_buffer_overflow_defaults(self):
        rec = BufferOverflow(sequence=1, session_id="s1")
        assert rec.kind == JournalRecordKind.CONTROL
        assert rec.name == "buffer_overflow"

    def test_journal_degraded_defaults(self):
        rec = JournalDegraded(sequence=1, session_id="s1")
        assert rec.kind == JournalRecordKind.DEGRADED
        assert rec.name == "journal_degraded"


class TestJournalRecordKind:
    def test_all_kinds(self):
        expected = {
            "event",
            "span_start",
            "span_end",
            "metric",
            "control",
            "framework_transition",
            "degraded",
            "recovery",
        }
        actual = {k.value for k in JournalRecordKind}
        assert actual == expected


class TestSubclassDefaultsRule:
    """All JournalRecord subclass fields (except sequence and session_id) must have defaults."""

    def test_all_subclass_fields_have_defaults(self):
        required_no_default = {"sequence", "session_id"}
        for cls in [
            JournalRecord,
            FrameworkTransitionRecord,
            ControlSignalRecord,
            RecoveredSessionMarker,
            BufferOverflow,
            JournalDegraded,
        ]:
            for f in dataclasses.fields(cls):
                if f.name in required_no_default:
                    continue
                has_default = (
                    f.default is not dataclasses.MISSING
                    or f.default_factory is not dataclasses.MISSING
                )
                assert has_default, f"{cls.__name__}.{f.name} must have a default value"
