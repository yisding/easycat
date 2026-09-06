"""Unit coverage for the declarative built-in backend metadata tables (M6b).

The five roles with built-in tables
(transport/vad/noise_reducer/echo_canceller/agent) resolve their bundled
backends here; registered audio-stage extensions supplement them through
provider catalogs. Every backend must map to the right config-type / extra /
required_env / probe-module / capabilities.
"""

from __future__ import annotations

import pytest

from easycat.planning.transport_registry import (
    AGENT_BACKENDS,
    BUILTIN_BACKEND_ROLES,
    ECHO_CANCELLER_BACKENDS,
    EXTRA_PROBE_MODULE,
    NOISE_REDUCER_BACKENDS,
    NON_CATALOG_ROLES,
    TRANSPORT_BACKENDS,
    TRANSPORT_BACKENDS_BY_CONFIG_TYPE,
    VAD_BACKENDS,
    RoleBackend,
    probe_module_for_extra,
)


def test_transport_backends_cover_manifest_shortcuts() -> None:
    from easycat.project.schema import TRANSPORT_SHORTCUTS

    assert set(TRANSPORT_BACKENDS) == set(TRANSPORT_SHORTCUTS)
    assert TRANSPORT_BACKENDS["webrtc"].extra == "webrtc"
    assert TRANSPORT_BACKENDS["websocket"].extra is None  # stdlib websockets
    assert TRANSPORT_BACKENDS["twilio"].extra == "telephony"
    assert TRANSPORT_BACKENDS["telnyx"].extra == "telnyx"
    assert TRANSPORT_BACKENDS["local"].extra == "local"
    assert TRANSPORT_BACKENDS["webrtc"].config_type == "WebRTCTransportConfig"
    assert TRANSPORT_BACKENDS["telnyx"].capabilities == frozenset({"telephony", "l16", "16khz"})
    assert TRANSPORT_BACKENDS["telnyx"].default_echo_cancellation_enabled is False


def test_vad_backends_cover_vad_literal() -> None:
    from easycat.vad._base import _VALID_VAD_BACKENDS

    assert set(VAD_BACKENDS) == set(_VALID_VAD_BACKENDS)
    assert VAD_BACKENDS["silero"].extra == "silero-vad"
    assert VAD_BACKENDS["ten"].extra == "ten-vad"
    assert VAD_BACKENDS["funasr"].extra == "funasr-vad"
    # Krisp is a commercial SDK with no pyproject extra, so it declares the
    # module to probe instead — see
    # ``test_every_backend_without_an_extra_declares_a_probe_or_needs_none``.
    assert VAD_BACKENDS["krisp"].extra is None
    assert VAD_BACKENDS["krisp"].probe_module == "krisp_audio"


def test_noise_reducer_backends_cover_backend_literal() -> None:
    from easycat.noise_reduction import _VALID_NOISE_REDUCER_BACKENDS

    assert set(NOISE_REDUCER_BACKENDS) == set(_VALID_NOISE_REDUCER_BACKENDS)
    assert NOISE_REDUCER_BACKENDS["rnnoise"].extra == "rnnoise"
    assert NOISE_REDUCER_BACKENDS["krisp"].extra is None
    assert NOISE_REDUCER_BACKENDS["krisp"].probe_module == "krisp_audio"


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
    from easycat._provider_registry import provider_extras

    named_extras.update(e for e in provider_extras().values() if e)

    missing = named_extras - set(EXTRA_PROBE_MODULE)
    assert not missing, f"EXTRA_PROBE_MODULE missing entries for: {sorted(missing)}"


def test_probe_module_for_extra_resolves() -> None:
    assert probe_module_for_extra(None) is None
    # Providers backed entirely by core HTTP/WebSocket dependencies map to
    # None (nothing additional to probe).
    assert probe_module_for_extra("openai") is None
    assert probe_module_for_extra("deepgram") is None
    assert probe_module_for_extra("silero-vad") == "onnxruntime"
    assert probe_module_for_extra("webrtc") == "aiortc"
    assert probe_module_for_extra("telnyx") == "cryptography"


def test_probe_module_for_unmapped_third_party_extra_falls_back_to_extra_name() -> None:
    # A registered third-party provider may carry an arbitrary extra with no
    # built-in mapping. Rather than crash the planner (a KeyError pinned
    # /health/ready), probe the extra NAME itself so a genuinely-missing install
    # is still flagged. (Built-in completeness is guarded separately above.)
    assert probe_module_for_extra("acme-stt") == "acme-stt"


def test_transport_aec_defaults_match_manifest_resolved_easyconfig(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The planner's per-transport ``default_echo_cancellation_enabled`` mirror is
    # consumed ONLY by the manifest path (``_resolution.resolve_from_profile``),
    # so tie it to what ``to_easyconfig`` ACTUALLY resolves per transport — NOT
    # the bare transport-config ClassVar, which the browser/phone presets
    # override (webrtc's ClassVar is False but ``EasyConfig.browser`` forces AEC
    # on). This catches a silent drift if a preset OR the mirror changes.
    from easycat.project.manifest import ProjectManifest
    from easycat.project.schema import (
        ProjectSection,
        ServerSection,
        VoiceProfile,
        parse_auth_reference,
    )

    monkeypatch.setenv("OPENAI_API_KEY", "sk-registry-test")
    monkeypatch.setenv("TWILIO_STREAM_TOKEN_SECRET", "twilio-registry-test")
    monkeypatch.setenv("TELNYX_STREAM_TOKEN_SECRET", "telnyx-registry-test")

    for shortcut, backend in TRANSPORT_BACKENDS.items():
        token = (
            parse_auth_reference(
                f"bearer-env:{shortcut.upper()}_STREAM_TOKEN_SECRET",
                field_name="voice.default.token",
            )
            if shortcut in {"twilio", "telnyx"}
            else None
        )
        profile = VoiceProfile(name="default", transport=shortcut, token=token)
        manifest = ProjectManifest(
            project=ProjectSection(), server=ServerSection(), profiles={"default": profile}
        )
        config = manifest.to_easyconfig("default", resolve_agent=False)
        resolved = bool(config.echo_cancellation and config.echo_cancellation.enabled)
        assert resolved == backend.default_echo_cancellation_enabled, shortcut


def test_transport_factory_dispatch_and_registry_agree() -> None:
    # DX1-1, duplicate G (§7.1 of the DX1 design): closes the "transport
    # config-type -> backend metadata" duplication between
    # ``_factory._transport_factories()`` and this module's declarative table
    # WITHOUT merging them — a key-set consistency check instead. Verified at
    # this baseline: ``_transport_factories()`` imports all six transport
    # modules successfully with ZERO extras installed, so this test needs no
    # importorskip gate.
    from easycat.config import _factory

    factory_config_types = {
        config_type.__name__ for config_type in _factory._transport_factories()
    }
    registry_config_types = set(TRANSPORT_BACKENDS_BY_CONFIG_TYPE)
    missing_from_registry = sorted(factory_config_types - registry_config_types)
    missing_from_factory = sorted(registry_config_types - factory_config_types)
    assert factory_config_types == registry_config_types, (
        f"missing from the registry: {missing_from_registry}; "
        f"missing from the factory dispatch: {missing_from_factory}"
    )


# Every backend that declares NO pip extra, and why it needs no probe module.
# A backend absent from this map must declare ``probe_module`` — the planner has
# no other way to notice that ``create_session`` would refuse to build it, and a
# selection with neither is silently reported READY forever. Adding an entry here
# is a deliberate statement that the backend needs no third-party import at all.
_EXTRA_LESS_BACKENDS_NEEDING_NO_PROBE: dict[tuple[str, str], str] = {
    ("transport", "websocket"): "stdlib + the core ``websockets`` dependency",
    ("echo_canceller", "passthrough"): "the built-in no-op AEC; pure Python",
    ("agent", "python"): "the user's own module; its imports are not EasyCat's",
    ("agent", "none"): "``NoopAgent``; pure Python",
}


def test_every_backend_without_an_extra_declares_a_probe_or_needs_none() -> None:
    # The D2 guard. ``VAD_BACKENDS["krisp"]`` reported READY on every machine
    # without the commercial SDK for exactly this reason: no extra to probe and
    # no probe module either. This test makes the next extra-less backend a
    # deliberate decision rather than a silent repeat.
    tables: dict[str, dict[str, RoleBackend]] = {
        "transport": TRANSPORT_BACKENDS,
        "vad": VAD_BACKENDS,
        "noise_reducer": NOISE_REDUCER_BACKENDS,
        "echo_canceller": ECHO_CANCELLER_BACKENDS,
        "agent": AGENT_BACKENDS,
    }
    undeclared: list[str] = []
    for role, table in tables.items():
        for name, backend in table.items():
            if backend.extra is not None or backend.probe_module is not None:
                continue
            if (role, name) in _EXTRA_LESS_BACKENDS_NEEDING_NO_PROBE:
                continue
            undeclared.append(f"{role}:{name}")
    assert not undeclared, (
        "These backends declare neither an install extra nor a probe module, so "
        "the planner can never report them as unbuildable: "
        + ", ".join(sorted(undeclared))
        + ". Declare probe_module=..., or add the entry to "
        "_EXTRA_LESS_BACKENDS_NEEDING_NO_PROBE with the reason it needs none."
    )
    # The allow-list must not rot either: every entry has to name a real,
    # extra-less, probe-less backend.
    stale = [
        f"{role}:{name}"
        for role, name in _EXTRA_LESS_BACKENDS_NEEDING_NO_PROBE
        if name not in tables[role]
        or tables[role][name].extra is not None
        or tables[role][name].probe_module is not None
    ]
    assert not stale, f"stale _EXTRA_LESS_BACKENDS_NEEDING_NO_PROBE entries: {sorted(stale)}"


def test_a_backend_declaring_an_extra_needs_no_probe_module() -> None:
    # ``probe_module`` is only read when ``extra is None`` (an extra resolves its
    # own probe through ``probe_module_for_extra``), so the two must not both be
    # set — that would be two sources of truth for one backend.
    for table in (
        TRANSPORT_BACKENDS,
        VAD_BACKENDS,
        NOISE_REDUCER_BACKENDS,
        ECHO_CANCELLER_BACKENDS,
        AGENT_BACKENDS,
    ):
        for name, backend in table.items():
            assert not (backend.extra is not None and backend.probe_module is not None), name


def test_builtin_backend_roles_are_the_five() -> None:
    expected = {
        "transport",
        "vad",
        "agent",
        "noise_reducer",
        "echo_canceller",
    }
    assert set(BUILTIN_BACKEND_ROLES) == expected
    assert NON_CATALOG_ROLES == BUILTIN_BACKEND_ROLES
