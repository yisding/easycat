"""Contracts for the bug-resistant refactor measurement inputs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
METRICS_DIR = REPO_ROOT / "plan" / "metrics"


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
    assert incidents == {
        "schema_version": 1,
        "rubric_version": 1,
        "soak_window_days": 14,
        "incidents": [],
    }
