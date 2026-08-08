"""Transcript projection from journal records (``debug/_turn_timeline``)."""

from __future__ import annotations

from easycat.debug._turn_timeline import extract_turn_transcripts


def _delta_record(seq: int, data: dict) -> dict:
    return {"sequence": seq, "name": "agent_delta", "turn_id": "t1", "data": data}


def test_transcript_fallback_reconstructs_indexed_text_replacements():
    """Interrupted indexed-stream turns rebuild agent text with part semantics.

    Mirrors ``AgentTextStream``: ``TEXT_REPLACE`` overwrites its part, an
    indexed ``TEXT_DELTA`` appends to it, and parts join in index order.
    """
    records = [
        _delta_record(1, {"type": "TEXT_REPLACE", "text": "stale", "part_index": 0}),
        _delta_record(2, {"type": "TEXT_REPLACE", "text": "Hello", "part_index": 0}),
        _delta_record(3, {"type": "TEXT_DELTA", "text": " world", "part_index": 0}),
        _delta_record(4, {"type": "TEXT_REPLACE", "text": "!", "part_index": 1}),
    ]

    [entry] = extract_turn_transcripts(records)

    assert entry["agent"] == "Hello world!"
    assert entry["agent_seq"] == 1


def test_transcript_fallback_keeps_flat_deltas_as_appends():
    records = [
        _delta_record(1, {"type": "TEXT_DELTA", "text": "hello"}),
        _delta_record(2, {"type": "TEXT_DELTA", "text": " back"}),
    ]

    [entry] = extract_turn_transcripts(records)

    assert entry["agent"] == "hello back"
    assert entry["agent_seq"] == 1
