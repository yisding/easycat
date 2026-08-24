"""Freeze WS4.1 transport lifecycle applicability and pre-harness coverage."""

from __future__ import annotations

import ast
import json
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "tests" / "transports" / "transport-lifecycle-matrix.json"

TRANSPORTS = frozenset({"local", "telnyx", "twilio", "webrtc", "websocket", "webtransport"})
SCENARIOS = frozenset(
    {
        "connect_leadership_race",
        "degraded_emission",
        "disconnect_during_connect",
        "interrupted_disconnect_publication",
        "late_frames",
        "mid_stream_teardown",
        "queue_overflow",
        "startup_rollback",
    }
)
LIFECYCLE_TEST_METHODS = frozenset(f"test_{scenario}" for scenario in SCENARIOS)


def test_transport_lifecycle_matrix_is_a_complete_classified_cross_product() -> None:
    manifest = _load_manifest()
    assert manifest["version"] == 1
    assert manifest["update_rationale"]
    assert set(manifest["transports"]) == TRANSPORTS
    assert set(manifest["scenarios"]) == SCENARIOS
    assert all(manifest["scenarios"][scenario].strip() for scenario in SCENARIOS)

    cells = manifest["cells"]
    assert set(cells) == TRANSPORTS
    assert all(set(transport_cells) == SCENARIOS for transport_cells in cells.values())

    applicability: Counter[str] = Counter()
    coverage: Counter[str] = Counter()
    for transport, transport_cells in cells.items():
        for scenario, cell in transport_cells.items():
            prefix = f"{transport}/{scenario}"
            assert cell["applicability"] == "required", prefix
            assert cell["coverage"] in {"existing", "missing"}, prefix
            assert isinstance(cell["rationale"], str) and cell["rationale"].strip(), prefix
            evidence = cell["evidence"]
            assert isinstance(evidence, list), prefix
            if cell["coverage"] == "existing":
                assert evidence, prefix
                for node_id in evidence:
                    _assert_test_exists(node_id)
            else:
                assert evidence == [], prefix
            applicability[cell["applicability"]] += 1
            coverage[cell["coverage"]] += 1

    assert manifest["counts"] == {
        "applicability": dict(sorted(applicability.items())),
        "coverage": dict(sorted(coverage.items())),
    }

    execution = manifest["driver_execution"]
    assert set(execution["transports"]) == TRANSPORTS
    statuses: Counter[str] = Counter()
    for transport, driver in execution["transports"].items():
        assert driver["status"] == "complete", f"{transport} lifecycle driver regressed to pending"
        assert isinstance(driver["suite_node"], str), transport
        _assert_lifecycle_suite_exists(driver["suite_node"])
        assert set(driver["scenarios"]) == SCENARIOS, transport
        statuses[driver["status"]] += 1
    assert execution["counts"] == {"complete": len(TRANSPORTS)}
    assert statuses == Counter({"complete": len(TRANSPORTS)})


def _load_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _assert_test_exists(node_id: str) -> None:
    parts = node_id.split("::")
    assert len(parts) >= 2, f"invalid pytest node id: {node_id}"
    path = REPO_ROOT / parts[0]
    assert path.is_relative_to(REPO_ROOT / "tests"), f"evidence escapes tests/: {node_id}"
    assert path.is_file(), f"missing evidence file: {node_id}"

    body: list[ast.stmt] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path)).body
    parent_class: ast.ClassDef | None = None
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
        if (
            node is None
            and index == len(parts[1:]) - 1
            and parent_class is not None
            and name in LIFECYCLE_TEST_METHODS
            and _inherits_lifecycle_suite(parent_class)
        ):
            return
        assert node is not None, f"missing evidence node: {node_id}"
        if index < len(parts[1:]) - 1:
            assert isinstance(node, ast.ClassDef), f"non-class evidence parent: {node_id}"
            parent_class = node
            body = node.body
        else:
            assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)), (
                f"evidence leaf is not a test: {node_id}"
            )
            assert node.name.startswith("test_"), f"evidence leaf is not a test: {node_id}"


def _assert_lifecycle_suite_exists(node_id: str) -> None:
    parts = node_id.split("::")
    assert len(parts) == 2, f"invalid lifecycle suite node id: {node_id}"
    path = REPO_ROOT / parts[0]
    assert path.is_relative_to(REPO_ROOT / "tests"), f"suite escapes tests/: {node_id}"
    assert path.is_file(), f"missing lifecycle suite file: {node_id}"
    body = ast.parse(path.read_text(encoding="utf-8"), filename=str(path)).body
    suite = next(
        (node for node in body if isinstance(node, ast.ClassDef) and node.name == parts[1]),
        None,
    )
    assert suite is not None, f"missing lifecycle suite: {node_id}"
    assert _inherits_lifecycle_suite(suite), (
        f"lifecycle driver must inherit TransportLifecycleScenarioSuite: {node_id}"
    )


def _inherits_lifecycle_suite(node: ast.ClassDef) -> bool:
    return any(
        (isinstance(base, ast.Name) and base.id == "TransportLifecycleScenarioSuite")
        or (isinstance(base, ast.Attribute) and base.attr == "TransportLifecycleScenarioSuite")
        for base in node.bases
    )
