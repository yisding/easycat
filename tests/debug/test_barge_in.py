"""Barge-in surface: milestone delta, interruption count, and issue cards."""

from __future__ import annotations

import pytest

from easycat.debug._issues import IssueThresholds, build_issues
from easycat.debug._turn_timeline import turn_milestones, turn_waterfall


def _rec(seq: int, name: str, *, turn_id: str = "t1", wall_ms: float = 0.0, **data) -> dict:
    record: dict = {
        "sequence": seq,
        "name": name,
        "turn_id": turn_id,
        "timing": {"wall_ns": int(wall_ms * 1_000_000)},
    }
    if data:
        record["data"] = data
    return record


# ── Milestone: user_speech_start_to_bot_stopped_ms ───────────────


def test_milestone_key_always_present_in_waterfall() -> None:
    """Both the per-turn and the empty milestone dicts carry the new key."""
    record = _rec(1, "stt_final", text="hi")
    (turn,) = turn_waterfall([record])
    assert "user_speech_start_to_bot_stopped_ms" in turn["milestones"]
    # A text-only turn never barged in, so the delta is None.
    assert turn["milestones"]["user_speech_start_to_bot_stopped_ms"] is None


def test_barge_in_milestone_computes_cutoff_delta() -> None:
    records = [
        _rec(1, "bot_started_speaking", wall_ms=0),
        _rec(2, "vad_start_speaking", wall_ms=100),
        _rec(3, "bot_stopped_speaking", wall_ms=400),
    ]
    milestones = turn_milestones(records)
    assert milestones["t1"]["user_speech_start_to_bot_stopped_ms"] == 300.0


def test_barge_in_milestone_uses_playback_mark_ack_as_stop() -> None:
    records = [
        _rec(1, "bot_started_speaking", wall_ms=0),
        _rec(2, "vad_start_speaking", wall_ms=200),
        _rec(3, "playback_mark_ack", wall_ms=450),
    ]
    milestones = turn_milestones(records)
    assert milestones["t1"]["user_speech_start_to_bot_stopped_ms"] == 250.0


def test_barge_in_milestone_ignores_user_speech_before_bot_started() -> None:
    """A user utterance before any playback window is not a barge-in."""
    records = [
        _rec(1, "vad_start_speaking", wall_ms=0),
        _rec(2, "bot_started_speaking", wall_ms=100),
        _rec(3, "bot_stopped_speaking", wall_ms=500),
    ]
    milestones = turn_milestones(records)
    assert milestones["t1"]["user_speech_start_to_bot_stopped_ms"] is None


@pytest.mark.xfail(strict=True, reason="_barge_in_walls never resets bot_speaking (see #issue)")
def test_barge_in_milestone_ignores_second_window_after_clean_stop() -> None:
    """A second playback window's real barge-in must not be shadowed by the first.

    ``_barge_in_walls`` sets ``bot_speaking = True`` on ``bot_started_speaking``
    but only clears it back to ``False`` inside the branch guarded by
    ``user_speech_start is not None`` — so a playback window that ends cleanly
    (the user never spoke during it) leaves ``bot_speaking`` stuck ``True``.
    Any later, unrelated ``vad_start_speaking`` — even one long after the bot
    went quiet and unrelated to any playback window — is then wrongly latched
    in as "the" barge-in, shadowing the real one that follows.
    """
    records = [
        _rec(1, "bot_started_speaking", wall_ms=0),
        _rec(2, "bot_stopped_speaking", wall_ms=1000),  # window 1 ends cleanly
        _rec(3, "vad_start_speaking", wall_ms=3000),  # unrelated speech, bot is quiet
        _rec(4, "bot_started_speaking", wall_ms=5000),  # window 2 opens
        _rec(5, "vad_start_speaking", wall_ms=6100),  # the real barge-in
        _rec(6, "bot_stopped_speaking", wall_ms=6200),  # real cutoff, 100ms later
    ]
    milestones = turn_milestones(records)
    assert milestones["t1"]["user_speech_start_to_bot_stopped_ms"] == 100.0


def test_barge_in_milestone_is_robust_to_record_order() -> None:
    """Detection is pure wall ordering, not arrival order."""
    records = [
        _rec(3, "bot_stopped_speaking", wall_ms=400),
        _rec(1, "bot_started_speaking", wall_ms=0),
        _rec(2, "vad_start_speaking", wall_ms=100),
    ]
    milestones = turn_milestones(records)
    assert milestones["t1"]["user_speech_start_to_bot_stopped_ms"] == 300.0


# ── interruption_count as a top-level turn key ───────────────────


def test_waterfall_carries_interruption_count_top_level() -> None:
    records = [
        _rec(1, "turn_started", wall_ms=0),
        _rec(
            2,
            "control_signal",
            wall_ms=100,
            stage="tts",
            signal_kind="interrupt",
            signal_id="abc",
        ),
        _rec(3, "turn_ended", wall_ms=200),
    ]
    (turn,) = turn_waterfall(records)
    assert turn["interruption_count"] == 1
    # The count is a TOP-LEVEL turn key, never inside milestones.
    assert "interruption_count" not in turn["milestones"]


def test_waterfall_interruption_count_defaults_to_zero() -> None:
    (turn,) = turn_waterfall([_rec(1, "stt_final", text="hi")])
    assert turn["interruption_count"] == 0


def test_waterfall_ignores_non_string_interrupt_signal_id() -> None:
    records = [
        _rec(
            1,
            "control_signal",
            signal_kind="interrupt",
            signal_id=["malformed"],
        ),
    ]

    (turn,) = turn_waterfall(records)

    assert turn["interruption_count"] == 1


# ── slow_barge_in card ───────────────────────────────────────────


def test_slow_barge_in_fires_when_cutoff_exceeds_budget() -> None:
    # Interruption at 0ms, bot did not stop until 900ms > 600ms budget.
    records = [
        _rec(1, "bot_started_speaking", wall_ms=0),
        _rec(2, "interruption", wall_ms=100),
        _rec(3, "bot_stopped_speaking", wall_ms=900),
    ]
    report = build_issues(records)
    slow = [i for i in report["issues"] if i["code"] == "slow_barge_in"]
    assert len(slow) == 1
    issue = slow[0]
    assert issue["severity"] == "warning"
    assert issue["metric"] == "barge_in_cutoff_ms"
    assert issue["value"] == 800.0
    assert issue["threshold"] == IssueThresholds().barge_in_cutoff_ms
    assert issue["stage"] == "tts"
    assert issue["turn_id"] == "t1"


def test_slow_barge_in_does_not_fire_on_fast_cutoff() -> None:
    records = [
        _rec(1, "bot_started_speaking", wall_ms=0),
        _rec(2, "interruption", wall_ms=100),
        _rec(3, "bot_stopped_speaking", wall_ms=300),  # 200ms < 600ms budget
    ]
    report = build_issues(records)
    assert not [i for i in report["issues"] if i["code"] == "slow_barge_in"]


def test_slow_barge_in_handles_control_signal_interrupt() -> None:
    records = [
        _rec(1, "bot_started_speaking", wall_ms=0),
        _rec(2, "control_signal", wall_ms=100, signal_kind="interrupt", signal_id="x"),
        _rec(3, "playback_mark_ack", wall_ms=1000),
    ]
    report = build_issues(records)
    slow = [i for i in report["issues"] if i["code"] == "slow_barge_in"]
    assert len(slow) == 1
    assert slow[0]["value"] == 900.0


# ── missed_barge_in card ─────────────────────────────────────────


def test_missed_barge_in_fires_when_bot_never_stops() -> None:
    # User speaks over the bot; nothing interrupts or stops it in the window.
    records = [
        _rec(1, "bot_started_speaking", wall_ms=0),
        _rec(2, "vad_start_speaking", wall_ms=100),
    ]
    report = build_issues(records)
    missed = [i for i in report["issues"] if i["code"] == "missed_barge_in"]
    assert len(missed) == 1
    issue = missed[0]
    assert issue["severity"] == "warning"
    assert issue["metric"] == "missed_barge_in_window_ms"
    assert issue["threshold"] == IssueThresholds().missed_barge_in_window_ms
    assert issue["stage"] == "vad"
    assert issue["turn_id"] == "t1"


def test_missed_barge_in_does_not_fire_when_interruption_acted() -> None:
    records = [
        _rec(1, "bot_started_speaking", wall_ms=0),
        _rec(2, "vad_start_speaking", wall_ms=100),
        _rec(3, "interruption", wall_ms=300),  # acted within the window
    ]
    report = build_issues(records)
    assert not [i for i in report["issues"] if i["code"] == "missed_barge_in"]


def test_missed_barge_in_does_not_fire_when_bot_stops_in_window() -> None:
    records = [
        _rec(1, "bot_started_speaking", wall_ms=0),
        _rec(2, "vad_start_speaking", wall_ms=100),
        _rec(3, "bot_stopped_speaking", wall_ms=500),  # stopped within window
    ]
    report = build_issues(records)
    assert not [i for i in report["issues"] if i["code"] == "missed_barge_in"]


def test_missed_barge_in_does_not_fire_outside_playback_window() -> None:
    # No bot playback open when the user spoke — that is just a normal turn.
    records = [
        _rec(1, "vad_start_speaking", wall_ms=0),
        _rec(2, "stt_final", wall_ms=200, text="hello"),
    ]
    report = build_issues(records)
    assert not [i for i in report["issues"] if i["code"] == "missed_barge_in"]
