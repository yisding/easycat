from __future__ import annotations

from easycat.debug._issues import IssueThresholds, _issue, build_issues


def _rec(seq: int, name: str, *, turn_id: str = "t1", wall_ms: int = 0, **data) -> dict:
    record: dict = {
        "sequence": seq,
        "name": name,
        "turn_id": turn_id,
        "timing": {"wall_ns": wall_ms * 1_000_000},
    }
    error = data.pop("error", None)
    if error is not None:
        record["error"] = error
    if data:
        record["data"] = data
    return record


def test_build_issues_empty_journal_has_stable_shape():
    report = build_issues([])
    assert report == {
        "issues": [],
        "summary": {"error": 0, "warning": 0, "info": 0},
        "total": 0,
    }


def test_build_issues_flags_record_error():
    records = [
        _rec(1, "stage_start", stage="agent"),
        _rec(2, "error", error={"type": "BoomError", "message": "kaboom"}, stage="agent"),
    ]
    report = build_issues(records)
    assert report["summary"]["error"] == 1
    assert report["total"] == 1
    (issue,) = report["issues"]
    assert issue["code"] == "record_error"
    assert issue["severity"] == "error"
    assert issue["turn_id"] == "t1"
    assert issue["sequence"] == 2
    assert issue["stage"] == "agent"
    assert "BoomError" in issue["title"]


def test_build_issues_flags_tool_failure_and_timeout_by_name():
    records = [
        _rec(1, "tool_call_failed", tool_name="calc", stage="agent"),
        _rec(2, "provider_timeout", stage="tts"),
    ]
    report = build_issues(records)
    codes = {issue["code"] for issue in report["issues"]}
    assert codes == {"tool_call_failed", "timeout"}
    assert report["summary"]["error"] == 2


def test_build_issues_ignores_non_string_record_names():
    records = [
        {"sequence": 1, "name": ["tool_call_failed"], "data": {}},
        {"sequence": 2, "name": {"event": "provider_timeout"}, "data": {}},
        {
            "sequence": 3,
            "name": ["bot_stopped_speaking"],
            "turn_id": "t1",
            "timing": {"wall_ns": 0},
        },
    ]

    report = build_issues(records)

    assert report == {
        "issues": [],
        "summary": {"error": 0, "warning": 0, "info": 0},
        "total": 0,
    }


def test_build_issues_with_artifact_resolver_ignores_non_string_record_names():
    records = [
        {
            "sequence": 1,
            "name": ["stage_start"],
            "turn_id": "t1",
            "timing": {"wall_ns": 0},
            "data": {"stage": "stt"},
        },
        {
            "sequence": 2,
            "name": {"event": "tts_frame"},
            "turn_id": "t1",
            "timing": {"wall_ns": 4_000_000_000},
        },
    ]

    report = build_issues(records, artifact_resolver=lambda _ref: None)

    assert report == {
        "issues": [],
        "summary": {"error": 0, "warning": 0, "info": 0},
        "total": 0,
    }


def test_build_issues_flags_empty_stt_final_as_warning():
    records = [
        _rec(1, "stt_final", text="   "),
        _rec(2, "stt_final", turn_id="t2", text="real words"),
    ]
    report = build_issues(records)
    warnings = [issue for issue in report["issues"] if issue["code"] == "empty_stt_final"]
    assert len(warnings) == 1
    assert warnings[0]["severity"] == "warning"
    assert warnings[0]["turn_id"] == "t1"


def test_build_issues_flags_slow_milestone():
    # vad_stop_speaking → stt_final delta of 3000ms exceeds the 1500ms budget.
    records = [
        _rec(1, "vad_stop_speaking", wall_ms=0),
        _rec(2, "stt_final", wall_ms=3000, text="hi"),
    ]
    report = build_issues(records)
    slow = [issue for issue in report["issues"] if issue["code"] == "slow_milestone"]
    assert slow, "expected a slow_milestone issue"
    issue = slow[0]
    assert issue["metric"] == "vad_endpoint_to_stt_final_ms"
    assert issue["value"] == 3000.0
    assert issue["threshold"] == 1500.0
    assert issue["stage"] == "stt"


def test_build_issues_flags_slow_turn():
    records = [
        _rec(1, "turn_started", wall_ms=0),
        _rec(2, "turn_ended", wall_ms=15_000),
    ]
    report = build_issues(records)
    slow = [issue for issue in report["issues"] if issue["code"] == "slow_turn"]
    assert slow
    assert slow[0]["value"] == 15_000.0
    assert slow[0]["threshold"] == IssueThresholds().slow_turn_wall_ms


def test_build_issues_sorts_errors_before_warnings():
    records = [
        _rec(1, "stt_final", text=""),  # warning
        _rec(2, "error", error={"type": "BoomError"}),  # error
        _rec(3, "timeout"),  # error
    ]
    report = build_issues(records)
    severities = [issue["severity"] for issue in report["issues"]]
    assert severities == ["error", "error", "warning"]


def test_build_issues_milestone_uses_wp3_split_keys():
    thresholds = IssueThresholds()
    metrics = {check[0] for check in thresholds.latency_checks}
    assert "stt_final_to_agent_request_ms" in metrics
    assert "agent_request_to_first_token_ms" in metrics
    # The pre-split combined key must be gone.
    assert "stt_final_to_agent_first_token_ms" not in metrics


def test_issue_card_has_stable_field_set():
    card = _issue(code="x", severity="info", title="t", detail="d")
    assert set(card) == {
        "code",
        "severity",
        "title",
        "detail",
        "turn_id",
        "sequence",
        "stage",
        "metric",
        "value",
        "threshold",
    }


def test_issue_thresholds_is_frozen():
    import dataclasses

    import pytest

    thresholds = IssueThresholds()
    assert dataclasses.is_dataclass(thresholds)
    with pytest.raises(dataclasses.FrozenInstanceError):
        thresholds.slow_turn_wall_ms = 1.0  # type: ignore[misc]
