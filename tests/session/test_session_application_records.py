from __future__ import annotations

import pytest

from easycat._turn_context import TurnContext
from easycat.cancel import CancelToken
from easycat.runtime import InMemoryRingBuffer, SqliteJournal
from easycat.runtime.journal import ExecutionJournal
from easycat.runtime.records import JournalRecordKind
from easycat.session._session import Session
from easycat.session._types import SessionConfig
from easycat.validation.redaction import REDACTED_SECRET


def _session_with_journal() -> tuple[Session, InMemoryRingBuffer]:
    journal = InMemoryRingBuffer()
    session = Session(SessionConfig(runtime_mode="text_session", journal=journal))
    return session, journal


def _session_with_backend(backend: str, tmp_path) -> tuple[Session, ExecutionJournal]:
    session_id = f"app-record-{backend}"
    journal = (
        InMemoryRingBuffer()
        if backend == "memory"
        else SqliteJournal(session_id, data_dir=tmp_path)
    )
    session = Session(
        SessionConfig(
            runtime_mode="text_session",
            journal=journal,
            session_id=session_id,
        )
    )
    return session, journal


def test_record_appends_namespaced_event_with_turn_and_tags() -> None:
    session, journal = _session_with_journal()

    session.record(
        "app.call_metadata",
        data={"arrival_channel": "forwarded"},
        turn_id="turn-app-1",
        tags=frozenset({"tenant:a", "channel:phone"}),
    )

    [record] = journal.read()
    assert record.name == "app.call_metadata"
    assert record.kind is JournalRecordKind.EVENT
    assert record.session_id == session.session_id
    assert record.turn_id == "turn-app-1"
    assert record.data == {"arrival_channel": "forwarded"}
    assert record.tags == frozenset({"tenant:a", "channel:phone"})


def test_record_uses_the_standard_journal_write_filter() -> None:
    session, journal = _session_with_journal()

    session.record("app.credentials_seen", data={"api_key": "not-for-storage"})

    [record] = journal.read()
    assert record.data == {"api_key": REDACTED_SECRET}


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_record_snapshots_nested_data_at_write_time(backend: str, tmp_path) -> None:
    session, journal = _session_with_backend(backend, tmp_path)
    data = {"nested": {"items": [{"value": "before"}]}}
    try:
        session.record("app.snapshot", data=data)
        data["nested"]["items"][0]["value"] = "after"
        data["nested"]["items"].append({"value": "later"})

        [record] = journal.read()
        assert record.data == {"nested": {"items": [{"value": "before"}]}}
    finally:
        journal.close()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_record_rejects_comma_delimited_tags_across_backends(backend: str, tmp_path) -> None:
    session, journal = _session_with_backend(backend, tmp_path)
    try:
        with pytest.raises(ValueError, match="must not contain commas"):
            session.record(
                "app.invalid_tag",
                data={},
                tags=frozenset({"tenant,a"}),
            )

        assert journal.read() == []
    finally:
        journal.close()


def test_record_inherits_the_active_turn_when_turn_id_is_omitted() -> None:
    session, journal = _session_with_journal()
    session._turn = TurnContext("turn-current", CancelToken())

    session.record("app.turn_fact", data={"intent": "schedule"})

    [record] = journal.read()
    assert record.turn_id == "turn-current"


def test_record_explicit_none_does_not_inherit_active_turn() -> None:
    session, journal = _session_with_journal()
    session._turn = TurnContext("turn-current", CancelToken())

    session.record("app.session_fact", data={}, turn_id=None)

    assert journal.read()[0].turn_id is None


@pytest.mark.parametrize("turn_id", ["", " ", 123])
def test_record_rejects_invalid_turn_id(turn_id) -> None:
    session, journal = _session_with_journal()

    with pytest.raises(ValueError, match="turn_id"):
        session.record("app.invalid_turn", data={}, turn_id=turn_id)

    assert journal.read() == []


@pytest.mark.parametrize("tags", [{"tenant:a"}, ["tenant:a"], ("tenant:a",)])
def test_record_canonicalizes_tag_iterables(tags) -> None:
    session, journal = _session_with_journal()

    session.record("app.tags", data={}, tags=tags)

    assert journal.read()[0].tags == frozenset({"tenant:a"})


@pytest.mark.parametrize("tags", ["tenant:a", None, [""]])
def test_record_rejects_invalid_tag_collections(tags) -> None:
    session, journal = _session_with_journal()

    with pytest.raises(ValueError, match="tags"):
        session.record("app.tags", data={}, tags=tags)

    assert journal.read() == []


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {"bytes": b"abc"},
        {"set": {"a"}},
        {"nan": float("nan")},
        {1: "non-string-key"},
    ],
)
def test_record_rejects_non_json_payloads(data) -> None:
    session, journal = _session_with_journal()

    with pytest.raises(ValueError, match="Application journal record"):
        session.record("app.invalid_data", data=data)

    assert journal.read() == []
    assert not journal.degraded


def test_record_rejects_cycles_without_degrading_journal() -> None:
    session, journal = _session_with_journal()
    data: dict[str, object] = {}
    data["self"] = data

    with pytest.raises(ValueError, match="cycle"):
        session.record("app.cycle", data=data)

    session.record("app.after_cycle", data={"ok": True})
    assert [record.name for record in journal.read()] == ["app.after_cycle"]
    assert not journal.degraded


def test_mutating_read_result_does_not_rewrite_memory_journal() -> None:
    session, journal = _session_with_journal()
    session.record("app.immutable_read", data={"nested": {"value": "before"}})

    first = journal.read()[0]
    first.data["nested"]["value"] = "after"

    assert journal.read()[0].data == {"nested": {"value": "before"}}


@pytest.mark.parametrize("name", ["stt_final", "call_ended"])
def test_record_rejects_builtin_names(name: str) -> None:
    session, journal = _session_with_journal()

    with pytest.raises(ValueError, match="reserved by EasyCat"):
        session.record(name, data={})

    assert journal.read() == []


@pytest.mark.parametrize("name", ["call_metadata", "application.call_metadata", "app.", "app. "])
def test_record_requires_nonempty_app_namespace(name: str) -> None:
    session, journal = _session_with_journal()

    with pytest.raises(ValueError, match=r"app\.<name>"):
        session.record(name, data={})

    assert journal.read() == []


@pytest.mark.asyncio
async def test_record_rejects_writes_after_stop() -> None:
    session, journal = _session_with_journal()
    session.record("app.before_stop", data={"phase": "live"})
    await session.stop()

    with pytest.raises(RuntimeError, match="stopping or has been stopped"):
        session.record("app.after_stop", data={"phase": "postmortem"})

    assert [record.name for record in journal.read()] == ["app.before_stop"]


def test_record_is_a_noop_when_journaling_is_disabled() -> None:
    session = Session(SessionConfig(runtime_mode="text_session"))

    session.record("app.metadata", data={"value": 1})
