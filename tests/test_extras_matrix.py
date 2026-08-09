"""Guards for the nightly extras install matrix scripts.

The nightly workflow derives its install matrix from
``scripts/extras_matrix.py`` and import-smokes each cell with
``scripts/extras_smoke.py``; these tests pin the contract between
``pyproject.toml``, those scripts, and the provider surface matrix.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _declared_extras() -> set[str]:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return set(pyproject["project"]["optional-dependencies"])


def test_planned_extras_cover_every_declared_extra_except_documented_exclusions() -> None:
    extras_matrix = _load_script("extras_matrix")
    declared = _declared_extras()
    excluded = set(extras_matrix.EXCLUDED_EXTRAS)

    assert set(extras_matrix.planned_extras()) == declared - excluded
    assert excluded <= declared, "EXCLUDED_EXTRAS names an extra pyproject no longer declares"
    for name, reason in extras_matrix.EXCLUDED_EXTRAS.items():
        assert reason.strip(), f"excluded extra {name!r} must record a reason"


def test_ten_vad_exclusion_is_a_recorded_licensing_decision() -> None:
    extras_matrix = _load_script("extras_matrix")

    assert "ten-vad" in extras_matrix.EXCLUDED_EXTRAS
    assert "license" in extras_matrix.EXCLUDED_EXTRAS["ten-vad"]


def test_extras_matrix_main_emits_github_output_json_assignment() -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "extras_matrix.py")],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    )
    key, _, payload = result.stdout.strip().partition("=")

    assert key == "extras"
    extras = json.loads(payload)
    assert isinstance(extras, list)
    assert extras and all(isinstance(extra, str) for extra in extras)
    assert "ten-vad" not in extras


def test_nightly_extra_cells_execute_sdk_gated_bridge_tests() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "nightly-validation.yml").read_text(
        encoding="utf-8"
    )

    assert (
        'pytest tests/contracts tests/integrations/agents -q -m "not integration_live"' in workflow
    )


def test_bridge_contract_extras_have_exact_nodes_and_sha_evidence() -> None:
    evidence = _load_script("bridge_extras_evidence")
    declared = _declared_extras()

    assert set(evidence.BRIDGE_CONTRACT_NODES) == {
        "langchain",
        "langchain-v0",
        "langgraph",
        "llama-agents",
        "openai-agents",
        "pydantic-ai",
        "pydantic-ai-v2",
    }
    assert set(evidence.BRIDGE_CONTRACT_NODES) <= declared
    assert all(
        node.startswith("tests/integrations/agents/test_real_sdk_bridge_contracts.py::TestReal")
        for node in evidence.BRIDGE_CONTRACT_NODES.values()
    )

    workflow = (REPO_ROOT / ".github" / "workflows" / "nightly-validation.yml").read_text(
        encoding="utf-8"
    )
    assert "scripts/bridge_extras_evidence.py node" in workflow
    assert "scripts/bridge_extras_evidence.py report" in workflow
    assert '--expected-sha "$CANDIDATE_SHA"' in workflow
    assert "CANDIDATE_SHA: ${{ github.sha }}" in workflow
    assert '--junitxml="$BRIDGE_EXTRAS_EVIDENCE_DIR/junit.xml"' in workflow
    assert "bridge-extras-proof-${{ matrix.extra }}-${{ github.sha }}" in workflow


def test_bridge_extras_evidence_requires_exact_sha_and_unskipped_tests(tmp_path: Path) -> None:
    evidence = _load_script("bridge_extras_evidence")
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite tests="7" failures="0" errors="0" skipped="0" /></testsuites>',
        encoding="utf-8",
    )

    report, problems = evidence.build_evidence(
        extra="openai-agents",
        expected_sha="a" * 40,
        checkout_sha="a" * 40,
        junit_path=junit,
    )

    assert problems == []
    assert report["status"] == "passed"
    assert report["exact_candidate_checkout"] is True
    assert report["junit"] == {"tests": 7, "failures": 0, "errors": 0, "skipped": 0}


def test_bridge_extras_evidence_rejects_wrong_sha_or_skips(tmp_path: Path) -> None:
    evidence = _load_script("bridge_extras_evidence")
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuite tests="7" failures="0" errors="0" skipped="1" />',
        encoding="utf-8",
    )

    report, problems = evidence.build_evidence(
        extra="pydantic-ai-v2",
        expected_sha="a" * 40,
        checkout_sha="b" * 40,
        junit_path=junit,
    )

    assert report["status"] == "failed"
    assert report["exact_candidate_checkout"] is False
    assert any("does not match candidate SHA" in problem for problem in problems)
    assert any("1 skipped" in problem for problem in problems)


def test_transport_backend_extras_have_exact_nodes_and_sha_evidence() -> None:
    evidence = _load_script("transport_extras_evidence")
    declared = _declared_extras()

    assert evidence.TRANSPORT_CONTRACT_NODES == {
        "webrtc": "tests/transports/test_webrtc_outbound_audio.py",
        "webtransport": "tests/transports/test_webtransport_server_protocol.py",
    }
    assert set(evidence.TRANSPORT_CONTRACT_NODES) <= declared

    workflow = (REPO_ROOT / ".github" / "workflows" / "nightly-validation.yml").read_text(
        encoding="utf-8"
    )
    assert "scripts/transport_extras_evidence.py node" in workflow
    assert "scripts/transport_extras_evidence.py report" in workflow
    assert '--expected-sha "$CANDIDATE_SHA"' in workflow
    assert '--junitxml="$TRANSPORT_EXTRAS_EVIDENCE_DIR/junit.xml"' in workflow
    assert "transport-extras-proof-${{ matrix.extra }}-${{ github.sha }}" in workflow


def test_transport_extras_evidence_requires_exact_sha_and_unskipped_tests(
    tmp_path: Path,
) -> None:
    evidence = _load_script("transport_extras_evidence")
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite tests="17" failures="0" errors="0" skipped="0" /></testsuites>',
        encoding="utf-8",
    )

    report, problems = evidence.build_evidence(
        extra="webtransport",
        expected_sha="a" * 40,
        checkout_sha="a" * 40,
        junit_path=junit,
    )

    assert problems == []
    assert report["status"] == "passed"
    assert report["exact_candidate_checkout"] is True
    assert report["junit"] == {"tests": 17, "failures": 0, "errors": 0, "skipped": 0}


def test_transport_extras_evidence_rejects_wrong_sha_or_skips(tmp_path: Path) -> None:
    evidence = _load_script("transport_extras_evidence")
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuite tests="17" failures="0" errors="0" skipped="1" />',
        encoding="utf-8",
    )

    report, problems = evidence.build_evidence(
        extra="webrtc",
        expected_sha="a" * 40,
        checkout_sha="b" * 40,
        junit_path=junit,
    )

    assert report["status"] == "failed"
    assert report["exact_candidate_checkout"] is False
    assert any("does not match candidate SHA" in problem for problem in problems)
    assert any("1 skipped" in problem for problem in problems)


def test_smoke_adapter_targets_derive_from_required_extra_mapping() -> None:
    extras_smoke = _load_script("extras_smoke")
    from tests.contracts.provider_surface_matrix import PROVIDER_SURFACE_CONTRACTS

    deepgram_targets = extras_smoke.adapter_targets("deepgram")
    expected = sorted(
        {row.adapter for row in PROVIDER_SURFACE_CONTRACTS if row.required_extra == "deepgram"}
    )
    assert deepgram_targets == expected
    assert any("deepgram_provider" in target for target in deepgram_targets)
    assert any("deepgram_tts" in target for target in deepgram_targets)


def test_smoke_covers_cartesia_marker_extra_via_adapters() -> None:
    extras_smoke = _load_script("extras_smoke")

    assert extras_smoke.extra_requirements("cartesia") == []
    targets = extras_smoke.adapter_targets("cartesia")
    assert any("cartesia_provider" in target for target in targets)
    assert any("cartesia_tts" in target for target in targets)


def test_smoke_aliases_versioned_extras_to_their_bridge_rows() -> None:
    extras_smoke = _load_script("extras_smoke")

    assert extras_smoke.MATRIX_EXTRA_ALIASES == {
        "langchain-v0": "langchain",
        "pydantic-ai-v2": "pydantic-ai",
    }
    assert extras_smoke.adapter_targets("langchain-v0") == extras_smoke.adapter_targets(
        "langchain"
    )
    assert extras_smoke.adapter_targets("langchain") == [
        "easycat.integrations.agents.langchain.LangChainBridge"
    ]
    assert extras_smoke.adapter_targets("pydantic-ai-v2") == (
        extras_smoke.adapter_targets("pydantic-ai")
    )
    assert extras_smoke.adapter_targets("pydantic-ai") == [
        "easycat.integrations.agents.pydantic_ai.PydanticAIBridge"
    ]


def test_smoke_requirements_respect_environment_markers() -> None:
    extras_smoke = _load_script("extras_smoke")

    names = {requirement.name for requirement in extras_smoke.extra_requirements("funasr-vad")}
    assert {"kaldi-native-fbank", "numpy", "onnxruntime"} <= names
    assert "funasr-onnx" not in names


def test_smoke_distribution_modules_map_installed_distributions() -> None:
    extras_smoke = _load_script("extras_smoke")

    assert "httpx" in extras_smoke.distribution_modules(["httpx"])
    assert extras_smoke.distribution_modules(["not-a-real-distribution"]) == []


def test_smoke_rejects_unknown_extras() -> None:
    import pytest

    extras_smoke = _load_script("extras_smoke")
    with pytest.raises(SystemExit, match="unknown extra"):
        extras_smoke.extra_requirements("no-such-extra")
