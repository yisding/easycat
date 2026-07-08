from __future__ import annotations

from pathlib import Path

import pytest

from easycat.stt.factory import _CATALOG as _STT_CATALOG
from easycat.tts.factory import _CATALOG as _TTS_CATALOG
from tests.contracts.provider_surface_matrix import (
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


def test_stt_tts_matrix_rows_match_provider_catalog_metadata() -> None:
    """STT/TTS matrix rows must carry the catalog's extra and env var verbatim."""
    catalog_by_surface = {"stt": _STT_CATALOG, "tts": _TTS_CATALOG}
    for row in PROVIDER_SURFACE_CONTRACTS:
        catalog = catalog_by_surface.get(row.surface)
        if catalog is None:
            continue
        assert row.required_extra == catalog.extras[row.provider], (
            f"{row.provider}/{row.surface}: required_extra {row.required_extra!r} "
            f"diverges from catalog extra {catalog.extras[row.provider]!r}"
        )
        assert row.credential_env_var == catalog.env_vars[row.provider], (
            f"{row.provider}/{row.surface}: credential_env_var {row.credential_env_var!r} "
            f"diverges from catalog env var {catalog.env_vars[row.provider]!r}"
        )


def test_provider_surface_matrix_has_no_duplicate_rows() -> None:
    keys = [row.key for row in PROVIDER_SURFACE_CONTRACTS]

    assert len(keys) == len(set(keys))


def test_every_registered_stt_tts_provider_surface_has_contract_row_or_exclusion() -> None:
    missing = missing_registered_provider_surfaces()

    assert not missing


def test_wiring_matrix_scope_is_documented_separately_from_protocol_contracts() -> None:
    wiring_matrix = Path("tests/contracts/test_provider_session_matrix.py").read_text()
    contract_readme = Path("tests/contracts/README.md").read_text()
    normalized_readme = " ".join(contract_readme.split())

    assert "wiring seam" in wiring_matrix
    assert "protocol cassette" not in wiring_matrix.lower()
    assert "factory/session wiring" in contract_readme
    assert "protocol contracts" in contract_readme
    for command in (
        "uv run easycat docs --audience provider-maintainers",
        "uv run easycat docs --audience provider-maintainers --json",
        "uv run easycat validate contracts",
        "uv run easycat validate contracts --json",
        "uv run pytest tests/contracts",
        "uv run pytest tests/contracts/test_provider_session_matrix.py",
    ):
        assert command in contract_readme
    for linked_file in (
        "[`provider_surface_matrix.py`](provider_surface_matrix.py)",
        "[`test_stt_provider_contracts.py`](test_stt_provider_contracts.py)",
        "[`test_tts_provider_contracts.py`](test_tts_provider_contracts.py)",
        "[`test_vad_provider_contracts.py`](test_vad_provider_contracts.py)",
        "[`test_transport_contracts.py`](test_transport_contracts.py)",
        "[`test_agent_bridge_contracts.py`](test_agent_bridge_contracts.py)",
    ):
        assert linked_file in contract_readme
    for phrase in (
        "required extra",
        "credential env var",
        "cassette status",
        "contract path",
        "Refresh cassettes or schema fingerprints only when the provider protocol shape changes",
    ):
        assert phrase in normalized_readme
