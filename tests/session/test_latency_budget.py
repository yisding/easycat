"""Unit tests for the runtime :class:`LatencyBudgetMonitor` and stage mapping.

These cover the M12 reconciliation of the three latency vocabularies: the flat
runtime metric names, their ``_ms``/``_latency_ms`` suffix forms, and the
waterfall ``*_to_*_ms`` milestone names that lift onto the same flat stage.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from easycat.runtime.records import JournalRecordKind
from easycat.session._latency_budget import LatencyBudgetMonitor, _budget_matches_stage


@dataclass
class _Record:
    name: str
    kind: JournalRecordKind
    turn_id: str | None
    data: dict


class _FakeSink:
    def __init__(self) -> None:
        self.records: list[_Record] = []

    def append_record(
        self,
        *,
        name: str,
        kind: JournalRecordKind = JournalRecordKind.EVENT,
        turn_id: str | None = None,
        data: dict | None = None,
    ) -> None:
        self.records.append(_Record(name=name, kind=kind, turn_id=turn_id, data=dict(data or {})))


@pytest.mark.parametrize(
    ("observed_stage", "budget_stage"),
    [
        ("stt_final_latency_ms", "stt_final_latency_ms"),
        ("stt_final_latency_ms", "vad_endpoint_to_stt_final_ms"),
        ("llm_ttft_ms", "llm_ttft_ms"),
        ("llm_ttft_ms", "agent_request_to_first_token_ms"),
        ("tts_ttfb_ms", "tts_ttfb_ms"),
        ("tts_ttfb_ms", "agent_first_token_to_tts_first_byte_ms"),
        ("first_audio_ms", "first_audio_ms"),
        ("first_audio_ms", "vad_endpoint_to_tts_first_byte_ms"),
        ("barge_in_ack_ms", "user_speech_start_to_bot_stopped_ms"),
        ("total_ms", "total_ms"),
        ("total", "total_ms"),
        ("stt_final", "stt_final_latency_ms"),
    ],
)
def test_budget_matches_stage_maps_flat_and_waterfall_names(
    observed_stage: str, budget_stage: str
) -> None:
    assert _budget_matches_stage(observed_stage, budget_stage)


@pytest.mark.parametrize(
    ("observed_stage", "budget_stage"),
    [
        ("stt_final_latency_ms", "tts_ttfb_ms"),
        ("first_audio_ms", "agent_first_token_to_tts_first_byte_ms"),
        ("llm_ttft_ms", None),
        ("total_ms", "first_audio_ms"),
    ],
)
def test_budget_matches_stage_rejects_unrelated_stages(
    observed_stage: str, budget_stage: object
) -> None:
    assert not _budget_matches_stage(observed_stage, budget_stage)


def test_monitor_records_metric_without_violation() -> None:
    sink = _FakeSink()
    monitor = LatencyBudgetMonitor(
        journal_sink=sink,
        budgets=({"stage": "first_audio_ms", "max_ms": 1000.0},),
    )

    monitor.record_metric(
        name="first_audio_ms",
        turn_id="turn-1",
        stage="first_audio_ms",
        observed_ms=500.0,
        data={"from": "turn_ended", "to": "tts_first_byte"},
    )

    assert [record.name for record in sink.records] == ["first_audio_ms"]
    metric = sink.records[0]
    assert metric.kind is JournalRecordKind.METRIC
    assert metric.data["value"] == 500.0
    assert "latency_budget_exceeded" not in metric.data


def test_monitor_records_budget_exceeded_for_waterfall_named_budget() -> None:
    sink = _FakeSink()
    # The budget is expressed with the waterfall milestone name; the runtime
    # record uses the flat stage. They must still match (M12 reconciliation).
    monitor = LatencyBudgetMonitor(
        journal_sink=sink,
        budgets=({"stage": "vad_endpoint_to_stt_final_ms", "max_ms": 10.0},),
    )

    monitor.record_metric(
        name="stt_final_latency_ms",
        turn_id="turn-2",
        stage="stt_final_latency_ms",
        observed_ms=42.0,
        data={"from": "turn_ended", "to": "stt_final"},
    )

    metric = next(record for record in sink.records if record.name == "stt_final_latency_ms")
    assert metric.data["latency_budget_exceeded"] is True
    assert metric.data["latency_budget_violations"] == [
        {
            "stage": "vad_endpoint_to_stt_final_ms",
            "observed_ms": 42.0,
            "budget_ms": 10.0,
            "percentile": "p95",
            "scope": "turn_metric",
        }
    ]
    alert = next(record for record in sink.records if record.name == "latency_budget_exceeded")
    assert alert.turn_id == "turn-2"
    assert alert.data["trigger_record_name"] == "stt_final_latency_ms"
    assert alert.data["stage"] == "vad_endpoint_to_stt_final_ms"


def test_monitor_has_budget_for_flat_and_waterfall_names() -> None:
    monitor = LatencyBudgetMonitor(
        journal_sink=_FakeSink(),
        budgets=({"stage": "agent_first_token_to_tts_first_byte_ms", "max_ms": 1.0},),
    )
    assert monitor.has_budget_for("tts_ttfb_ms")
    assert not monitor.has_budget_for("first_audio_ms")
