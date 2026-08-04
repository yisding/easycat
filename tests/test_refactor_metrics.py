"""Contracts for the bug-resistant refactor measurement inputs."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from scripts.refactor_metrics import (
    ChangedFile,
    CommitRecord,
    build_report,
    format_utc,
    main,
    parse_utc,
    render_report_json,
    render_report_markdown,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
METRICS_DIR = REPO_ROOT / "plan" / "metrics"
FIXTURE_COMPLETION = parse_utc("2026-03-02T00:00:00Z")
FIXTURE_AS_OF = parse_utc("2026-05-02T00:00:00Z")
WS2_REQUIRED_COMMANDS = [
    (
        "uv run pytest tests/runtime/test_scope.py "
        "tests/session/test_session_lifecycle_teardown.py "
        "tests/integration/test_session_lifecycle_e2e.py -q"
    ),
    "just check",
]
REQUIRED_INCIDENT_SEARCHES = [
    "issues",
    "regression_prs",
    "reverts",
    "release_blocking_ci",
]
REQUIRED_GITHUB_CHECKS = ["CodeRabbit", "GitGuardian Security Checks"]


def _load(name: str) -> dict[str, Any]:
    return json.loads((METRICS_DIR / name).read_text(encoding="utf-8"))


def _assert_paths_exist(entries: list[dict[str, Any]], *, allow_planned: bool = False) -> None:
    for entry in entries:
        paths = entry["paths"]
        assert paths, entry
        for path in paths:
            assert path.startswith("src/easycat/"), path
            if not (allow_planned and entry.get("planned", False)):
                assert (REPO_ROOT / path).is_file(), path


def _sha(number: int) -> str:
    return f"{number:040x}"


def _commit(
    number: int,
    committed_at: str,
    *paths: str,
) -> CommitRecord:
    return CommitRecord(
        sha=_sha(number),
        committed_at=parse_utc(committed_at),
        files=tuple(ChangedFile(path=path, additions=1, deletions=1) for path in paths),
    )


def _classification(
    number: int,
    classification: str,
    *,
    affected_members: list[str] | None = None,
) -> dict[str, Any]:
    is_fix = classification == "fix"
    return {
        "cohort_id": "fixture_cohort",
        "sha": _sha(number),
        "classification": classification,
        "bug_classes": ["fixture_bug"] if is_fix else [],
        "affected_members": affected_members or [],
        "evidence": ["fixture evidence"],
        "rationale": "fixture adjudication",
        "reviewer": "fixture-reviewer",
        "reviewed_at": "2026-05-02T00:00:00Z",
    }


def _pending_incidents() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "rubric_version": 1,
        "required_verification": {
            "commands": WS2_REQUIRED_COMMANDS,
            "github_checks": REQUIRED_GITHUB_CHECKS,
            "incident_searches": REQUIRED_INCIDENT_SEARCHES,
        },
        "vertical_slice_gate": {
            "status": "pending",
            "completion_sha": None,
            "completion_date": None,
            "review": {
                "status": "pending",
                "reviewer": None,
                "reviewed_at": None,
                "commands": [],
                "github_checks": [],
                "incident_searches": [],
                "evidence": [],
            },
        },
        "incidents": [],
    }


def _fixture_inputs() -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    list[CommitRecord],
]:
    pre_window = {
        "start": format_utc(FIXTURE_COMPLETION - timedelta(days=60)),
        "end": format_utc(FIXTURE_COMPLETION),
    }
    post_window = {
        "start": format_utc(FIXTURE_COMPLETION),
        "end": format_utc(FIXTURE_COMPLETION + timedelta(days=60)),
    }
    manifest = {
        "schema_version": 1,
        "method": {
            "pre_window_days": 60,
            "post_window_days": 60,
            "recurrence_window_days": 7,
        },
        "cohorts": [
            {
                "id": "fixture_cohort",
                "gate": "fixture_gate",
                "bug_classes": ["fixture_bug"],
                "members": [
                    {"id": "member_a", "paths": ["src/easycat/member_a.py"]},
                    {"id": "member_b", "paths": ["src/easycat/member_b.py"]},
                ],
                "controls": [
                    {
                        "id": "control_a",
                        "paths": ["src/easycat/control_a.py"],
                        "invalidated_by": None,
                    }
                ],
                "minimum_exposure": {
                    "treated_touching_commits": 1,
                    "treated_changed_lines": 1,
                    "pooled_control_touching_commits": 1,
                    "pooled_control_changed_lines": 1,
                },
                "non_inferiority_epsilon": 0.01,
                "anchor": {
                    "status": "active",
                    "completion_sha": _sha(4),
                    "completion_date": format_utc(FIXTURE_COMPLETION),
                    "migration_commits": [_sha(4)],
                    "pre_window": pre_window,
                    "post_window": post_window,
                    "superseded_anchors": [],
                },
            }
        ],
    }
    adjudications = {
        "schema_version": 1,
        "commit_classifications": [
            _classification(2, "fix", affected_members=["member_a"]),
            _classification(3, "not_fix"),
            _classification(5, "not_fix"),
            _classification(6, "not_fix"),
            _classification(8, "fix", affected_members=["member_b"]),
            _classification(9, "not_fix"),
        ],
        "recurrence_adjudications": [],
    }
    incidents = _pending_incidents()
    history = [
        _commit(1, "2025-12-31T23:59:59Z", "src/easycat/member_a.py"),
        _commit(2, "2026-01-01T00:00:00Z", "src/easycat/member_a.py"),
        _commit(3, "2026-01-02T00:00:00Z", "src/easycat/control_a.py"),
        _commit(4, "2026-03-02T00:00:00Z", "src/easycat/member_a.py"),
        _commit(9, "2026-03-02T00:00:00Z", "src/easycat/member_a.py"),
        _commit(5, "2026-03-03T00:00:00Z", "src/easycat/member_a.py"),
        _commit(6, "2026-03-04T00:00:00Z", "src/easycat/control_a.py"),
        _commit(
            8,
            "2026-03-10T00:00:00Z",
            "src/easycat/member_b.py",
            "src/easycat/control_a.py",
        ),
        _commit(7, "2026-05-01T00:00:00Z", "src/easycat/member_a.py"),
    ]
    return manifest, adjudications, incidents, history


def test_refactor_family_manifest_freezes_measurement_contract() -> None:
    manifest = _load("refactor-families.json")

    assert manifest["schema_version"] == 1
    assert manifest["preregistered_at"] == "2026-08-02"
    assert manifest["method"] == {
        "history": "first_parent",
        "diff_parent": "first_parent",
        "timestamp": "committer_timestamp_utc",
        "window_boundary": "[start,end)",
        "pre_window_days": 60,
        "post_window_days": 60,
        "recurrence_window_days": 7,
        "migration_exclusion": "exact_sha_only",
        "control_pooling": "deduplicate_commit_sha",
        "zero_denominator_result": "insufficient_data",
        "incomplete_adjudication_result": "insufficient_data",
    }

    bug_classes = manifest["bug_classes"]
    bug_class_ids = {entry["id"] for entry in bug_classes}
    assert len(bug_class_ids) == len(bug_classes)
    assert bug_class_ids == {
        "lifecycle_cancellation",
        "peer_fix_divergence",
        "staleness_fencing",
    }
    assert all(entry["name"] and entry["criterion"] for entry in bug_classes)

    cohorts = manifest["cohorts"]
    cohort_ids = {cohort["id"] for cohort in cohorts}
    assert len(cohort_ids) == len(cohorts)
    assert cohort_ids == {
        "tier_a_session_lifecycle_staleness",
        "tier_b_agent_bridge_lifecycle",
        "tier_b_transport_lifecycle",
    }

    for cohort in cohorts:
        assert cohort["treatment_slices"]
        assert set(cohort["bug_classes"]) <= bug_class_ids
        assert cohort["controls"]
        _assert_paths_exist(cohort["controls"])

        exposure = cohort["minimum_exposure"]
        assert set(exposure) == {
            "treated_touching_commits",
            "treated_changed_lines",
            "pooled_control_touching_commits",
            "pooled_control_changed_lines",
        }
        assert all(isinstance(value, int) and value > 0 for value in exposure.values())
        assert 0 <= cohort["non_inferiority_epsilon"] < 1
        assert cohort["attribution_reviewer"]

        anchor = cohort["anchor"]
        assert anchor["completion_sha"] is None
        assert anchor["completion_date"] is None
        assert anchor["migration_commits"] == []
        assert anchor["pre_window"] is None
        assert anchor["post_window"] is None
        assert anchor["superseded_anchors"] == []

        if cohort["status"] == "preregistered":
            assert anchor["status"] == "pending"
            assert cohort["members"]
            _assert_paths_exist(cohort["members"], allow_planned=True)
            assert "candidate_members" not in cohort
            assert "member_selection" not in cohort
        else:
            assert cohort["status"] == "blocked_peer_decision"
            assert anchor["status"] == "blocked"
            assert cohort["members"] == []
            assert cohort["candidate_members"]
            _assert_paths_exist(cohort["candidate_members"])
            selection = cohort["member_selection"]
            assert selection["source"] == "peer-set ADR"
            assert selection["decision_sha"] is None
            assert selection["locked_at"] is None


def test_refactor_metric_review_inputs_start_empty_and_versioned() -> None:
    adjudications = _load("adjudications.json")
    incidents = _load("incidents.json")

    assert adjudications == {
        "schema_version": 1,
        "commit_classifications": [],
        "recurrence_adjudications": [],
    }
    assert incidents == _pending_incidents()


def test_report_uses_exact_windows_migration_exclusions_and_group_assignment() -> None:
    manifest, adjudications, incidents, history = _fixture_inputs()

    report = build_report(
        manifest,
        adjudications,
        incidents,
        history,
        as_of=FIXTURE_AS_OF,
    )

    cohort = report["cohorts"][0]
    assert cohort["result"] == "pass"
    assert cohort["windows"]["pre"]["treated"]["touching_commits"] == [_sha(2)]
    assert cohort["windows"]["pre"]["pooled_control"]["touching_commits"] == [_sha(3)]
    assert cohort["windows"]["post"]["treated"]["touching_commits"] == [
        _sha(9),
        _sha(5),
        _sha(8),
    ]
    assert cohort["windows"]["post"]["pooled_control"]["touching_commits"] == [
        _sha(6),
        _sha(8),
    ]
    assert cohort["windows"]["post"]["treated"]["fix_commits"] == [_sha(8)]
    assert cohort["windows"]["post"]["pooled_control"]["fix_commits"] == []
    measured_shas = {
        sha
        for window in cohort["windows"].values()
        for group in ("treated", "pooled_control")
        for sha in window[group]["touching_commits"]
    }
    assert _sha(1) not in measured_shas
    assert _sha(4) not in measured_shas
    assert _sha(7) not in measured_shas
    assert _sha(9) in measured_shas


def test_report_treats_missing_adjudication_and_zero_exposure_as_insufficient() -> None:
    manifest, adjudications, incidents, history = _fixture_inputs()
    adjudications["commit_classifications"] = [
        entry for entry in adjudications["commit_classifications"] if entry["sha"] != _sha(2)
    ]

    missing = build_report(
        manifest,
        adjudications,
        incidents,
        history,
        as_of=FIXTURE_AS_OF,
    )["cohorts"][0]
    assert missing["result"] == "insufficient_data"
    assert f"classification_missing:{_sha(2)}" in missing["reasons"]

    zero = build_report(
        manifest,
        {"commit_classifications": [], "recurrence_adjudications": []},
        incidents,
        [history[3]],
        as_of=FIXTURE_AS_OF,
    )["cohorts"][0]
    assert zero["result"] == "insufficient_data"
    assert "zero_denominator:pre:treated" in zero["reasons"]
    assert "zero_denominator:post:pooled_control" in zero["reasons"]
    assert "underexposed:post:treated_touching_commits" in zero["reasons"]


def test_report_invalidates_registered_control_without_replacement() -> None:
    manifest, adjudications, incidents, history = _fixture_inputs()
    manifest["cohorts"][0]["controls"][0]["invalidated_by"] = _sha(99)

    cohort = build_report(
        manifest,
        adjudications,
        incidents,
        history,
        as_of=FIXTURE_AS_OF,
    )["cohorts"][0]

    assert cohort["result"] == "insufficient_data"
    assert f"control_invalidated:control_a:{_sha(99)}" in cohort["reasons"]


def test_recurrence_candidate_is_stable_and_exact_adjudication_can_fail_gate() -> None:
    manifest, adjudications, incidents, history = _fixture_inputs()
    for entry in adjudications["commit_classifications"]:
        if entry["sha"] == _sha(5):
            entry.update(
                classification="fix",
                bug_classes=["fixture_bug"],
                affected_members=["member_a"],
            )

    unreviewed = build_report(
        manifest,
        adjudications,
        incidents,
        history,
        as_of=FIXTURE_AS_OF,
    )["cohorts"][0]
    candidate = unreviewed["recurrence_candidates"][0]
    assert unreviewed["result"] == "insufficient_data"
    assert candidate["commit_shas"] == [_sha(5), _sha(8)]
    assert candidate["affected_members"] == ["member_a", "member_b"]
    assert f"recurrence_adjudication_missing:{candidate['candidate_id']}" in unreviewed["reasons"]

    reversed_report = build_report(
        manifest,
        {**adjudications, "commit_classifications": adjudications["commit_classifications"][::-1]},
        incidents,
        history[::-1],
        as_of=FIXTURE_AS_OF,
    )["cohorts"][0]
    assert reversed_report["recurrence_candidates"][0]["candidate_id"] == candidate["candidate_id"]

    adjudications["recurrence_adjudications"] = [
        {
            "candidate_id": candidate["candidate_id"],
            "cohort_id": "fixture_cohort",
            "bug_class": "fixture_bug",
            "commit_shas": [_sha(5), _sha(8)],
            "verdict": "same_fix",
            "evidence": ["linked fixture diffs"],
            "rationale": "one repeated logical fix",
            "reviewer": "fixture-reviewer",
            "reviewed_at": "2026-05-02T00:00:00Z",
        }
    ]
    reviewed = build_report(
        manifest,
        adjudications,
        incidents,
        history,
        as_of=FIXTURE_AS_OF,
    )["cohorts"][0]
    assert reviewed["result"] == "fail"
    assert "post_multi_member_recurrence" in reviewed["reasons"]


def test_report_json_and_markdown_are_stable_for_normalized_input_order() -> None:
    manifest, adjudications, incidents, history = _fixture_inputs()
    first = build_report(
        manifest,
        adjudications,
        incidents,
        history,
        as_of=FIXTURE_AS_OF,
    )
    second = build_report(
        manifest,
        {**adjudications, "commit_classifications": adjudications["commit_classifications"][::-1]},
        incidents,
        history[::-1],
        as_of=FIXTURE_AS_OF,
    )

    assert render_report_json(first) == render_report_json(second)
    assert render_report_markdown(first) == render_report_markdown(second)


def test_vertical_slice_gate_requires_acceptance_evidence_and_applies_incident_rubric() -> None:
    manifest: dict[str, Any] = {"method": {}, "cohorts": []}
    pending = _pending_incidents()
    assert (
        build_report(manifest, {}, pending, [], as_of=FIXTURE_AS_OF)["vertical_slice_gate"][
            "result"
        ]
        == "insufficient_data"
    )

    active = _pending_incidents()
    active["vertical_slice_gate"] = {
        "status": "active",
        "completion_sha": _sha(20),
        "completion_date": "2026-01-01T00:00:00Z",
        "review": {
            "status": "pending",
            "reviewer": None,
            "reviewed_at": None,
            "commands": [],
            "github_checks": [],
            "incident_searches": [],
            "evidence": [],
        },
    }
    gate_history = [_commit(20, "2026-01-01T00:00:00Z")]
    review_pending = build_report(manifest, {}, active, gate_history, as_of=FIXTURE_AS_OF)[
        "vertical_slice_gate"
    ]
    assert review_pending["result"] == "insufficient_data"
    assert "vertical_slice_review_incomplete" in review_pending["reasons"]

    active["vertical_slice_gate"]["review"] = {
        "status": "complete",
        "reviewer": "fixture-reviewer",
        "reviewed_at": "2026-01-01T00:00:00Z",
        "commands": WS2_REQUIRED_COMMANDS,
        "github_checks": [
            {
                "name": "CodeRabbit",
                "conclusion": "success",
                "url": "https://example.test/check/1",
            },
            {
                "name": "GitGuardian Security Checks",
                "conclusion": "success",
                "url": "https://example.test/check/2",
            },
        ],
        "incident_searches": REQUIRED_INCIDENT_SEARCHES,
        "evidence": ["merge-anchor review"],
    }
    reviewed_empty = build_report(manifest, {}, active, gate_history, as_of=FIXTURE_AS_OF)[
        "vertical_slice_gate"
    ]
    assert reviewed_empty["result"] == "pass"

    review = active["vertical_slice_gate"]["review"]
    review["commands"] = WS2_REQUIRED_COMMANDS[:-1]
    missing_command = build_report(manifest, {}, active, gate_history, as_of=FIXTURE_AS_OF)[
        "vertical_slice_gate"
    ]
    assert "vertical_slice_required_commands_missing" in missing_command["reasons"]
    review["commands"] = WS2_REQUIRED_COMMANDS

    review["github_checks"][0]["conclusion"] = "failure"
    failed_check = build_report(manifest, {}, active, gate_history, as_of=FIXTURE_AS_OF)[
        "vertical_slice_gate"
    ]
    assert "vertical_slice_github_checks_incomplete" in failed_check["reasons"]
    review["github_checks"][0]["conclusion"] = "success"

    review["incident_searches"] = REQUIRED_INCIDENT_SEARCHES[:-1]
    missing_search = build_report(manifest, {}, active, gate_history, as_of=FIXTURE_AS_OF)[
        "vertical_slice_gate"
    ]
    assert "vertical_slice_incident_searches_missing" in missing_search["reasons"]
    review["incident_searches"] = REQUIRED_INCIDENT_SEARCHES

    active["vertical_slice_gate"]["completion_date"] = "2026-01-01T00:00:01Z"
    mismatched_anchor = build_report(
        manifest,
        {},
        active,
        gate_history,
        as_of=FIXTURE_AS_OF,
    )["vertical_slice_gate"]
    assert "vertical_slice_anchor_date_mismatch" in mismatched_anchor["reasons"]
    active["vertical_slice_gate"]["completion_date"] = "2026-01-01T00:00:00Z"

    incident = {
        "id": "fixture-incident",
        "severity": "P2",
        "cohort_id": "fixture_cohort",
        "slice": "WS2.1a",
        "occurred_at": "2026-01-01T00:00:00Z",
        "source": "fixture issue",
        "evidence": ["fixture reproduction"],
        "attribution": "attributable",
        "rationale": "introduced by fixture slice",
        "reviewer": "fixture-reviewer",
        "reviewed_at": "2026-01-01T00:00:00Z",
    }
    active["incidents"] = [incident]
    attributable = build_report(manifest, {}, active, gate_history, as_of=FIXTURE_AS_OF)[
        "vertical_slice_gate"
    ]
    assert attributable["result"] == "fail"

    incident["attribution"] = "disputed"
    disputed = build_report(manifest, {}, active, gate_history, as_of=FIXTURE_AS_OF)[
        "vertical_slice_gate"
    ]
    assert disputed["result"] == "insufficient_data"
    assert "incident_disputed:fixture-incident" in disputed["reasons"]


def test_checked_report_artifacts_have_a_versioned_schema_and_do_not_drift() -> None:
    schema = _load("report.schema.json")
    report = _load("report.json")
    markdown = (METRICS_DIR / "report.md").read_text(encoding="utf-8")

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["properties"]["schema_version"] == {"const": 2}
    assert set(schema["required"]) == {
        "schema_version",
        "as_of",
        "cohorts",
        "vertical_slice_gate",
    }
    assert set(report) == set(schema["required"])
    assert markdown.startswith("# Refactor outcome report\n")
    assert "## WS2.1a vertical-slice acceptance gate" in markdown
    assert (
        main(
            [
                "--repo",
                str(REPO_ROOT),
                "--as-of",
                "2026-08-02T00:00:00Z",
                "--check",
            ]
        )
        == 0
    )
