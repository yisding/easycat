"""Freeze WS3.1 bridge lifecycle applicability and pre-harness coverage."""

from __future__ import annotations

import ast
import json
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "tests" / "integrations" / "agents" / "bridge-lifecycle-matrix.json"

BRIDGES = frozenset(
    {
        "generic_workflow",
        "langchain",
        "langgraph",
        "llama_agents",
        "openai_agents",
        "pydantic_ai",
        "remote_responses_api",
    }
)
SCENARIOS = frozenset(
    {
        "interruption_prior_turn_isolation",
        "recorder_transient_cleanup",
        "stream_close_cleanup",
        "tool_inflight_cancellation_drain",
        "unknown_event_tolerance",
    }
)
POSTCONDITIONS = {
    "reset_isolation": "reset() clears state introduced by the scenario",
    "snapshot_json_safe": "snapshot_state() remains JSON-serializable after the scenario",
}


def test_bridge_lifecycle_matrix_is_a_complete_classified_cross_product() -> None:
    manifest = _load_manifest()
    assert manifest["version"] == 1
    assert manifest["update_rationale"]
    assert set(manifest["bridges"]) == BRIDGES
    assert set(manifest["scenarios"]) == SCENARIOS
    assert manifest["after_each_applicable_scenario"] == POSTCONDITIONS

    cells = manifest["cells"]
    assert set(cells) == BRIDGES
    assert all(set(bridge_cells) == SCENARIOS for bridge_cells in cells.values())

    applicability: Counter[str] = Counter()
    coverage: Counter[str] = Counter()
    for bridge, bridge_cells in cells.items():
        for scenario, cell in bridge_cells.items():
            prefix = f"{bridge}/{scenario}"
            assert cell["applicability"] in {"required", "not_applicable"}, prefix
            assert cell["coverage"] in {"existing", "missing", "not_applicable"}, prefix
            assert isinstance(cell["rationale"], str) and cell["rationale"].strip(), prefix
            evidence = cell["evidence"]
            assert isinstance(evidence, list), prefix

            if cell["applicability"] == "not_applicable":
                assert cell["coverage"] == "not_applicable", prefix
                assert evidence == [], prefix
            elif cell["coverage"] == "existing":
                assert evidence, prefix
                for node_id in evidence:
                    _assert_test_exists(node_id)
            else:
                assert cell["coverage"] == "missing", prefix
                assert evidence == [], prefix

            applicability[cell["applicability"]] += 1
            coverage[cell["coverage"]] += 1

    assert manifest["counts"] == {
        "applicability": dict(sorted(applicability.items())),
        "coverage": dict(sorted(coverage.items())),
    }


def _load_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _assert_test_exists(node_id: str) -> None:
    parts = node_id.split("::")
    assert len(parts) >= 2, f"invalid pytest node id: {node_id}"
    path = REPO_ROOT / parts[0]
    assert path.is_relative_to(REPO_ROOT / "tests"), f"evidence escapes tests/: {node_id}"
    assert path.is_file(), f"missing evidence file: {node_id}"

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    body: list[ast.stmt] = tree.body
    for index, name in enumerate(parts[1:]):
        node = next(
            (
                candidate
                for candidate in body
                if isinstance(candidate, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and candidate.name == name
            ),
            None,
        )
        assert node is not None, f"missing evidence node: {node_id}"
        if index < len(parts[1:]) - 1:
            assert isinstance(node, ast.ClassDef), f"non-class evidence parent: {node_id}"
            body = node.body
        else:
            assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)), (
                f"evidence leaf is not a test: {node_id}"
            )
            assert node.name.startswith("test_"), f"evidence leaf is not a test: {node_id}"
