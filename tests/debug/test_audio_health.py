"""Audio-health heuristics: clipping, near-silent capture, and dead air.

These exercise the stdlib PCM decode/RMS/clip helpers directly and the
``build_issues`` audio cards that fire only when an ``artifact_resolver`` is
supplied (the WP4 record-only contract is preserved when it is omitted).
"""

from __future__ import annotations

import math
from array import array

from easycat.debug._audio_health import (
    collect_caller_silence,
    collect_clipping,
    compute_rms,
    detect_clipping,
    detect_dead_air,
)
from easycat.debug._issues import build_issues


def _pcm16(samples: list[int]) -> bytes:
    """Little-endian int16 blob for *samples* (matches the stored artifact)."""
    return array("h", samples).tobytes()


def _clipped_blob(n: int = 200) -> bytes:
    return _pcm16([32767] * n)


def _silent_blob(n: int = 4000) -> bytes:
    return _pcm16([0] * n)


def _loud_blob(n: int = 4000) -> bytes:
    # A clean sine well above the silence floor, no clipping.
    return _pcm16([int(8000 * math.sin(i / 5.0)) for i in range(n)])


def _tts_frame(seq: int, ref: str, *, turn_id: str = "t1", **data) -> dict:
    record: dict = {
        "sequence": seq,
        "name": "tts_frame",
        "turn_id": turn_id,
        "output_ref": ref,
        "timing": {"wall_ns": 0},
        "data": {"stage": "tts", **data},
    }
    return record


def _stt_start(seq: int, ref: str, *, turn_id: str = "t1", wall_ms: float = 0.0, **data) -> dict:
    record: dict = {
        "sequence": seq,
        "name": "stage_start",
        "turn_id": turn_id,
        "input_ref": ref,
        "timing": {"wall_ns": int(wall_ms * 1_000_000)},
        "data": {"stage": "stt", **data},
    }
    return record


# ── primitive helpers ────────────────────────────────────────────


def test_iter_int16_round_trips_little_endian() -> None:
    from easycat.debug._audio_health import _iter_int16

    blob = _pcm16([1, -1, 32767, -32768, 7])
    samples = _iter_int16(blob, stride=1)
    assert list(samples) == [1, -1, 32767, -32768, 7]


def test_iter_int16_drops_trailing_odd_byte() -> None:
    from easycat.debug._audio_health import _iter_int16

    blob = _pcm16([5, 6]) + b"\x01"  # one stray byte
    samples = _iter_int16(blob, stride=1)
    assert list(samples) == [5, 6]


def test_detect_clipping_finds_longest_run() -> None:
    samples = array("h", [0, 32767, 32767, 0, -32767, -32767, -32767, 0])
    assert detect_clipping(samples, max_run=2) >= 2


def test_detect_clipping_zero_for_clean_audio() -> None:
    samples = array("h", [0, 100, -200, 300])
    assert detect_clipping(samples, max_run=16) == 0


def test_compute_rms_silent_is_zero_loud_is_positive() -> None:
    assert compute_rms(array("h", [])) == 0.0
    assert compute_rms(array("h", [0, 0, 0])) == 0.0
    assert compute_rms(array("h", [1000, -1000, 1000])) > 900.0


# ── collectors ───────────────────────────────────────────────────


def test_collect_clipping_flags_bot_and_caller() -> None:
    blobs = {"bot": _clipped_blob(), "caller": _clipped_blob()}
    records = [
        _tts_frame(1, "bot", turn_id="t1"),
        _stt_start(2, "caller", turn_id="t2"),
    ]
    hits = collect_clipping(records, blobs.get, stride=4, clip_consecutive=16)
    sides = {side for side, _t, _s in hits}
    assert sides == {"bot", "caller"}


def test_collect_clipping_skips_mulaw_sample_width_one() -> None:
    blobs = {"bot": _clipped_blob()}
    # Telephony mu-law artifact: sample_width 1 must be skipped.
    records = [_tts_frame(1, "bot", sample_width=1, encoding="mulaw")]
    assert collect_clipping(records, blobs.get, stride=4, clip_consecutive=16) == []


def test_collect_caller_silence_flags_quiet_turn_only() -> None:
    blobs = {"q": _silent_blob(), "loud": _loud_blob()}
    records = [
        _stt_start(1, "q", turn_id="quiet"),
        _stt_start(2, "loud", turn_id="audible"),
    ]
    flagged = collect_caller_silence(records, blobs.get, stride=4, silence_rms=200.0)
    assert set(flagged) == {"quiet"}


def test_collect_caller_silence_peak_excludes_turn_with_one_loud_frame() -> None:
    blobs = {"q": _silent_blob(), "loud": _loud_blob()}
    # One quiet frame and one loud frame in the SAME turn -> not silent.
    records = [
        _stt_start(1, "q", turn_id="t1"),
        _stt_start(2, "loud", turn_id="t1"),
    ]
    flagged = collect_caller_silence(records, blobs.get, stride=4, silence_rms=200.0)
    assert flagged == {}


def test_detect_dead_air_flags_large_gap_only() -> None:
    records = [
        _tts_frame(1, "ref", turn_id="gap", **{}),
    ]
    records[0]["timing"]["wall_ns"] = 0
    records.append(_tts_frame(2, "ref", turn_id="gap"))
    records[1]["timing"]["wall_ns"] = 4_000 * 1_000_000  # 4s gap
    flagged = detect_dead_air(records, dead_air_ms=3_000.0)
    assert set(flagged) == {"gap"}
    assert flagged["gap"] == 4_000.0


def test_detect_dead_air_ignores_small_gap() -> None:
    records = [
        _tts_frame(1, "ref", turn_id="ok"),
        _tts_frame(2, "ref", turn_id="ok"),
    ]
    records[0]["timing"]["wall_ns"] = 0
    records[1]["timing"]["wall_ns"] = 1_000 * 1_000_000  # 1s gap
    flagged = detect_dead_air(records, dead_air_ms=3_000.0)
    assert flagged == {}


# ── build_issues integration ─────────────────────────────────────


def test_build_issues_no_resolver_skips_audio_cards() -> None:
    """WP4 contract: without a resolver, no audio cards are produced."""
    records = [_tts_frame(1, "bot")]
    report = build_issues(records)  # no artifact_resolver
    codes = {issue["code"] for issue in report["issues"]}
    assert not (codes & {"clipping_bot", "clipping_caller", "near_silent_capture", "dead_air"})
    # And the resolver-less call still works positionally (WP4/WP5 contract).
    assert report["total"] == 0


def test_build_issues_clipping_bot_card() -> None:
    blobs = {"bot": _clipped_blob()}
    records = [_tts_frame(1, "bot")]
    report = build_issues(records, artifact_resolver=blobs.get)
    cards = [i for i in report["issues"] if i["code"] == "clipping_bot"]
    assert len(cards) == 1
    assert cards[0]["severity"] == "warning"
    assert cards[0]["stage"] == "tts"
    assert report["summary"]["warning"] >= 1


def test_build_issues_near_silent_capture_card() -> None:
    blobs = {"q": _silent_blob()}
    records = [_stt_start(1, "q", turn_id="quiet")]
    report = build_issues(records, artifact_resolver=blobs.get)
    cards = [i for i in report["issues"] if i["code"] == "near_silent_capture"]
    assert len(cards) == 1
    assert cards[0]["turn_id"] == "quiet"
    assert cards[0]["stage"] == "stt"


def test_build_issues_dead_air_card_for_four_second_gap() -> None:
    records = [
        _tts_frame(1, "a", turn_id="gap"),
        _tts_frame(2, "b", turn_id="gap"),
    ]
    records[0]["timing"]["wall_ns"] = 0
    records[1]["timing"]["wall_ns"] = 4_000 * 1_000_000
    # No clipping/silence blobs -> resolver returns None, only dead air fires.
    report = build_issues(records, artifact_resolver=lambda _ref: None)
    cards = [i for i in report["issues"] if i["code"] == "dead_air"]
    assert len(cards) == 1
    assert cards[0]["severity"] == "info"
    assert report["summary"]["info"] >= 1


def test_build_issues_one_second_gap_is_not_dead_air() -> None:
    records = [
        _tts_frame(1, "a", turn_id="ok"),
        _tts_frame(2, "b", turn_id="ok"),
    ]
    records[0]["timing"]["wall_ns"] = 0
    records[1]["timing"]["wall_ns"] = 1_000 * 1_000_000
    report = build_issues(records, artifact_resolver=lambda _ref: None)
    assert [i for i in report["issues"] if i["code"] == "dead_air"] == []


def test_build_issues_skips_mulaw_artifacts() -> None:
    blobs = {"bot": _clipped_blob()}
    records = [_tts_frame(1, "bot", sample_width=1, encoding="mulaw")]
    report = build_issues(records, artifact_resolver=blobs.get)
    assert [i for i in report["issues"] if i["code"].startswith("clipping")] == []


def test_build_issues_caches_audio_artifacts_across_collectors() -> None:
    blob = _silent_blob()
    calls: list[str] = []

    def resolver(ref: str) -> bytes | None:
        calls.append(ref)
        return blob

    records = [
        _stt_start(1, "shared", turn_id="quiet"),
        _stt_start(2, "shared", turn_id="quiet"),
    ]
    report = build_issues(records, artifact_resolver=resolver)
    assert [issue for issue in report["issues"] if issue["code"] == "near_silent_capture"]
    assert calls == ["shared"]


def test_build_issues_skips_oversized_artifact_refs() -> None:
    def resolver(_ref: str) -> bytes | None:
        raise AssertionError("oversized refs must not be resolved")

    records = [_stt_start(1, "x" * 129, turn_id="quiet")]
    report = build_issues(records, artifact_resolver=resolver)
    assert [issue for issue in report["issues"] if issue["code"] == "near_silent_capture"] == []
