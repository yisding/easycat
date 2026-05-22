from __future__ import annotations

from pathlib import Path

import pytest

from tests.contracts.provider_surface_matrix import (
    PROVIDER_SURFACE_CONTRACTS,
    ProviderSurfaceContract,
    missing_registered_provider_surfaces,
)

pytestmark = pytest.mark.contract


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
