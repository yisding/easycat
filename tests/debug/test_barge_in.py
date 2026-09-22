"""Barge-in surface: milestone delta, interruption count, and issue cards."""

from __future__ import annotations

from easycat.debug._issues import IssueThresholds, build_issues
from easycat.debug._turn_timeline import drain_ack_indices, turn_milestones, turn_waterfall


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


def test_barge_in_milestone_ignores_second_window_after_clean_stop() -> None:
    """A second playback window's real barge-in must not be shadowed by the first.

    ``bot_stopped_speaking`` closes the playback window it ends. Without that
    reset, a window that ended cleanly (the user never spoke during it) leaves
    ``bot_speaking`` latched ``True``, and any later, unrelated
    ``vad_start_speaking`` while the bot is quiet is wrongly captured as "the"
    barge-in, shadowing the real one in the next window.
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


def test_barge_in_milestone_ignores_second_window_without_barge_in() -> None:
    """Two clean windows and stray speech between them are not a barge-in."""
    records = [
        _rec(1, "bot_started_speaking", wall_ms=0),
        _rec(2, "bot_stopped_speaking", wall_ms=1000),
        _rec(3, "vad_start_speaking", wall_ms=3000),  # bot is quiet here
        _rec(4, "bot_started_speaking", wall_ms=5000),
        _rec(5, "bot_stopped_speaking", wall_ms=7000),  # nobody interrupted
    ]
    milestones = turn_milestones(records)
    assert milestones["t1"]["user_speech_start_to_bot_stopped_ms"] is None


def test_barge_in_milestone_survives_mark_acks_during_playback() -> None:
    """Playback mark acks are mid-window progress, not the end of the window.

    Transports with playback acknowledgements emit a ``playback_mark_ack``
    every ``_playback_mark_bytes_interval`` bytes (~125ms) while the bot is
    still talking. Treating one as a window close would drop every barge-in
    that happens after the first ack.
    """
    records = [
        _rec(1, "bot_started_speaking", wall_ms=0),
        _rec(2, "playback_mark_ack", wall_ms=125),
        _rec(3, "playback_mark_ack", wall_ms=250),
        _rec(4, "playback_mark_ack", wall_ms=375),
        _rec(5, "vad_start_speaking", wall_ms=2000),  # the real barge-in
        _rec(6, "playback_mark_ack", wall_ms=2125),  # the bot going quiet
    ]
    milestones = turn_milestones(records)
    assert milestones["t1"]["user_speech_start_to_bot_stopped_ms"] == 125.0


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


def test_missed_barge_in_survives_mark_acks_during_playback() -> None:
    """Mid-playback mark acks must not close the window the user barges into.

    Transports with playback acknowledgements emit a ``playback_mark_ack``
    every ``_playback_mark_bytes_interval`` bytes (~125ms) while the bot is
    still talking. Treating one as the end of the playback window would leave
    ``bot_speaking`` False for the rest of it and silently drop every missed
    barge-in that happens after the first ack.
    """
    records = [
        _rec(1, "bot_started_speaking", wall_ms=0),
        _rec(2, "playback_mark_ack", wall_ms=125),
        _rec(3, "playback_mark_ack", wall_ms=250),
        _rec(4, "playback_mark_ack", wall_ms=375),
        _rec(5, "vad_start_speaking", wall_ms=500),  # the bot talks over the user
    ]
    report = build_issues(records)
    missed = [i for i in report["issues"] if i["code"] == "missed_barge_in"]
    assert len(missed) == 1
    assert missed[0]["stage"] == "vad"
    assert missed[0]["turn_id"] == "t1"


def test_missed_barge_in_resolved_by_mark_ack_after_the_barge_in() -> None:
    """An ack AFTER the barge-in still counts as the bot going quiet."""
    records = [
        _rec(1, "bot_started_speaking", wall_ms=0),
        _rec(2, "playback_mark_ack", wall_ms=125),
        _rec(3, "playback_mark_ack", wall_ms=250),
        _rec(4, "vad_start_speaking", wall_ms=500),
        _rec(5, "playback_mark_ack", wall_ms=625),  # bot went quiet in the window
    ]
    report = build_issues(records)
    assert not [i for i in report["issues"] if i["code"] == "missed_barge_in"]


def test_missed_barge_in_fires_when_ack_lands_after_the_window() -> None:
    """An ack later than the miss window does not rescue the barge-in."""
    thresholds = IssueThresholds()
    late_ms = 500 + thresholds.missed_barge_in_window_ms + 100
    records = [
        _rec(1, "bot_started_speaking", wall_ms=0),
        _rec(2, "playback_mark_ack", wall_ms=125),
        _rec(3, "vad_start_speaking", wall_ms=500),
        _rec(4, "playback_mark_ack", wall_ms=late_ms),
    ]
    report = build_issues(records)
    missed = [i for i in report["issues"] if i["code"] == "missed_barge_in"]
    assert len(missed) == 1
    assert missed[0]["metric"] == "missed_barge_in_window_ms"


def test_missed_barge_in_ignores_speech_after_the_window_closed() -> None:
    """``bot_stopped_speaking`` still closes the window for later speech."""
    records = [
        _rec(1, "bot_started_speaking", wall_ms=0),
        _rec(2, "playback_mark_ack", wall_ms=125),
        _rec(3, "bot_stopped_speaking", wall_ms=1000),
        _rec(4, "vad_start_speaking", wall_ms=3000),  # bot is quiet here
    ]
    report = build_issues(records)
    assert not [i for i in report["issues"] if i["code"] == "missed_barge_in"]


# ── mid-playback progress acks vs the drain ack ──────────────────


def _ack_stream(
    start_seq: int, *, first_ms: float, last_ms: float, every_ms: float = 125.0
) -> list[dict]:
    """A transport's ``playback_mark_ack`` run, one ack every *every_ms*."""
    records: list[dict] = []
    wall_ms = first_ms
    seq = start_seq
    while wall_ms <= last_ms:
        records.append(_rec(seq, "playback_mark_ack", wall_ms=wall_ms))
        wall_ms += every_ms
        seq += 1
    return records


def test_drain_ack_indices_marks_only_the_last_ack_of_each_run() -> None:
    names = [
        "bot_started_speaking",
        "playback_mark_ack",
        "playback_mark_ack",  # drain of run 1
        "bot_stopped_speaking",
        "bot_started_speaking",
        "playback_mark_ack",
        "vad_start_speaking",
        "playback_mark_ack",  # drain of run 2
    ]
    assert drain_ack_indices(names) == frozenset({2, 7})


def test_drain_ack_indices_ignores_journals_without_acks() -> None:
    assert drain_ack_indices(["bot_started_speaking", "bot_stopped_speaking"]) == frozenset()


def test_missed_barge_in_fires_while_the_ack_stream_keeps_running() -> None:
    """The shape issue #1156 describes: acks keep arriving past the barge-in.

    A missed barge-in means the bot never stopped, so an ack-emitting transport
    keeps acknowledging marks every ~125ms for the whole utterance. Accepting
    any of those progress acks as "the bot went quiet" resolves the candidate
    ~125ms after the user spoke and the card can never fire.
    """
    records = [
        _rec(1, "bot_started_speaking", wall_ms=0),
        *_ack_stream(2, first_ms=125, last_ms=5000),
        _rec(100, "vad_start_speaking", wall_ms=2000),  # the bot talks over the user
    ]
    report = build_issues(records)
    missed = [i for i in report["issues"] if i["code"] == "missed_barge_in"]
    assert len(missed) == 1
    assert missed[0]["stage"] == "vad"


def test_missed_barge_in_fires_when_playback_only_drains_after_the_window() -> None:
    """A drain ack (and stop) far past the window does not rescue the barge-in."""
    records = [
        _rec(1, "bot_started_speaking", wall_ms=0),
        *_ack_stream(2, first_ms=125, last_ms=5000),
        _rec(100, "vad_start_speaking", wall_ms=2000),
        _rec(101, "bot_stopped_speaking", wall_ms=5125),
    ]
    report = build_issues(records)
    missed = [i for i in report["issues"] if i["code"] == "missed_barge_in"]
    assert len(missed) == 1


def test_missed_barge_in_resolved_when_the_ack_stream_drains_in_window() -> None:
    """Playback that really drains just after the barge-in is not a miss."""
    records = [
        _rec(1, "bot_started_speaking", wall_ms=0),
        *_ack_stream(2, first_ms=125, last_ms=2125),  # last ack = the drain ack
        _rec(100, "vad_start_speaking", wall_ms=2000),
    ]
    report = build_issues(records)
    assert not [i for i in report["issues"] if i["code"] == "missed_barge_in"]


def test_slow_barge_in_fires_while_the_ack_stream_keeps_running() -> None:
    """Progress acks must not stop the cutoff clock for an acted barge-in."""
    records = [
        _rec(1, "bot_started_speaking", wall_ms=0),
        _rec(2, "interruption", wall_ms=100),
        *_ack_stream(3, first_ms=125, last_ms=3000),
    ]
    report = build_issues(records)
    slow = [i for i in report["issues"] if i["code"] == "slow_barge_in"]
    assert len(slow) == 1
    # The clock runs to the drain ack at 3000ms, not the 125ms progress ack.
    assert slow[0]["value"] == 2900.0


def test_barge_in_milestone_skips_mid_playback_acks_after_the_barge_in() -> None:
    """The milestone and the issue card read the same journal the same way."""
    records = [
        _rec(1, "bot_started_speaking", wall_ms=0),
        *_ack_stream(2, first_ms=125, last_ms=3000),
        _rec(100, "vad_start_speaking", wall_ms=2000),
        _rec(101, "bot_stopped_speaking", wall_ms=3125),
    ]
    milestones = turn_milestones(records)
    assert milestones["t1"]["user_speech_start_to_bot_stopped_ms"] == 1000.0
