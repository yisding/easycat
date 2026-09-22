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
        assert e.children == ()

    def test_with_values(self):
        e = ErrorInfo(type="ValueError", message="bad input", traceback="line 1\nline 2")
        assert e.type == "ValueError"
        assert e.traceback is not None

    def test_from_exception_captures_pep678_notes(self):
        exc = RuntimeError("provider failed")
        exc.add_note("provider=openai")
        exc.add_note("stage=tts")

        info = ErrorInfo.from_exception(exc)

        assert info.type == "RuntimeError"
        assert info.message == "provider failed"
        assert info.notes == "provider=openai\nstage=tts"

    def test_from_exception_merges_explicit_and_pep678_notes(self):
        exc = ValueError("bad input")
        exc.add_note("provider=deepgram")

        info = ErrorInfo.from_exception(exc, notes="turn_id=turn-123")

        assert info.notes == "turn_id=turn-123\nprovider=deepgram"

    def test_from_exception_captures_exception_group_child_notes(self):
        left = ValueError("bad input")
        left.add_note("stage=stt")
        right = RuntimeError("provider failed")
        right.add_note("provider=openai")
        group = ExceptionGroup("pipeline failed", [left, right])
        group.add_note("turn_id=turn-123")

        info = ErrorInfo.from_exception(group)

        assert info.type == "ExceptionGroup"
        assert info.notes == "turn_id=turn-123\nstage=stt\nprovider=openai"
        assert [child.type for child in info.children] == ["ValueError", "RuntimeError"]
        assert info.children[0].message == "bad input"
        assert info.children[0].notes == "stage=stt"
        assert info.children[1].message == "provider failed"
        assert info.children[1].notes == "provider=openai"

    def test_from_exception_collapses_consecutive_third_party_frames(self, monkeypatch):
        # A traceback line only contains "site-packages" on the ``File ...``
        # line of a frame; the source line (and any caret-annotation line)
        # that traceback.format_exception prints beneath it does not. Two
        # consecutive third-party frames should still collapse to a single
        # "...N third-party frame(s)..." marker, with none of the
        # third-party source lines leaking into the journal record.
        fake_traceback = [
            "Traceback (most recent call last):\n",
            '  File "/x/site-packages/httpx/_client.py", line 10, in get\n',
            "    resp = self._pool.handle_request(req)\n",
            "           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n",
            '  File "/x/site-packages/httpcore/_sync.py", line 20, in handle_request\n',
            "    raise exc from None\n",
            "RuntimeError: boom\n",
        ]
        monkeypatch.setattr("traceback.format_exception", lambda *args, **kwargs: fake_traceback)

        info = ErrorInfo.from_exception(RuntimeError("boom"))

        assert info.traceback is not None
        assert info.traceback.count("third-party frame(s)") == 1
        assert "...2 third-party frame(s)...\n" in info.traceback
        assert "handle_request(req)" not in info.traceback
        assert "raise exc from None" not in info.traceback
        assert "^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^" not in info.traceback

    def test_from_exception_keeps_first_party_frames_between_collapsed_runs(self, monkeypatch):
        # Collapsing must stay surgical: only frames whose ``File ...`` header
        # points into site-packages are folded away. Application and stdlib
        # frames — and their source lines — have to survive between the runs,
        # otherwise the marker hides the very frames a reader needs.
        fake_traceback = [
            "Traceback (most recent call last):\n",
            '  File "/app/service.py", line 3, in run\n',
            "    return client.fetch()\n",
            '  File "/x/site-packages/httpx/_client.py", line 10, in fetch\n',
            "    resp = self._pool.handle_request(req)\n",
            '  File "/usr/lib/python3.11/contextlib.py", line 155, in __exit__\n',
            "    self.gen.throw(value)\n",
            '  File "/x/site-packages/httpcore/_sync.py", line 20, in handle_request\n',
            "    raise exc from None\n",
            "RuntimeError: boom\n",
        ]
        monkeypatch.setattr("traceback.format_exception", lambda *args, **kwargs: fake_traceback)

        info = ErrorInfo.from_exception(RuntimeError("boom"))

        assert info.traceback is not None
        assert info.traceback.count("...1 third-party frame(s)...\n") == 2
        assert '  File "/app/service.py", line 3, in run\n' in info.traceback
        assert "    return client.fetch()\n" in info.traceback
        assert '  File "/usr/lib/python3.11/contextlib.py", line 155, in __exit__\n' in (
            info.traceback
        )
        assert "    self.gen.throw(value)\n" in info.traceback
        assert "handle_request(req)" not in info.traceback
        assert "raise exc from None" not in info.traceback

    def test_from_exception_collapse_keeps_chained_exception_separators(self, monkeypatch):
        # Chained tracebacks separate the sections with a blank line and a
        # prose line at zero indent. Neither may be mistaken for a continuation
        # of the third-party frame above it, or the marker lands in the wrong
        # section and the separator disappears.
        fake_traceback = [
            "Traceback (most recent call last):\n",
            '  File "/x/site-packages/httpcore/_sync.py", line 20, in handle_request\n',
            "    raise exc from None\n",
            "ConnectError: connection refused\n",
            "\n",
            "The above exception was the direct cause of the following exception:\n",
            "\n",
            "Traceback (most recent call last):\n",
            '  File "/app/service.py", line 3, in run\n',
            "    return client.fetch()\n",
            "RuntimeError: boom\n",
        ]
        monkeypatch.setattr("traceback.format_exception", lambda *args, **kwargs: fake_traceback)

        info = ErrorInfo.from_exception(RuntimeError("boom"))

        assert info.traceback is not None
        assert info.traceback.count("...1 third-party frame(s)...\n") == 1
        assert (
            "  ...1 third-party frame(s)...\nConnectError: connection refused\n" in info.traceback
        )
        assert (
            "\nThe above exception was the direct cause of the following exception:\n\n"
            in info.traceback
        )
        assert "raise exc from None" not in info.traceback
        assert "    return client.fetch()\n" in info.traceback

    def test_from_exception_collapse_preserves_exception_group_structure(self, monkeypatch):
        # BaseExceptionGroup tracebacks prefix every line with a "| " gutter and
        # delimit each sub-exception with a "+---" separator. The gutter has to
        # be stripped before frames are compared, and the indented terminal
        # message line of a child must not be swallowed as frame source text.
        fake_traceback = [
            "  + Exception Group Traceback (most recent call last):\n",
            '  |   File "/app/main.py", line 5, in <module>\n',
            "  |     raise ExceptionGroup('pipeline failed', [RuntimeError('boom')])\n",
            "  | ExceptionGroup: pipeline failed (1 sub-exception)\n",
            "  +-+---------------- 1 ----------------\n",
            "    | Traceback (most recent call last):\n",
            '    |   File "/x/site-packages/httpx/_client.py", line 10, in get\n',
            "    |     resp = self._pool.handle_request(req)\n",
            '    |   File "/x/site-packages/httpcore/_sync.py", line 20, in handle_request\n',
            "    |     raise exc from None\n",
            "    | RuntimeError: boom\n",
            "    +------------------------------------\n",
        ]
        monkeypatch.setattr("traceback.format_exception", lambda *args, **kwargs: fake_traceback)

        info = ErrorInfo.from_exception(RuntimeError("boom"))

        assert info.traceback is not None
        assert info.traceback.count("third-party frame(s)") == 1
        assert "    |   ...2 third-party frame(s)...\n" in info.traceback
        assert "handle_request(req)" not in info.traceback
        assert "raise exc from None" not in info.traceback
        # Group scaffolding and the child's own message survive the collapse.
        assert '  |   File "/app/main.py", line 5, in <module>\n' in info.traceback
        assert "  | ExceptionGroup: pipeline failed (1 sub-exception)\n" in info.traceback
        assert "  +-+---------------- 1 ----------------\n" in info.traceback
        assert "    | RuntimeError: boom\n" in info.traceback
        assert "    +------------------------------------\n" in info.traceback

    def test_from_exception_captures_nested_exception_group_children(self):
        inner_left = ValueError("bad input")
        inner_right = RuntimeError("provider failed")
        nested = ExceptionGroup("nested failed", [inner_left, inner_right])
        top = ExceptionGroup("pipeline failed", [TimeoutError("turn timed out"), nested])

        info = ErrorInfo.from_exception(top)

        assert info.type == "ExceptionGroup"
        assert [child.type for child in info.children] == ["TimeoutError", "ExceptionGroup"]
        assert info.children[1].message == "nested failed (2 sub-exceptions)"
        assert [child.type for child in info.children[1].children] == [
            "ValueError",
            "RuntimeError",
        ]


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
        # These strings are the on-disk/JSON journal wire format. External
        # consumers and historical bundles key on them, so any rename,
        # removal, or addition must be a deliberate change to this set.
        assert {k.value for k in JournalRecordKind} == {
            "event",
            "span_start",
            "span_end",
            "metric",
            "control",
            "framework_transition",
            "degraded",
            "recovery",
        }


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
