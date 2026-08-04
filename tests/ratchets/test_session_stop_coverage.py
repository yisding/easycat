"""Freeze the reviewed WS2.7a coverage map for ``Session.stop()``."""

from __future__ import annotations

import ast
import json
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = Path(__file__).with_name("session-stop-coverage.json")

EXPECTED_CLASS_BY_ID = {
    "audio_provider_ownership": "ownership",
    "cancel_resistant_start_cannot_resurrect": "entry",
    "cancelled_stop_blocks_restart": "entry",
    "context_exit_uses_force": "entry",
    "debug_backends_finalize_and_close": "postmortem",
    "emergency_exporter_is_released": "ownership",
    "external_outbound_queue_survives": "ownership",
    "failed_stop_blocks_restart": "entry",
    "failed_stop_releases_owned_handlers": "entry",
    "force_cancels_application_prompt": "turn_work",
    "force_cancels_in_progress_start": "entry",
    "force_signals_task_cohort_before_await": "ordering",
    "force_stt_pause_commit_is_cancelled": "ordering",
    "force_supersedes_hung_graceful": "entry",
    "graceful_barge_cleanup_precedes_provider_teardown": "ordering",
    "graceful_drains_application_prompt": "turn_work",
    "graceful_rejects_new_application_prompt": "turn_work",
    "graceful_stt_pause_commit_is_drained": "ordering",
    "idempotent_stop": "entry",
    "joined_stop_preserves_caller_cancellation": "entry",
    "postmortem_sqlite_view_remains_readable": "postmortem",
    "preemptive_generation_precedes_agent_close": "turn_work",
    "runtime_owned_stop_does_not_self_cancel": "entry",
    "start_stop_basic": "entry",
    "stop_common_finalizer_partial_order": "ordering",
    "text_turn_rechecks_stop_admission": "turn_work",
    "wait_closed_observes_stop": "entry",
}


def test_session_stop_coverage_manifest_is_complete_and_points_to_tests() -> None:
    manifest = _load_manifest()
    assert manifest["version"] == 1
    assert manifest["scope"] == "easycat.session._session.Session.stop"
    assert manifest["update_rationale"]

    scenarios = manifest["scenarios"]
    by_id = {scenario["id"]: scenario for scenario in scenarios}
    assert len(by_id) == len(scenarios)
    assert set(by_id) == set(EXPECTED_CLASS_BY_ID)
    assert all(
        scenario["class"] == EXPECTED_CLASS_BY_ID[scenario_id]
        for scenario_id, scenario in by_id.items()
    )
    assert manifest["counts"] == dict(
        sorted(Counter(scenario["class"] for scenario in scenarios).items())
    )

    seen_evidence: set[str] = set()
    for scenario in scenarios:
        assert isinstance(scenario["contract"], str) and scenario["contract"].strip()
        evidence = scenario["evidence"]
        assert isinstance(evidence, list) and evidence
        for node_id in evidence:
            assert node_id not in seen_evidence, f"duplicate coverage evidence: {node_id}"
            seen_evidence.add(node_id)
            _assert_test_exists(node_id)


def _load_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _assert_test_exists(node_id: str) -> None:
    relative_path, separator, test_name = node_id.partition("::")
    assert separator and test_name.startswith("test_"), f"invalid pytest node id: {node_id}"
    path = REPO_ROOT / relative_path
    assert path.is_relative_to(REPO_ROOT / "tests"), f"evidence escapes tests/: {node_id}"
    assert path.is_file(), f"missing evidence file: {node_id}"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assert any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == test_name
        for node in tree.body
    ), f"missing evidence test: {node_id}"
