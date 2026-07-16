from __future__ import annotations

import operator

import pytest

from easycat.debug._bundle_summary import summarise_annotations, summarise_bundle_records


def test_bundle_record_summary_uses_timestamp_bounds_for_out_of_order_records() -> None:
    summary = summarise_bundle_records(
        [
            {
                "session_id": "",
                "turn_id": [],
                "wall_ns": 4_000_000_000,
                "error": "opaque error",
            },
            {
                "session_id": "session-1",
                "turn_id": "turn-2",
                "timing": {"wall_ns": 1_000_000_000},
                "error": {"type": "ToolTimeoutError"},
            },
            {
                "session_id": "ignored-later-session",
                "turn_id": "turn-1",
                "wall_ns": 3_000_000_000,
                "name": "tool_call_started",
            },
            {
                "turn_id": "",
                "wall_ns": "invalid",
                "name": "tool_call_started",
            },
        ]
    )

    assert summary.to_dict() == {
        "session_id": "session-1",
        "turn_count": 2,
        "errors": 2,
        "error_type": "ToolTimeoutError",
        "failing_turn_id": "turn-2",
        "tool_calls": 2,
        "records": 4,
        "duration_ms": 3000.0,
    }


def test_annotation_summary_tolerates_untrusted_sidecar_records() -> None:
    summary = summarise_annotations(
        {
            "turn-1": {"passed": True},
            "turn-2": {"passed": False, "failure_type": "tts_cutoff"},
            "turn-3": {"passed": None, "failure_type": "tts_cutoff"},
            "turn-4": "malformed",
        }
    )

    assert summary.to_dict() == {
        "annotated": 4,
        "passed": 1,
        "failed": 1,
        "failure_types": {"tts_cutoff": 2},
    }
    with pytest.raises(TypeError):
        operator.setitem(summary.failure_types, "tts_cutoff", 3)
