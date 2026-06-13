from __future__ import annotations

import pytest

pytest.importorskip("aiohttp")

import json
import zipfile

from easycat.debug.bundle import RunBundle
from easycat.debugger.server import (
    _build_issues,
    _build_transcript,
    _cost_rollup,
    _filter_records,
    _session_source,
    _summarise_turns,
)
from easycat.runtime import InMemoryRingBuffer
from easycat.runtime.artifacts import InMemoryArtifactStore
from easycat.runtime.records import ErrorInfo, JournalRecordKind

from ._server_helpers import _build_voice_bundle


def test_filter_records_by_stage():
    records = [
        {"sequence": 1, "name": "stage_start", "data": {"stage": "tts"}},
        {"sequence": 2, "name": "stage_complete", "data": {"stage": "stt"}},
        {"sequence": 3, "name": "tts_frame", "data": {"stage": "tts"}},
    ]
    out = _filter_records(
        records, stage="tts", turn_id=None, name=None, from_seq=None, to_seq=None
    )
    assert [r["sequence"] for r in out] == [1, 3]


def test_filter_records_by_sequence_range():
    records = [{"sequence": i, "data": {}} for i in range(10)]
    out = _filter_records(records, stage=None, turn_id=None, name=None, from_seq=3, to_seq=6)
    assert [r["sequence"] for r in out] == [3, 4, 5, 6]


def test_filter_records_by_multiple_names():
    records = [
        {"sequence": 1, "name": "vad_start_speaking", "data": {}},
        {"sequence": 2, "name": "tts_audio", "data": {}},
        {"sequence": 3, "name": "stt_partial", "data": {}},
        {"sequence": 4, "name": "bot_started_speaking", "data": {}},
    ]
    out = _filter_records(
        records,
        stage=None,
        turn_id=None,
        name=["vad_start_speaking", "stt_partial"],
        from_seq=None,
        to_seq=None,
    )
    assert [r["sequence"] for r in out] == [1, 3]


def test_cost_rollup_reports_budget_status_from_snapshot():
    records = [
        {
            "sequence": 1,
            "name": "cost",
            "turn_id": "turn-1",
            "data": {"usd": 0.2, "stt_seconds": 1.5, "llm_tokens": True},
        },
        {
            "sequence": 2,
            "name": "cost_record",
            "turn_id": "turn-2",
            "data": {"usd": 0.65, "tts_chars": 120, "llm_tokens": 42},
        },
        {
            "sequence": 3,
            "name": "stage_complete",
            "turn_id": "turn-2",
            "data": {"usd": 10.0},
        },
    ]

    out = _cost_rollup(records, config_snapshot={"max_session_cost_usd": "1.0"})

    assert out["totals"]["usd"] == pytest.approx(0.85)
    assert out["totals"]["stt_seconds"] == pytest.approx(1.5)
    assert out["totals"]["tts_chars"] == pytest.approx(120)
    assert out["totals"]["llm_tokens"] == pytest.approx(42)
    assert out["per_turn"]["turn-1"]["usd"] == pytest.approx(0.2)
    assert out["per_turn"]["turn-2"]["usd"] == pytest.approx(0.65)
    assert out["budget"] == {
        "configured": True,
        "max_session_cost_usd": 1.0,
        "warning_threshold_usd": 0.8,
        "usage_fraction": pytest.approx(0.85),
        "remaining_usd": pytest.approx(0.15),
        "overage_usd": 0.0,
        "status": "warning",
        "warning": True,
        "exceeded": False,
    }


def test_cost_rollup_reports_exceeded_budget():
    out = _cost_rollup(
        [{"sequence": 1, "name": "cost", "turn_id": "turn-1", "data": {"usd": 1.25}}],
        config_snapshot={"max_session_cost_usd": "1.0"},
    )

    assert out["budget"]["status"] == "exceeded"
    assert out["budget"]["warning"] is True
    assert out["budget"]["exceeded"] is True
    assert out["budget"]["remaining_usd"] == 0.0
    assert out["budget"]["overage_usd"] == pytest.approx(0.25)


def test_transcript_ignores_malformed_turn_ids():
    records = [
        {
            "sequence": 1,
            "name": "stt_final",
            "turn_id": ["bad"],
            "data": {"text": "ignored"},
        },
        {
            "sequence": 2,
            "name": "agent_final",
            "turn_id": {"id": "bad"},
            "data": {"text": "also ignored"},
        },
        {
            "sequence": 3,
            "name": "stt_final",
            "turn_id": "turn-1",
            "data": {"text": "hello"},
        },
    ]

    out = _build_transcript(records)

    assert len(out) == 1
    assert out[0]["turn_id"] == "turn-1"
    assert out[0]["user"] == "hello"


def test_cost_rollup_treats_malformed_turn_ids_as_session_level_cost():
    records = [
        {"sequence": 1, "name": "cost", "turn_id": ["bad"], "data": {"usd": 0.25}},
        {
            "sequence": 2,
            "name": "cost_record",
            "turn_id": {"id": "bad"},
            "data": {"tts_chars": 10},
        },
        {"sequence": 3, "name": "cost", "turn_id": "turn-1", "data": {"usd": 0.75}},
    ]

    out = _cost_rollup(records)

    assert out["totals"]["usd"] == pytest.approx(1.0)
    assert out["totals"]["tts_chars"] == pytest.approx(10)
    assert out["per_turn"][""]["usd"] == pytest.approx(0.25)
    assert out["per_turn"][""]["tts_chars"] == pytest.approx(10)
    assert out["per_turn"]["turn-1"]["usd"] == pytest.approx(0.75)


def test_summarise_turns_tracks_audio_bytes():
    records = [
        {
            "sequence": 1,
            "turn_id": "t1",
            "name": "stage_start",
            "data": {"stage": "stt"},
            "timing": {"wall_ns": 1_000_000},
        },
        {
            "sequence": 2,
            "turn_id": "t1",
            "name": "tts_frame",
            "data": {"stage": "tts", "audio_bytes": 320},
            "timing": {"wall_ns": 5_000_000},
        },
        {
            "sequence": 3,
            "turn_id": "t1",
            "name": "tts_frame",
            "data": {"stage": "tts", "audio_bytes": 640},
            "timing": {"wall_ns": 9_000_000},
        },
    ]
    turns = _summarise_turns(records)
    assert len(turns) == 1
    assert turns[0]["turn_id"] == "t1"
    assert turns[0]["tts_audio_bytes"] == 960
    assert turns[0]["wall_ms"] == 8.0
    assert turns[0]["stage_counts"] == {"stt": 1, "tts": 2}


def test_serve_bundle_raises_helpful_error_when_aiohttp_missing(monkeypatch, tmp_path):
    """If aiohttp is unavailable, the entry points must fail with a clear
    message rather than a bare ImportError."""
    bundle_path = tmp_path / "empty.zip"
    # Build a minimal bundle so _bundle_source succeeds before _serve fails.
    with zipfile.ZipFile(bundle_path, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"format_version": 1}))
        zf.writestr("journal.ndjson", b"")

    import easycat.debugger.server as srv

    def _no_aiohttp(*_a, **_kw):
        raise ImportError("simulated missing aiohttp")

    monkeypatch.setattr(srv, "_make_app", lambda *_a, **_kw: _no_aiohttp())
    with pytest.raises(ImportError):
        srv.serve_bundle(bundle_path, open_browser=False)


def test_debugger_source_session_adapts_live_journal():
    """Live-session source should snapshot the journal each call and pull
    artifact bytes from the session's artifact store."""
    artifact_store = InMemoryArtifactStore()
    journal = InMemoryRingBuffer(capacity=8, artifact_store=artifact_store)
    ref = artifact_store.put(b"hello-world", artifact_class="replay_critical")

    class _StubSession:
        session_id = "stub-1"
        is_running = True
        turn_state = "IDLE"
        _artifact_store = artifact_store

        @property
        def journal(self):
            return journal

    source = _session_source(_StubSession())
    assert source.manifest()["session_id"] == "stub-1"
    assert source.artifact(ref) == b"hello-world"
    # Empty journal returns no records.
    assert source.records() == []
    # Adding a record makes it visible on the next records() call —
    # this is the polling contract live sources rely on.
    journal.append(
        kind=JournalRecordKind.EVENT,
        name="test",
        session_id="stub-1",
        error=ErrorInfo(
            type="ExceptionGroup",
            message="pipeline failed",
            children=(ErrorInfo(type="ValueError", message="bad input"),),
        ),
    )
    records = source.records()
    record = next(r for r in records if r["name"] == "test")
    assert record["error"]["children"][0]["type"] == "ValueError"
    assert record["error"]["children"][0]["message"] == "bad input"


def test_journal_view_exposes_latest_sequence():
    """``JournalView`` re-exposes the backend's O(1) ``latest_sequence`` so
    live-tailing callers can detect growth without re-reading the journal."""
    from easycat.runtime import JournalView

    journal = InMemoryRingBuffer(capacity=8)
    view = JournalView(journal)
    assert view.latest_sequence == 0
    seq = journal.append(kind=JournalRecordKind.EVENT, name="a", session_id="s")
    assert view.latest_sequence == seq
    journal.append(kind=JournalRecordKind.EVENT, name="b", session_id="s")
    assert view.latest_sequence == seq + 1


def test_session_source_progress_is_cheap_and_tracks_growth():
    """The live source must report growth via the O(1) ``progress()`` probe
    without re-reading and re-serializing the whole journal each tick.

    Regression for the WebSocket busy-poll: it previously called
    ``records()`` (full ``read()`` + ``_record_to_dict`` per record) every
    500ms just to compare a count.
    """
    from easycat.runtime import JournalView

    journal = InMemoryRingBuffer(capacity=8)

    class _StubSession:
        session_id = "stub-progress"
        is_running = True
        turn_state = "IDLE"
        _artifact_store = None

        @property
        def journal(self):
            return JournalView(journal)

    source = _session_source(_StubSession())

    # progress() must not fall back to serializing records: spy on read().
    calls: list[int] = []
    real_read = journal.read

    def _counting_read(*a, **k):
        calls.append(1)
        return real_read(*a, **k)

    journal.read = _counting_read  # type: ignore[method-assign]

    latest_seq, count = source.progress()
    assert (latest_seq, count) == (0, 0)
    journal.append(kind=JournalRecordKind.EVENT, name="a", session_id="s")
    latest_seq, count = source.progress()
    assert latest_seq == 1
    assert count == 1
    # The cheap probe must never have read/serialized the journal.
    assert calls == []


async def test_timeline_helpers_are_shared_with_cli(tmp_path):
    """The waterfall math lives once in ``debug/_turn_timeline``.

    The debugger endpoints and ``easycat bundles show`` / ``inspect``
    must compute identical per-turn spans, so the server's helpers are
    the shared functions and ``turn_waterfall`` decorates the same
    timeline with milestone deltas for the CLI.
    """
    from easycat.debug import _turn_timeline
    from easycat.debugger import server

    assert server._summarise_turns is _turn_timeline.summarise_turns
    assert server._build_timeline is _turn_timeline.build_timeline

    bundle_path = await _build_voice_bundle(tmp_path)
    records = list(RunBundle.load(bundle_path).records())
    timeline = _turn_timeline.build_timeline(records)
    waterfall = _turn_timeline.turn_waterfall(records)

    assert waterfall, "expected at least one turn"
    by_turn = {turn["turn_id"]: turn for turn in timeline}
    for turn in waterfall:
        assert turn["spans"] == by_turn[turn["turn_id"]]["spans"]
        assert set(turn["milestones"]) == {
            "vad_endpoint_to_stt_final_ms",
            "stt_final_to_agent_request_ms",
            "agent_request_to_first_token_ms",
            "agent_first_token_to_tts_first_byte_ms",
            "vad_endpoint_to_tts_first_byte_ms",
            "user_speech_start_to_bot_stopped_ms",
        }
    # A real voice turn reaches TTS, so at least one turn resolves the
    # full VAD endpoint → TTS first byte delta.
    assert any(
        turn["milestones"]["vad_endpoint_to_tts_first_byte_ms"] is not None for turn in waterfall
    )


async def test_api_timeline_carries_per_turn_milestones(tmp_path):
    """``/api/timeline`` now serves ``turn_waterfall``: each turn keeps its
    stage spans (so the SPA waterfall is unaffected) AND a ``milestones``
    block the critical-path panel renders."""
    from aiohttp.test_utils import TestClient, TestServer

    from easycat.debugger.server import _bundle_source, _make_app

    bundle_path = await _build_voice_bundle(tmp_path)
    app = _make_app(_bundle_source(bundle_path))

    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/api/timeline")).json()

    assert "timeline" in body
    assert body["timeline"], "expected at least one turn"
    for turn in body["timeline"]:
        assert "spans" in turn
        assert set(turn["milestones"]) == {
            "vad_endpoint_to_stt_final_ms",
            "stt_final_to_agent_request_ms",
            "agent_request_to_first_token_ms",
            "agent_first_token_to_tts_first_byte_ms",
            "vad_endpoint_to_tts_first_byte_ms",
            "user_speech_start_to_bot_stopped_ms",
        }
    assert any(
        turn["milestones"]["vad_endpoint_to_tts_first_byte_ms"] is not None
        for turn in body["timeline"]
    )


def test_build_issues_is_shared_with_debug_engine():
    """The server's ``_build_issues`` re-export is the ``debug/_issues`` engine."""
    from easycat.debug import _issues

    assert _build_issues is _issues.build_issues


def test_build_issues_returns_stable_rollup_shape():
    """``/api/issues`` serves the ``{issues, summary, total}`` contract."""
    records = [
        {"sequence": 1, "name": "error", "turn_id": "t1", "error": {"type": "BoomError"}},
        {"sequence": 2, "name": "stt_final", "turn_id": "t1", "data": {"text": ""}},
    ]
    report = _build_issues(records)
    assert set(report) == {"issues", "summary", "total"}
    assert report["total"] == 2
    assert report["summary"] == {"error": 1, "warning": 1, "info": 0}
    # Errors sort before warnings.
    assert [issue["severity"] for issue in report["issues"]] == ["error", "warning"]


def test_filter_records_negative_offset_raises():
    from easycat.debugger.server import _filter_records

    with pytest.raises(ValueError, match="offset"):
        _filter_records(
            [],
            stage=None,
            turn_id=None,
            name=None,
            from_seq=None,
            to_seq=None,
            offset=-1,
        )


def test_filter_records_zero_limit_raises():
    from easycat.debugger.server import _filter_records

    with pytest.raises(ValueError, match="limit"):
        _filter_records(
            [],
            stage=None,
            turn_id=None,
            name=None,
            from_seq=None,
            to_seq=None,
            limit=0,
        )


def _search_sample_records() -> list[dict]:
    return [
        {"sequence": 1, "name": "stt_final", "turn_id": "t1", "data": {"text": "hello world"}},
        {
            "sequence": 2,
            "name": "agent_error",
            "turn_id": "t1",
            "data": {},
            "error": {"type": "TimeoutError", "message": "request timed out"},
        },
        {"sequence": 3, "name": "tts_frame", "turn_id": "t2", "data": {"codec": "pcm"}},
    ]


def test_search_records_matches_data_substring():
    from easycat.debugger.server import _search_records

    matched, truncated = _search_records(_search_sample_records(), query="hello")
    assert [r["sequence"] for r in matched] == [1]
    assert matched[0]["_match_fields"] == ["data"]
    assert truncated is False


def test_search_records_matches_error_fields():
    from easycat.debugger.server import _search_records

    matched, _ = _search_records(_search_sample_records(), query="timed out")
    assert [r["sequence"] for r in matched] == [2]
    assert matched[0]["_match_fields"] == ["error"]


def test_search_records_matches_name_and_turn():
    from easycat.debugger.server import _search_records

    matched, _ = _search_records(_search_sample_records(), query="t1")
    assert [r["sequence"] for r in matched] == [1, 2]
    assert all("turn_id" in r["_match_fields"] for r in matched)


def test_search_records_regex_matches():
    from easycat.debugger.server import _search_records

    matched, _ = _search_records(_search_sample_records(), query="timeout|pcm", use_regex=True)
    assert [r["sequence"] for r in matched] == [2, 3]


def test_search_records_invalid_regex_raises():
    from easycat.debugger.server import _search_records

    with pytest.raises(ValueError, match="invalid regex"):
        _search_records(_search_sample_records(), query="[", use_regex=True)


def test_search_records_empty_query_matches_nothing():
    from easycat.debugger.server import _search_records

    matched, truncated = _search_records(_search_sample_records(), query="")
    assert matched == []
    assert truncated is False


def test_search_records_errors_only():
    from easycat.debugger.server import _search_records

    # ``t1`` appears in two records but only the error one survives ``errors_only``.
    matched, _ = _search_records(_search_sample_records(), query="t1", errors_only=True)
    assert [r["sequence"] for r in matched] == [2]


def test_search_records_does_not_mutate_source():
    from easycat.debugger.server import _search_records

    records = _search_sample_records()
    _search_records(records, query="hello")
    assert all("_match_fields" not in r for r in records)


def test_search_records_sets_scan_truncated_past_limit():
    from easycat.debugger.server import _SEARCH_SCAN_LIMIT, _search_records

    big = [
        {"sequence": i, "name": "x", "data": {"k": "match"}} for i in range(_SEARCH_SCAN_LIMIT + 5)
    ]
    matched, truncated = _search_records(big, query="match")
    assert truncated is True
    assert len(matched) == _SEARCH_SCAN_LIMIT


def test_summarise_turns_dedupes_t3_8_interrupt_fanout():
    """T3.8 fans an InterruptSignal across 8 stages, so a single
    barge-in would appear as 9 interruptions (8 control_signal + 1
    legacy interruption event).  ``_summarise_turns`` must dedupe by
    ``signal_id`` and report 1.
    """
    from easycat.debugger.server import _summarise_turns

    sig_id = "barge-1"
    records = [
        {
            "sequence": 1,
            "turn_id": "t1",
            "name": "interruption",
            "data": {},
            "timing": {"wall_ns": 1_000_000},
        },
    ]
    # Fan-out from T3.8: one control_signal per stage, all sharing
    # the same signal_id.
    for i, stage in enumerate(
        ("transport", "tts", "agent", "turn", "stt", "vad", "audio", "telephony")
    ):
        records.append(
            {
                "sequence": 2 + i,
                "turn_id": "t1",
                "kind": "control",
                "name": "control_signal",
                "data": {
                    "stage": stage,
                    "observed_stage": stage,
                    "signal_kind": "interrupt",
                    "signal_id": sig_id,
                },
                "timing": {"wall_ns": (2 + i) * 1_000_000},
            }
        )
    out = _summarise_turns(records)
    assert len(out) == 1
    assert out[0]["interruption_count"] == 1


def test_summarise_turns_counts_legacy_interruption_when_no_signals():
    """Older bundles have only the legacy ``interruption`` event with
    no ``control_signal`` records.  The counter should still find them."""
    from easycat.debugger.server import _summarise_turns

    records = [
        {
            "sequence": 1,
            "turn_id": "t1",
            "name": "interruption",
            "data": {},
            "timing": {"wall_ns": 1_000_000},
        },
    ]
    out = _summarise_turns(records)
    assert len(out) == 1
    assert out[0]["interruption_count"] == 1


def test_build_timeline_skips_control_signal_records_for_spans():
    """``control_signal`` from a barge-in shouldn't generate synthetic
    instant-spans for stages that had no real pipeline activity in
    that turn (e.g. telephony in a pure-WS session)."""
    from easycat.debugger.server import _build_timeline

    records = [
        {
            "sequence": 1,
            "turn_id": "t1",
            "name": "stage_start",
            "data": {"stage": "tts"},
            "timing": {"wall_ns": 1_000_000},
        },
        {
            "sequence": 2,
            "turn_id": "t1",
            "name": "stage_complete",
            "data": {"stage": "tts"},
            "timing": {"wall_ns": 5_000_000},
        },
        # Barge-in fan-out reaches telephony but it had no real activity.
        {
            "sequence": 3,
            "turn_id": "t1",
            "kind": "control",
            "name": "control_signal",
            "data": {
                "stage": "telephony",
                "observed_stage": "telephony",
                "signal_kind": "interrupt",
                "signal_id": "barge-1",
            },
            "timing": {"wall_ns": 6_000_000},
        },
    ]
    timeline = _build_timeline(records)
    assert len(timeline) == 1
    stage_names = [s["stage"] for s in timeline[0]["spans"]]
    assert "tts" in stage_names
    assert "telephony" not in stage_names
