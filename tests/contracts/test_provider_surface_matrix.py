from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from tests.contracts.provider_surface_matrix import (
    EXPLICIT_PROVIDER_SURFACE_EXCLUSIONS,
    PROVIDER_SURFACE_CONTRACTS,
    ProviderSurfaceContract,
    missing_registered_provider_surfaces,
)

pytestmark = pytest.mark.contract
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_provider_surface_matrix_rows_have_required_report_dimensions() -> None:
    assert PROVIDER_SURFACE_CONTRACTS
    for row in PROVIDER_SURFACE_CONTRACTS:
        assert isinstance(row, ProviderSurfaceContract)
        assert row.provider
        assert row.surface in {"stt", "tts", "vad", "transport", "agent_bridge"}
        assert row.adapter
        assert row.protocol
        assert row.mode
        assert row.model_api_version
        assert row.required_extra is not None
        assert row.credential_env_var is not None
        assert row.contract_path
        assert Path(row.contract_path).exists()
        assert row.cassette_path
        assert row.cassette_status in {"required", "deferred", "not_applicable"}
        if row.cassette_status == "required":
            assert Path(row.cassette_path).exists()
        if row.cassette_status == "deferred" or row.required_extra:
            assert row.expected_skip_reason
        assert row.live_canary_status in {"required", "deferred", "not_applicable"}


def test_provider_surface_matrix_has_no_duplicate_rows() -> None:
    keys = [row.key for row in PROVIDER_SURFACE_CONTRACTS]

    assert len(keys) == len(set(keys))


def test_every_registered_stt_tts_provider_surface_has_contract_row_or_exclusion() -> None:
    missing = missing_registered_provider_surfaces()

    assert not missing


def test_wiring_matrix_scope_is_documented_separately_from_protocol_contracts() -> None:
    wiring_matrix = Path("tests/integration/test_provider_contract_matrix.py").read_text()
    contract_readme = Path("tests/contracts/README.md").read_text()

    assert "wiring seam" in wiring_matrix
    assert "protocol cassette" not in wiring_matrix.lower()
    assert "factory/session wiring" in contract_readme
    assert "protocol contracts" in contract_readme


def test_validation_tasks_v31_current_state_tracks_contract_matrix_layout() -> None:
    plan = (REPO_ROOT / "plan/validation/tasks.md").read_text(encoding="utf-8")
    section = plan.split("### V3.1 Create Contract Test Directory", 1)[1].split(
        "### V3.2 Preserve Existing Provider Matrix Scope", 1
    )[0]
    normalized_section = " ".join(section.split())
    contract_files = {path.name for path in (REPO_ROOT / "tests/contracts").glob("test_*.py")}
    expected_contract_files = {
        "test_agent_bridge_contracts.py",
        "test_http_cassette_redaction.py",
        "test_provider_capability_report_model.py",
        "test_provider_capability_reports.py",
        "test_provider_reports.py",
        "test_provider_surface_matrix.py",
        "test_sse_cassette_replay.py",
        "test_stt_provider_contracts.py",
        "test_transport_contracts.py",
        "test_tts_provider_contracts.py",
        "test_vad_provider_contracts.py",
        "test_ws_cassette_replay.py",
    }

    assert expected_contract_files <= contract_files
    assert PROVIDER_SURFACE_CONTRACTS
    assert EXPLICIT_PROVIDER_SURFACE_EXCLUSIONS == {}
    assert not missing_registered_provider_surfaces()
    assert "Current verified state:" in section
    assert "`tests/contracts/`" in section
    assert "`tests/integration/`" in section
    assert "`tests/integration/test_provider_contract_matrix.py`" in section
    assert "`tests/contracts/README.md`" in section
    assert "factory/session wiring seam" in section
    assert "protocol contracts" in section
    for name in expected_contract_files:
        assert (
            name.removeprefix("test_").removesuffix(".py").replace("_", " ") in (section.lower())
            or f"`{name}`" in section
        )
    for symbol in (
        "ProviderSurfaceContract",
        "PROVIDER_SURFACE_CONTRACTS",
        "EXPLICIT_PROVIDER_SURFACE_EXCLUSIONS",
        "missing_registered_provider_surfaces()",
    ):
        assert f"`{symbol}`" in section
    for field in fields(ProviderSurfaceContract):
        assert f"`{field.name}`" in section
    for phrase in (
        "required report dimensions",
        "no duplicate keys",
        "existing contract paths",
        "cassette_status=required",
        "no missing registered STT/TTS/VAD/transport provider surfaces",
    ):
        assert phrase in normalized_section
