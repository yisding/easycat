"""Two-source per-turn diff engine (``debug/_turn_diff.diff_bundles``)."""

from __future__ import annotations

from typing import Any

from easycat.debug._turn_diff import diff_bundles


def _rec(seq: int, name: str, *, turn_id: str, wall_ms: float = 0.0, **data: Any) -> dict:
    record: dict = {
        "sequence": seq,
        "name": name,
        "turn_id": turn_id,
        "timing": {"wall_ns": int(wall_ms * 1_000_000)},
    }
    if data:
        record["data"] = data
    return record


def _chain(
    turn_id: str,
    *,
    base_seq: int,
    vad: float,
    stt: float,
    req: float,
    token: float,
    tts: float,
    user: str = "hello",
    agent: str = "hi there",
) -> list[dict]:
    """A full milestone chain plus a transcript for one turn at given wall_ms."""
    return [
        _rec(base_seq, "vad_stop_speaking", turn_id=turn_id, wall_ms=vad),
        _rec(base_seq + 1, "stt_final", turn_id=turn_id, wall_ms=stt, text=user),
        _rec(base_seq + 2, "agent_request_started", turn_id=turn_id, wall_ms=req),
        _rec(
            base_seq + 3,
            "agent_delta",
            turn_id=turn_id,
            wall_ms=token,
            text=agent,
            type="TEXT_DELTA",
        ),
        _rec(base_seq + 4, "tts_frame", turn_id=turn_id, wall_ms=tts, audio_bytes=320),
    ]


def test_diff_flags_regressed_milestone() -> None:
    """A milestone slower on B (past both gates) is flagged ``regressed`` with
    a positive ``delta_ms``."""
    a = _chain("ta", base_seq=1, vad=0, stt=50, req=100, token=200, tts=300)
    # Same turn, but the agent took 100 ms longer to first token.
    b = _chain("tb", base_seq=1, vad=0, stt=50, req=100, token=300, tts=400)

    result = diff_bundles(a, b)
    (turn,) = result["turns"]
    cell = turn["milestones"]["agent_request_to_first_token_ms"]
    assert cell["a"] == 100.0
    assert cell["b"] == 200.0
    assert cell["delta_ms"] == 100.0
    assert cell["pct"] == 100.0
    assert cell["regressed"] is True

    worst = result["summary"]["worst_regression"]
    assert worst is not None
    assert worst["milestone"] == "agent_first_token_to_tts_first_byte_ms" or worst["delta_ms"] > 0
    assert worst["delta_ms"] >= 100.0


def test_diff_does_not_flag_small_or_faster_deltas() -> None:
    """A delta inside the noise gate, or a B that is faster, never regresses."""
    a = _chain("ta", base_seq=1, vad=0, stt=50, req=100, token=200, tts=300)
    # token->tts is 2 ms faster (improvement), stt->req only 4 ms slower (under gate),
    # req->token unchanged.
    b = _chain("tb", base_seq=1, vad=0, stt=50, req=104, token=204, tts=302)

    result = diff_bundles(a, b)
    (turn,) = result["turns"]
    # 4 ms slower stt->req is under the 5 ms absolute gate.
    assert turn["milestones"]["stt_final_to_agent_request_ms"]["regressed"] is False
    # A faster milestone is never a regression.
    improved = turn["milestones"]["agent_first_token_to_tts_first_byte_ms"]
    assert improved["delta_ms"] < 0
    assert improved["regressed"] is False
    assert result["summary"]["worst_regression"] is None


def test_diff_aligns_turns_by_index() -> None:
    """Turn 0 of A pairs with turn 0 of B even when the turn ids differ."""
    a = _chain("a0", base_seq=1, vad=0, stt=50, req=100, token=200, tts=300) + _chain(
        "a1", base_seq=10, vad=1000, stt=1050, req=1100, token=1200, tts=1300
    )
    b = _chain("b0", base_seq=1, vad=0, stt=50, req=100, token=200, tts=300) + _chain(
        "b1", base_seq=10, vad=1000, stt=1050, req=1100, token=1200, tts=1300
    )

    result = diff_bundles(a, b)
    assert [t["index"] for t in result["turns"]] == [0, 1]
    assert result["turns"][0]["turn_id_a"] == "a0"
    assert result["turns"][0]["turn_id_b"] == "b0"
    assert result["turns"][1]["turn_id_a"] == "a1"
    assert result["turns"][1]["turn_id_b"] == "b1"
    assert all(not t["unmatched"] for t in result["turns"])


def test_diff_handles_ragged_turn_counts() -> None:
    """An extra turn on one side pads ``None`` and marks the pair unmatched
    without crashing."""
    a = _chain("a0", base_seq=1, vad=0, stt=50, req=100, token=200, tts=300)
    b = _chain("b0", base_seq=1, vad=0, stt=50, req=100, token=200, tts=300) + _chain(
        "b1", base_seq=10, vad=1000, stt=1050, req=1100, token=1200, tts=1300
    )

    result = diff_bundles(a, b)
    assert len(result["turns"]) == 2
    extra = result["turns"][1]
    assert extra["turn_id_a"] is None
    assert extra["turn_id_b"] == "b1"
    assert extra["unmatched"] is True
    # Missing A side leaves every milestone delta None, never raising.
    for cell in extra["milestones"].values():
        assert cell["a"] is None
        assert cell["delta_ms"] is None
        assert cell["regressed"] is False


def test_diff_handles_missing_milestones_without_crashing() -> None:
    """A text-only turn (no VAD endpoint, no TTS) diffs to ``None`` deltas."""
    a = [_rec(1, "stt_final", turn_id="a0", wall_ms=0, text="hi")]
    b = [_rec(1, "stt_final", turn_id="b0", wall_ms=0, text="hi")]

    result = diff_bundles(a, b)
    (turn,) = result["turns"]
    assert turn["milestones"]["vad_endpoint_to_stt_final_ms"]["delta_ms"] is None
    assert turn["milestones"]["vad_endpoint_to_stt_final_ms"]["regressed"] is False


def test_diff_computes_transcript_changed_flag() -> None:
    a = _chain("a0", base_seq=1, vad=0, stt=50, req=100, token=200, tts=300, agent="first answer")
    b = _chain("b0", base_seq=1, vad=0, stt=50, req=100, token=200, tts=300, agent="second answer")
    same_b = _chain(
        "b0", base_seq=1, vad=0, stt=50, req=100, token=200, tts=300, agent="first answer"
    )

    changed = diff_bundles(a, b)["turns"][0]["transcript"]
    assert changed["changed"] is True
    assert changed["agent_a"] == "first answer"
    assert changed["agent_b"] == "second answer"

    unchanged = diff_bundles(a, same_b)["turns"][0]["transcript"]
    assert unchanged["changed"] is False


def test_diff_milestone_keys_are_dynamic() -> None:
    """Every milestone key from ``turn_milestones`` flows through the diff, so a
    future milestone change never silently drops a segment."""
    from easycat.debug._turn_timeline import turn_milestones

    a = _chain("a0", base_seq=1, vad=0, stt=50, req=100, token=200, tts=300)
    expected_keys = set(turn_milestones(a)["a0"])

    result = diff_bundles(a, a)
    assert set(result["turns"][0]["milestones"]) == expected_keys


def test_diff_worst_regression_picks_largest_delta() -> None:
    """Across multiple regressed milestones the summary names the largest delta.

    B shifts only the first-token landmark 200 ms later (everything downstream
    rides along), so the single-segment ``agent_request_to_first_token_ms``
    regression (+200 ms) is the worst; the end-to-end ``vad->tts`` delta also
    moves +200 ms but never more, so the per-segment headline wins the tie by
    encounter order.
    """
    a = _chain("a0", base_seq=1, vad=0, stt=50, req=100, token=200, tts=300)
    # first token slips 200 ms; token->tts span is preserved (tts also +200).
    b = _chain("b0", base_seq=1, vad=0, stt=50, req=100, token=400, tts=500)

    worst = diff_bundles(a, b)["summary"]["worst_regression"]
    assert worst["delta_ms"] == 200.0
    assert worst["milestone"] in {
        "agent_request_to_first_token_ms",
        "vad_endpoint_to_tts_first_byte_ms",
    }
