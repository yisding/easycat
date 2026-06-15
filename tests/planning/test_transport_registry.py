"""Unit coverage for the NET-NEW declarative metadata tables (M6b).

The five non-catalog roles (transport/vad/noise_reducer/echo_canceller/agent)
resolve from these tables. Every backend must map to the right config-type /
extra / required_env / probe-module / capabilities, and ``EXTRA_PROBE_MODULE``
must cover every extra named anywhere in the planning surface so
``importlib.util.find_spec`` works uniformly.
"""

from __future__ import annotations

import pytest

from easycat.planning.transport_registry import (
    AGENT_BACKENDS,
    ECHO_CANCELLER_BACKENDS,
    EXTRA_PROBE_MODULE,
    NOISE_REDUCER_BACKENDS,
    NON_CATALOG_ROLES,
    TRANSPORT_BACKENDS,
    VAD_BACKENDS,
    probe_module_for_extra,
)


def test_transport_backends_cover_manifest_shortcuts() -> None:
    from easycat.project.schema import TRANSPORT_SHORTCUTS

    assert set(TRANSPORT_BACKENDS) == set(TRANSPORT_SHORTCUTS)
    assert TRANSPORT_BACKENDS["webrtc"].extra == "webrtc"
    assert TRANSPORT_BACKENDS["websocket"].extra is None  # stdlib websockets
    assert TRANSPORT_BACKENDS["twilio"].extra == "telephony"
    assert TRANSPORT_BACKENDS["local"].extra == "local"
    assert TRANSPORT_BACKENDS["webrtc"].config_type == "WebRTCTransportConfig"


def test_vad_backends_cover_vad_literal() -> None:
    from easycat.vad._base import _VALID_VAD_BACKENDS

    assert set(VAD_BACKENDS) == set(_VALID_VAD_BACKENDS)
    assert VAD_BACKENDS["silero"].extra == "silero-vad"
    assert VAD_BACKENDS["ten"].extra == "ten-vad"
    assert VAD_BACKENDS["funasr"].extra == "funasr-vad"
    # Krisp is a commercial SDK with no pyproject extra.
    assert VAD_BACKENDS["krisp"].extra is None


def test_noise_reducer_backends_cover_backend_literal() -> None:
    from easycat.noise_reduction import _VALID_NOISE_REDUCER_BACKENDS

    assert set(NOISE_REDUCER_BACKENDS) == set(_VALID_NOISE_REDUCER_BACKENDS)
    assert NOISE_REDUCER_BACKENDS["rnnoise"].extra == "rnnoise"
    assert NOISE_REDUCER_BACKENDS["krisp"].extra is None


def test_echo_canceller_backends() -> None:
    assert ECHO_CANCELLER_BACKENDS["livekit"].extra == "aec"
    assert ECHO_CANCELLER_BACKENDS["passthrough"].extra is None
    assert "echo_cancellation" in ECHO_CANCELLER_BACKENDS["livekit"].capabilities


def test_agent_backend_has_no_default_env_or_extra() -> None:
    python_backend = AGENT_BACKENDS["python"]
    assert python_backend.extra is None
    assert python_backend.required_env is None
    assert "custom_agent" in python_backend.capabilities


def test_capabilities_are_declared_frozensets() -> None:
    for table in (
        TRANSPORT_BACKENDS,
        VAD_BACKENDS,
        NOISE_REDUCER_BACKENDS,
        ECHO_CANCELLER_BACKENDS,
        AGENT_BACKENDS,
    ):
        for backend in table.values():
            assert isinstance(backend.capabilities, frozenset)


def test_extra_probe_module_covers_every_named_extra() -> None:
    # Every extra named by any backend table must have a probe-module entry so
    # find_spec works uniformly.
    named_extras: set[str] = set()
    for table in (
        TRANSPORT_BACKENDS,
        VAD_BACKENDS,
        NOISE_REDUCER_BACKENDS,
        ECHO_CANCELLER_BACKENDS,
        AGENT_BACKENDS,
    ):
        named_extras.update(b.extra for b in table.values() if b.extra is not None)
    # Plus the STT/TTS catalog extras.
    from easycat._provider_catalog import provider_extras

    named_extras.update(e for e in provider_extras().values() if e)

    missing = named_extras - set(EXTRA_PROBE_MODULE)
    assert not missing, f"EXTRA_PROBE_MODULE missing entries for: {sorted(missing)}"


def test_probe_module_for_extra_resolves() -> None:
    assert probe_module_for_extra(None) is None
    # Empty-dependency marker extras map to None (nothing to probe).
    assert probe_module_for_extra("deepgram") is None
    assert probe_module_for_extra("silero-vad") == "onnxruntime"
    assert probe_module_for_extra("webrtc") == "aiortc"


def test_probe_module_for_unknown_extra_raises() -> None:
    with pytest.raises(KeyError):
        probe_module_for_extra("not-a-real-extra")


def test_non_catalog_roles_are_the_five() -> None:
    assert set(NON_CATALOG_ROLES) == {
        "transport",
        "vad",
        "agent",
        "noise_reducer",
        "echo_canceller",
    }
