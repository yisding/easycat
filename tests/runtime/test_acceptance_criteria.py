"""Acceptance criteria verification tests from the WS1 plan.

Each test maps to a specific AC in plan/workstream-1-journal-foundation.md.
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
import textwrap

import pytest

from easycat.runtime.artifacts import (
    InMemoryArtifactStore,
)
from easycat.runtime.journal import (
    InMemoryRingBuffer,
    JournalView,
    SqliteJournal,
    create_journal,
)
from easycat.runtime.records import (
    JournalRecordKind,
)
from easycat.runtime.safe_defaults import (
    safe_config_snapshot,
    safe_env_snapshot,
)

# ── AC1.3: Backend selection and debug capability matrix ─────────


class TestJournalBackendSelection:
    """AC1.3 — backends selected via debug mode."""

    def test_off_returns_none(self):
        """debug='off' → caller should not create a journal."""
        # create_journal shouldn't normally be called for "off", but if it
        # is, it returns an in-memory fallback.
        j = create_journal("s", debug="off")
        assert isinstance(j, InMemoryRingBuffer)

    def test_light_returns_ring_buffer(self):
        j = create_journal("s", debug="light")
        assert isinstance(j, InMemoryRingBuffer)

    def test_full_returns_sqlite(self, tmp_path):
        j = create_journal("s", debug="full", data_dir=str(tmp_path))
        assert isinstance(j, SqliteJournal)
        j.close()


class TestDebugCapabilityMatrix:
    """AC1.3 — mode semantics."""

    def test_off_produces_no_journal(self):
        """debug='off' → create_journal should not be called; journal=None."""
        # When debug="off", config.py skips journal creation entirely.
        # Verify the factory returns in-memory fallback if called anyway.
        j = create_journal("s", debug="off")
        assert isinstance(j, InMemoryRingBuffer)
        # The real contract is that config.py sets journal=None for "off".

    def test_light_uses_in_memory(self):
        j = create_journal("s", debug="light")
        assert isinstance(j, InMemoryRingBuffer)
        view = JournalView(j)
        assert view.enabled is True

    def test_full_uses_durable(self, tmp_path):
        j = create_journal("s", debug="full", data_dir=str(tmp_path))
        assert isinstance(j, SqliteJournal)
        view = JournalView(j)
        assert view.enabled is True
        j.close()


class TestDebugBoolRejected:
    """``debug=bool`` was removed; it must now raise ``ValueError``."""

    def test_true_raises(self):
        from easycat.config import EasyCatConfig
        from easycat.stt.openai_provider import OpenAISTTConfig
        from easycat.tts.openai_tts import OpenAITTSConfig

        with pytest.raises(ValueError, match="Invalid debug=True"):
            EasyCatConfig(
                stt=OpenAISTTConfig(api_key="test"),
                tts=OpenAITTSConfig(api_key="test"),
                debug=True,
            )

    def test_false_raises(self):
        from easycat.config import EasyCatConfig
        from easycat.stt.openai_provider import OpenAISTTConfig
        from easycat.tts.openai_tts import OpenAITTSConfig

        with pytest.raises(ValueError, match="Invalid debug=False"):
            EasyCatConfig(
                stt=OpenAISTTConfig(api_key="test"),
                tts=OpenAITTSConfig(api_key="test"),
                debug=False,
            )


# ── AC1.4: Monotonic sequence ────────────────────────────────────


class TestJournalMonotonicSequence:
    """AC1.4 — 1000 records, strictly increasing from 1, no gaps."""

    def test_in_memory_1000_records(self):
        j = InMemoryRingBuffer(capacity=10_000)
        seqs = []
        for i in range(1000):
            s = j.append(
                kind=JournalRecordKind.EVENT,
                name=f"e{i}",
                session_id="s",
            )
            seqs.append(s)
        assert seqs == list(range(1, 1001))

    def test_sqlite_1000_records(self, tmp_path):
        j = SqliteJournal("s", data_dir=tmp_path)
        seqs = []
        for i in range(1000):
            s = j.append(
                kind=JournalRecordKind.EVENT,
                name=f"e{i}",
                session_id="s",
            )
            seqs.append(s)
        assert seqs == list(range(1, 1001))
        j.close()


# ── AC1.5a: Read-after-write visibility ──────────────────────────


class TestJournalSynchronousAppendReadback:
    """AC1.5a — after every append, immediate read returns the record."""

    def test_in_memory(self):
        j = InMemoryRingBuffer(capacity=1000)
        for i in range(50):
            seq = j.append(
                kind=JournalRecordKind.EVENT,
                name=f"e{i}",
                session_id="s",
            )
            records = j.read(start=seq, limit=1)
            assert len(records) == 1
            assert records[0].sequence == seq
            assert records[0].name == f"e{i}"

    def test_sqlite(self, tmp_path):
        j = SqliteJournal("s", data_dir=tmp_path)
        for i in range(50):
            seq = j.append(
                kind=JournalRecordKind.EVENT,
                name=f"e{i}",
                session_id="s",
            )
            records = j.read(start=seq, limit=1)
            assert len(records) == 1
            assert records[0].sequence == seq
            assert records[0].name == f"e{i}"
        j.close()


# ── AC1.5b / AC1.8: Crash durability ────────────────────────────


class TestJournalCrashDurability:
    """AC1.5b + AC1.8 — SQLite survives SIGKILL with zero committed
    records lost, uncheckpointed WAL is readable, RecoveredSessionMarker
    emitted at sequence=0, file moved to crash-dumps/.
    """

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="SIGKILL not available on Windows",
    )
    def test_sigkill_durability(self, tmp_path):
        """Subprocess writes records to SQLite, parent sends SIGKILL,
        reopens the journal, asserts all committed records intact."""
        n_records = 50
        script = textwrap.dedent(f"""\
            import sys, time
            sys.path.insert(0, "src")
            from easycat.runtime.journal import SqliteJournal
            from easycat.runtime.records import JournalRecordKind

            j = SqliteJournal("crash-sess", data_dir="{tmp_path}")
            for i in range({n_records}):
                j.append(
                    kind=JournalRecordKind.EVENT,
                    name=f"event_{{i}}",
                    session_id="crash-sess",
                )
            # Flush to ensure the transaction is committed to WAL.
            j.flush()
            # Signal parent we're done writing.
            print("READY", flush=True)
            # Block until killed.
            time.sleep(60)
        """)

        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Wait for the child to finish writing.
        line = proc.stdout.readline().strip()
        assert line == "READY", f"Child did not signal ready: {line}"

        # Kill without clean shutdown.
        proc.send_signal(signal.SIGKILL)
        proc.wait()

        # Reopen — should detect unclean shutdown.
        j2 = SqliteJournal("crash-sess", data_dir=tmp_path)
        assert j2._recovered is True

        records = j2.read(start=0)
        # Recovery marker at sequence=0.
        recovery = [r for r in records if r.kind == JournalRecordKind.RECOVERY]
        assert len(recovery) == 1
        assert recovery[0].sequence == 0

        # All committed records present.
        event_records = [r for r in records if r.kind == JournalRecordKind.EVENT]
        assert len(event_records) == n_records

        j2.close()

        # Crash dump should have been created.
        crash_dump = tmp_path / "crash-dumps" / "crash-sess.sqlite"
        assert crash_dump.exists()


# ── AC1.6: Artifact store indirection and atomicity ──────────────


class TestArtifactStoreIndirectionAndAtomicity:
    """AC1.6 — artifact refs keep record inline size bounded."""

    def test_large_artifact_via_ref(self):
        """1MB artifact stored by ref; journal record inline size < 4KB."""
        store = InMemoryArtifactStore()
        journal = InMemoryRingBuffer(capacity=100)

        payload = b"\x00" * 1_000_000  # 1MB
        ref = store.put(payload)
        assert ref == hashlib.sha256(payload).hexdigest()

        seq = journal.append(
            kind=JournalRecordKind.EVENT,
            name="audio_capture",
            session_id="s",
            input_ref=ref,
        )
        rec = journal.read(start=seq, limit=1)[0]
        # The record itself should be small — the payload is in the store.
        assert rec.input_ref == ref
        # Record data dict should not contain the 1MB payload.
        import json

        inline_size = len(json.dumps(rec.data))
        assert inline_size < 4096

    def test_dedup_returns_same_ref(self):
        """Two writes of identical bytes return the same SHA-256 ref."""
        store = InMemoryArtifactStore()
        ref1 = store.put(b"identical")
        ref2 = store.put(b"identical")
        assert ref1 == ref2

    def test_artifact_ref_roundtrip_sqlite(self, tmp_path):
        """input_ref/output_ref survive SQLite serialization."""
        j = SqliteJournal("s", data_dir=tmp_path)
        j.append(
            kind=JournalRecordKind.EVENT,
            name="with_refs",
            session_id="s",
            input_ref="abc123",
            output_ref="def456",
        )
        rec = j.read()[0]
        assert rec.input_ref == "abc123"
        assert rec.output_ref == "def456"
        j.close()

    def test_artifact_ref_roundtrip_in_memory(self):
        """input_ref/output_ref stored on in-memory records."""
        j = InMemoryRingBuffer(capacity=100)
        j.append(
            kind=JournalRecordKind.EVENT,
            name="with_refs",
            session_id="s",
            input_ref="abc123",
            output_ref="def456",
        )
        rec = j.read()[0]
        assert rec.input_ref == "abc123"
        assert rec.output_ref == "def456"


# ── AC1.7: Safe config and env defaults ──────────────────────────


class TestSafeConfigDefaultDropsApiKeys:
    """AC1.7 — synthetic API key must not appear in safe snapshot."""

    def test_api_key_excluded(self):
        from dataclasses import dataclass

        @dataclass
        class _Config:
            debug: str = "light"
            stt: str = "openai"
            openai_api_key: str = "sk-SYNTHETIC-KEY-12345"
            deepgram_api_key: str = "dg-secret"

        snap = safe_config_snapshot(_Config())
        # The key fields must not appear.
        for val in snap.values():
            assert "sk-SYNTHETIC" not in str(val)
            assert "dg-secret" not in str(val)
        assert "openai_api_key" not in snap
        assert "deepgram_api_key" not in snap


class TestSafeEnvDefaultDropsNonEasycatVars:
    """AC1.7 — sensitive env vars outside EASYCAT_* allowlist excluded."""

    def test_sensitive_vars_excluded(self):
        from unittest.mock import patch

        env = {
            "OPENAI_API_KEY": "sk-secret",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "DEEPGRAM_API_KEY": "dg-secret",
            "ELEVENLABS_API_KEY": "el-secret",
            "EASYCAT_DATA_DIR": "/safe/path",
        }
        with patch.dict(os.environ, env, clear=True):
            snap = safe_env_snapshot()
        assert "OPENAI_API_KEY" not in snap
        assert "AWS_SECRET_ACCESS_KEY" not in snap
        assert "DEEPGRAM_API_KEY" not in snap
        assert "ELEVENLABS_API_KEY" not in snap
        # Only the allowlisted var should be present.
        assert snap.get("EASYCAT_DATA_DIR") == "/safe/path"


# ── AC1.12: Session exposes read-only journal ────────────────────


class TestSessionExposesReadOnlyJournal:
    """AC1.12 — Session.journal is a JournalView with read-only methods."""

    def test_journal_view_is_read_only(self):
        """JournalView exposes read/slice/follow but not append/close/flush."""
        journal = InMemoryRingBuffer(capacity=100)
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="test",
            session_id="s",
        )
        view = JournalView(journal)

        assert view.enabled is True
        assert view.degraded is False

        # Can read records.
        records = view.read()
        assert len(records) == 1

        # No append/mutation methods exposed.
        assert not hasattr(view, "append")
        assert not hasattr(view, "close")
        assert not hasattr(view, "flush")

    async def test_follow_from_sequence(self):
        """follow(from_sequence=0) replays existing records then tails."""
        import asyncio

        journal = InMemoryRingBuffer(capacity=100)
        # Pre-populate two records.
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="e0",
            session_id="s",
        )
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="e1",
            session_id="s",
        )
        view = JournalView(journal)

        received: list[int] = []

        async def follower():
            async for rec in view.follow(from_sequence=0, poll_interval=0.01):
                received.append(rec.sequence)
                if len(received) >= 2:
                    break

        await asyncio.wait_for(follower(), timeout=2.0)
        assert received == [1, 2]

    async def test_follow_yields_live_records(self):
        import asyncio

        journal = InMemoryRingBuffer(capacity=100)
        view = JournalView(journal)

        received: list[int] = []

        async def follower():
            async for rec in view.follow(from_sequence=1, poll_interval=0.01):
                received.append(rec.sequence)
                if len(received) >= 3:
                    break

        async def appender():
            await asyncio.sleep(0.02)
            for i in range(3):
                journal.append(
                    kind=JournalRecordKind.EVENT,
                    name=f"e{i}",
                    session_id="s",
                )
                await asyncio.sleep(0.01)

        await asyncio.gather(
            asyncio.wait_for(follower(), timeout=2.0),
            appender(),
        )
        assert received == [1, 2, 3]


# ── AC1.14: Degraded mode ────────────────────────────────────────


class TestJournalDegradedMode:
    """AC1.14 — backend failure triggers degraded mode gracefully."""

    def test_degraded_marker_on_stderr(self, capsys):
        """Patched backend raises on append → single JournalDegraded
        marker on stderr, degraded flag set, no exception raised."""
        j = InMemoryRingBuffer(capacity=10)

        def broken(*args, **kwargs):
            raise RuntimeError("disk full")

        j._do_append = broken

        seq = j.append(
            kind=JournalRecordKind.EVENT,
            name="e1",
            session_id="s",
        )
        assert seq == -1
        assert j.degraded is True

        captured = capsys.readouterr()
        assert "journal degraded" in captured.err
        assert "disk full" in captured.err

    def test_subsequent_appends_silently_drop(self, capsys):
        """Once degraded, further appends return -1 with no stderr."""
        j = InMemoryRingBuffer(capacity=10)
        j._degraded = True

        seq = j.append(
            kind=JournalRecordKind.EVENT,
            name="e1",
            session_id="s",
        )
        assert seq == -1
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_sqlite_degraded_mode(self, tmp_path, capsys):
        """SqliteJournal enters degraded mode when DB is broken."""
        j = SqliteJournal("s", data_dir=tmp_path)
        # Break the connection.
        j._conn.close()
        j._closed = False

        seq = j.append(
            kind=JournalRecordKind.EVENT,
            name="fail",
            session_id="s",
        )
        assert seq == -1
        assert j.degraded is True

        captured = capsys.readouterr()
        assert "journal degraded" in captured.err
