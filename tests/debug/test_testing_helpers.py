"""Tests for the bundle-driven pytest helpers in easycat.debug.testing."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from easycat.debug.bundle import FORMAT_VERSION, RunBundle
from easycat.debug.testing import (
    assert_exact_match,
    assert_no_error,
    assert_regex,
    assert_tool_called,
    assert_turn_completed,
    find_record,
    iter_records,
    load_bundle,
    turn_records,
)


def _make_bundle(tmp_path: Path, records: list[dict]) -> Path:
    """Roll a minimal bundle zip around *records*."""
    path = tmp_path / "test.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps({"format_version": FORMAT_VERSION}))
        zf.writestr(
            "journal.ndjson",
            "\n".join(json.dumps(r) for r in records),
        )
    return path


def test_load_bundle_returns_runbundle(tmp_path: Path):
    bundle_path = _make_bundle(
        tmp_path,
        [{"sequence": 1, "name": "turn_started", "turn_id": "t1"}],
    )
    bundle = load_bundle(bundle_path)
    assert isinstance(bundle, RunBundle)


def test_iter_records_filters_by_name(tmp_path: Path):
    records = [
        {"sequence": 1, "name": "turn_started"},
        {"sequence": 2, "name": "stt_final", "data": {"text": "hi"}},
        {"sequence": 3, "name": "stt_final", "data": {"text": "there"}},
        {"sequence": 4, "name": "turn_ended"},
    ]
    bundle = load_bundle(_make_bundle(tmp_path, records))
    stt = list(iter_records(bundle, name="stt_final"))
    assert len(stt) == 2


def test_assert_exact_match_passes(tmp_path: Path):
    records = [
        {"sequence": 1, "name": "agent_final", "data": {"text": "Hello!"}},
    ]
    bundle = load_bundle(_make_bundle(tmp_path, records))
    assert_exact_match(bundle, expected="Hello!")


def test_assert_exact_match_fails(tmp_path: Path):
    records = [
        {"sequence": 1, "name": "agent_final", "data": {"text": "Hello!"}},
    ]
    bundle = load_bundle(_make_bundle(tmp_path, records))
    with pytest.raises(AssertionError, match="text mismatch"):
        assert_exact_match(bundle, expected="Goodbye")


def test_assert_exact_match_reads_top_level_text(tmp_path: Path):
    """Some bundle variants stash the reply text on the record root rather than under ``data``."""
    records = [
        {"sequence": 1, "name": "agent_final", "text": "Hello from root!"},
    ]
    bundle = load_bundle(_make_bundle(tmp_path, records))
    assert_exact_match(bundle, expected="Hello from root!")


def test_assert_regex_matches(tmp_path: Path):
    records = [
        {"sequence": 1, "name": "agent_final", "data": {"text": "The weather is 72F."}},
    ]
    bundle = load_bundle(_make_bundle(tmp_path, records))
    assert_regex(bundle, pattern=r"\d+F")


def test_assert_regex_fails_on_no_match(tmp_path: Path):
    records = [
        {"sequence": 1, "name": "agent_final", "data": {"text": "all clear"}},
    ]
    bundle = load_bundle(_make_bundle(tmp_path, records))
    with pytest.raises(AssertionError, match="did not match"):
        assert_regex(bundle, pattern=r"\d+F")


def test_assert_turn_completed_requires_both_boundaries(tmp_path: Path):
    # turn_started without turn_ended = hang.
    records = [
        {"sequence": 1, "name": "turn_started", "turn_id": "t1"},
    ]
    bundle = load_bundle(_make_bundle(tmp_path, records))
    with pytest.raises(AssertionError, match="never completed"):
        assert_turn_completed(bundle, "t1")


def test_assert_turn_completed_passes(tmp_path: Path):
    records = [
        {"sequence": 1, "name": "turn_started", "turn_id": "t1"},
        {"sequence": 2, "name": "turn_ended", "turn_id": "t1"},
    ]
    bundle = load_bundle(_make_bundle(tmp_path, records))
    assert_turn_completed(bundle, "t1")


def test_assert_no_error_passes_on_clean_bundle(tmp_path: Path):
    records = [{"sequence": 1, "name": "turn_started", "turn_id": "t1"}]
    bundle = load_bundle(_make_bundle(tmp_path, records))
    assert_no_error(bundle)


def test_assert_no_error_flags_error_record(tmp_path: Path):
    records = [
        {
            "sequence": 1,
            "name": "error",
            "turn_id": "t1",
            "error": {"type": "STTTimeout", "message": "no partials"},
        }
    ]
    bundle = load_bundle(_make_bundle(tmp_path, records))
    with pytest.raises(AssertionError, match="STTTimeout"):
        assert_no_error(bundle)


def test_assert_tool_called(tmp_path: Path):
    records = [
        {
            "sequence": 1,
            "name": "tool_call_started",
            "data": {"tool_name": "calculator"},
        }
    ]
    bundle = load_bundle(_make_bundle(tmp_path, records))
    assert_tool_called(bundle, tool_name="calculator")


def test_turn_records_and_find_record(tmp_path: Path):
    records = [
        {"sequence": 1, "name": "turn_started", "turn_id": "t1"},
        {"sequence": 2, "name": "stt_final", "turn_id": "t1", "data": {"text": "hi"}},
        {"sequence": 3, "name": "turn_started", "turn_id": "t2"},
    ]
    bundle = load_bundle(_make_bundle(tmp_path, records))
    assert len(turn_records(bundle, "t1")) == 2
    first = find_record(bundle, name="turn_started")
    assert first is not None
    assert first["turn_id"] == "t1"
    assert find_record(bundle, name="does_not_exist") is None
