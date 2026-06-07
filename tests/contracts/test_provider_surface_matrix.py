from __future__ import annotations

from dataclasses import fields
from importlib import import_module
from pathlib import Path

import pytest

from easycat.stt.factory import _PROVIDER_TO_CONFIG as _STT_REGISTRY
from easycat.tts.factory import _PROVIDERS as _TTS_REGISTRY
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
    normalized_readme = " ".join(contract_readme.split())

    assert "wiring seam" in wiring_matrix
    assert "protocol cassette" not in wiring_matrix.lower()
    assert "factory/session wiring" in contract_readme
    assert "protocol contracts" in contract_readme
    for command in (
        "uv run easycat validate contracts",
        "uv run easycat validate contracts --json",
        "uv run pytest tests/contracts",
        "uv run pytest tests/integration/test_provider_contract_matrix.py",
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


def test_validation_tasks_v32_current_state_tracks_provider_matrix_scope() -> None:
    wiring_matrix = import_module("tests.integration.test_provider_contract_matrix")
    plan = (REPO_ROOT / "plan/validation/tasks.md").read_text(encoding="utf-8")
    section = plan.split("### V3.2 Preserve Existing Provider Matrix Scope", 1)[1].split(
        "### V3.3 Add STT/TTS/VAD/Transport Contract Tests", 1
    )[0]
    normalized_section = " ".join(section.split())
    wiring_source = (REPO_ROOT / "tests/integration/test_provider_contract_matrix.py").read_text(
        encoding="utf-8"
    )
    contract_readme = (REPO_ROOT / "tests/contracts/README.md").read_text(encoding="utf-8")

    registered_stt_configs = {config_cls for _provider_cls, config_cls in _STT_REGISTRY.values()}
    registered_tts_configs = {config_cls for _provider_cls, config_cls in _TTS_REGISTRY.values()}

    assert set(wiring_matrix._STT_CONFIG_CLASSES) == registered_stt_configs
    assert set(wiring_matrix._TTS_CONFIG_CLASSES) == registered_tts_configs
    assert wiring_matrix._EXPECTED_STT_CONFIGS <= registered_stt_configs
    assert wiring_matrix._EXPECTED_TTS_CONFIGS <= registered_tts_configs
    assert "OpenAIRealtimeSTTConfig" in {config.__name__ for config in registered_stt_configs}
    assert "CartesiaSTTConfig" in {config.__name__ for config in registered_stt_configs}
    assert "CartesiaTTSConfig" in {config.__name__ for config in registered_tts_configs}
    assert "factory/session wiring seam" in wiring_source
    assert "protocol cassette" not in wiring_source.lower()
    assert "create_stt_provider_from_config" in wiring_source
    assert "create_tts_provider_from_config" in wiring_source
    assert "_CONFIG_TO_PROVIDER" in wiring_source
    assert "create_session" in wiring_source
    assert "ScriptedSTT" in wiring_source
    assert "ScriptedVAD" in wiring_source
    assert "RecordingTTS" in wiring_source
    assert "factory/session wiring" in contract_readme
    assert "protocol contracts" in contract_readme
    assert not missing_registered_provider_surfaces()

    assert "Current verified state:" in section
    for token in (
        "tests/integration/test_provider_contract_matrix.py",
        "factory/session wiring seam",
        "_STT_CONFIG_CLASSES",
        "easycat.stt.factory._PROVIDER_TO_CONFIG",
        "_TTS_CONFIG_CLASSES",
        "easycat.tts.factory._PROVIDERS",
        "create_stt_provider_from_config",
        "create_tts_provider_from_config",
        "_CONFIG_TO_PROVIDER",
        "create_session()",
        "test_registry_covers_every_known_config",
        "OpenAISTTConfig",
        "OpenAIRealtimeSTTConfig",
        "DeepgramSTTConfig",
        "ElevenLabsSTTConfig",
        "CartesiaSTTConfig",
        "OpenAITTSConfig",
        "DeepgramTTSConfig",
        "ElevenLabsTTSConfig",
        "CartesiaTTSConfig",
        "tests/contracts/test_provider_surface_matrix.py",
        "missing_registered_provider_surfaces()",
        "tests/contracts/README.md",
        "tests/contracts/",
    ):
        assert f"`{token}`" in section
    for phrase in (
        "not a protocol cassette suite",
        "auto-parametrized across every STT x TTS session pair",
        "EventBus injection checks",
        "scripted",
        "OpenAI realtime and Cartesia cannot silently fall out",
        "contract row or explicit exclusion",
        "cassette replay, schema drift fingerprints, and bridge event grammar",
    ):
        assert phrase in normalized_section


def test_validation_tasks_v33_current_state_tracks_surface_contract_files() -> None:
    plan = (REPO_ROOT / "plan/validation/tasks.md").read_text(encoding="utf-8")
    section = plan.split("### V3.3 Add STT/TTS/VAD/Transport Contract Tests", 1)[1].split(
        "### V3.4 Add Agent Bridge Contract Tests", 1
    )[0]
    normalized_section = " ".join(section.split())
    contract_paths_by_surface = {
        "stt": "tests/contracts/test_stt_provider_contracts.py",
        "tts": "tests/contracts/test_tts_provider_contracts.py",
        "vad": "tests/contracts/test_vad_provider_contracts.py",
        "transport": "tests/contracts/test_transport_contracts.py",
    }
    expected_providers_by_surface = {
        "stt": {"openai", "openai-realtime", "deepgram", "elevenlabs", "cartesia"},
        "tts": {"openai", "deepgram", "elevenlabs", "cartesia"},
        "vad": {"silero", "funasr", "ten", "krisp"},
        "transport": {"local", "websocket", "twilio", "webrtc", "webtransport"},
    }
    source_by_surface = {
        surface: (REPO_ROOT / contract_path).read_text(encoding="utf-8")
        for surface, contract_path in contract_paths_by_surface.items()
    }

    for surface, contract_path in contract_paths_by_surface.items():
        rows = [row for row in PROVIDER_SURFACE_CONTRACTS if row.surface == surface]
        assert rows
        assert {row.provider for row in rows} == expected_providers_by_surface[surface]
        assert {row.contract_path for row in rows} == {contract_path}
        assert Path(contract_path).exists()
        assert "pytest.mark.contract" in source_by_surface[surface]
        assert f"pytest.mark.surface_{surface}" in source_by_surface[surface]

    stt_source = source_by_surface["stt"]
    tts_source = source_by_surface["tts"]
    vad_source = source_by_surface["vad"]
    transport_source = source_by_surface["transport"]
    assert "STTEventType.PARTIAL" in stt_source
    assert "STTEventType.FINAL" in stt_source
    assert "commit_segment" in stt_source
    assert "end_stream" in stt_source
    assert "PCM16_MONO_16K" in stt_source
    assert "TTSEventType.AUDIO" in tts_source
    assert "TTSEventType.MARKERS" in tts_source
    assert "supports_ssml = False" in tts_source
    assert "PCM16_MONO_24K" in tts_source
    assert "stop()" in tts_source
    assert "cancel()" in tts_source
    assert "VADStartSpeaking" in vad_source
    assert "VADStopSpeaking" in vad_source
    assert "configure" in vad_source
    assert "send_audio" in transport_source
    assert "receive_audio" in transport_source
    assert "clear_audio" in transport_source
    assert not missing_registered_provider_surfaces()

    assert "Current verified state:" in section
    for token in (
        "tests/contracts/test_stt_provider_contracts.py",
        "tests/contracts/test_tts_provider_contracts.py",
        "tests/contracts/test_vad_provider_contracts.py",
        "tests/contracts/test_transport_contracts.py",
        "contract",
        "surface_stt",
        "surface_tts",
        "surface_vad",
        "surface_transport",
        "matrix",
        "offline-fake",
        "STTEventType.PARTIAL",
        "STTEventType.FINAL",
        "AudioChunk",
        "PCM16_MONO_16K",
        "TTSEventType.AUDIO",
        "TTSEventType.MARKERS",
        "PCM16_MONO_24K",
        "supports_ssml=False",
        "VADStartSpeaking",
        "VADStopSpeaking",
        "clear_audio()",
        "tests/contracts/provider_surface_matrix.py",
        "missing_registered_provider_surfaces()",
    ):
        assert f"`{token}`" in section
    expected_provider_names = {
        name for names in expected_providers_by_surface.values() for name in names
    }
    for provider in sorted(expected_provider_names):
        assert f"`{provider}`" in section
    for phrase in (
        "provider protocol conformance",
        "repeat `end_stream()` behavior",
        "idempotent `stop()` / `cancel()`",
        "failed sends before connect",
        "audio-format expectations",
        "marker passthrough",
        "idempotency behavior",
        "provider error-taxonomy and live-output quality checks remain outside",
    ):
        assert phrase in normalized_section
