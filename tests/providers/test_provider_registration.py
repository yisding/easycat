"""Guards for third-party STT/TTS provider registration.

Covers both extension layers:

1. Direct registration via the public ``register_stt_provider`` /
   ``register_tts_provider`` functions.
2. Entry-point discovery via the ``easycat.stt_providers`` /
   ``easycat.tts_providers`` groups, loaded lazily at the first factory
   call.

And the downstream surfaces a registered provider must reach: string
shortcuts, ``available_*_providers()``, catalog-membership config checks,
``easycat doctor`` env-var rows, and scaffold provider validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import get_args

import pytest

from easycat import register_stt_provider, register_tts_provider
from easycat.stt.factory import _CATALOG as STT_CATALOG
from easycat.stt.factory import (
    STTConfig,
    STTProviderConfig,
    available_stt_providers,
    create_stt_provider,
    is_stt_config,
    parse_stt_string,
)
from easycat.tts.factory import _CATALOG as TTS_CATALOG
from easycat.tts.factory import (
    TTSConfig,
    available_tts_providers,
    is_tts_config,
)


@dataclass
class FakeSTTConfig:
    api_key: str = ""
    model: str = "fake-1"


class FakeSTT:
    def __init__(self, config: FakeSTTConfig) -> None:
        self.config = config


class PlainSTTConfig:
    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key


class PlainSTT:
    def __init__(self, config: PlainSTTConfig) -> None:
        self.config = config


@dataclass
class LocalSTTConfig:
    model: str = "tiny"


class LocalSTT:
    def __init__(self, config: LocalSTTConfig) -> None:
        self.config = config


@dataclass
class FakeTTSConfig:
    api_key: str = ""
    model: str = "fake-1"


class FakeTTS:
    def __init__(self, config: FakeTTSConfig) -> None:
        self.config = config


@pytest.fixture(autouse=True)
def restore_catalogs():
    """Snapshot and restore both catalogs around every test.

    Registration mutates the module-level catalogs in place, so tests
    must not leak fake providers into the rest of the suite.
    """
    snapshots = []
    for catalog in (STT_CATALOG, TTS_CATALOG):
        snapshots.append(
            (
                catalog,
                dict(catalog.providers),
                dict(catalog.env_vars),
                dict(catalog.extras),
                dict(catalog.api_domains),
                dict(catalog.probe_modules),
                dict(catalog.capabilities),
                dict(catalog.capability_resolvers),
                dict(catalog.config_to_provider),
                catalog._discovered,
            )
        )
    yield
    for (
        catalog,
        providers,
        env_vars,
        extras,
        api_domains,
        probe_modules,
        capabilities,
        capability_resolvers,
        reverse,
        discovered,
    ) in snapshots:
        catalog.providers.clear()
        catalog.providers.update(providers)
        catalog.env_vars.clear()
        catalog.env_vars.update(env_vars)
        catalog.extras.clear()
        catalog.extras.update(extras)
        catalog.api_domains.clear()
        catalog.api_domains.update(api_domains)
        catalog.probe_modules.clear()
        catalog.probe_modules.update(probe_modules)
        catalog.capabilities.clear()
        catalog.capabilities.update(capabilities)
        catalog.capability_resolvers.clear()
        catalog.capability_resolvers.update(capability_resolvers)
        catalog.config_to_provider.clear()
        catalog.config_to_provider.update(reverse)
        object.__setattr__(catalog, "_discovered", discovered)


def _register_fake_stt() -> None:
    register_stt_provider("fakestt", FakeSTT, FakeSTTConfig, env_var="FAKESTT_API_KEY")


def _register_fake_tts() -> None:
    register_tts_provider("faketts", FakeTTS, FakeTTSConfig, env_var="FAKETTS_API_KEY")


# ── Layer 1: direct registration ─────────────────────────────────


def test_builtin_config_typing_unions_match_catalog_specs() -> None:
    """Adding a built-in spec must also keep the public typing aliases current."""
    assert set(get_args(STTConfig)) == {spec.config_cls for spec in STT_CATALOG.specs.values()}
    assert set(get_args(TTSConfig)) == {spec.config_cls for spec in TTS_CATALOG.specs.values()}


def test_registered_stt_provider_gets_string_shortcuts(monkeypatch: pytest.MonkeyPatch) -> None:
    _register_fake_stt()

    assert "fakestt" in available_stt_providers()

    provider = create_stt_provider(STTProviderConfig(provider="fakestt", api_key="k"))
    assert isinstance(provider, FakeSTT)

    monkeypatch.setenv("FAKESTT_API_KEY", "env-key")
    config = parse_stt_string("fakestt/fake-2")
    assert isinstance(config, FakeSTTConfig)
    assert config.api_key == "env-key"
    assert config.model == "fake-2"


def test_registered_tts_provider_is_listed_and_config_recognized() -> None:
    _register_fake_tts()

    assert "faketts" in available_tts_providers()
    assert is_tts_config(FakeTTSConfig(api_key="k"))
    assert not is_tts_config(FakeSTTConfig(api_key="k"))


def test_registered_config_passes_catalog_membership_checks() -> None:
    assert not is_stt_config(FakeSTTConfig(api_key="k"))
    _register_fake_stt()
    assert is_stt_config(FakeSTTConfig(api_key="k"))


def test_create_from_config_dispatches_registered_provider() -> None:
    from easycat.events import EventBus
    from easycat.stt.factory import create_stt_provider_from_config

    _register_fake_stt()
    provider = create_stt_provider_from_config(FakeSTTConfig(api_key="k"), EventBus())
    assert isinstance(provider, FakeSTT)


def test_create_from_plain_config_does_not_require_dataclass() -> None:
    from easycat.events import EventBus
    from easycat.stt.factory import create_stt_provider_from_config

    register_stt_provider("plainstt", PlainSTT, PlainSTTConfig, env_var="PLAINSTT_API_KEY")
    config = PlainSTTConfig(api_key="k")

    provider = create_stt_provider_from_config(config, EventBus())

    assert isinstance(provider, PlainSTT)
    assert provider.config is config


def test_identical_reregistration_is_a_noop() -> None:
    _register_fake_stt()
    _register_fake_stt()  # must not raise
    assert available_stt_providers().count("fakestt") == 1


def test_conflicting_reregistration_raises() -> None:
    _register_fake_stt()
    with pytest.raises(ValueError, match="already registered"):
        register_stt_provider("fakestt", FakeSTT, FakeSTTConfig, env_var="OTHER_KEY")


def test_registration_requires_name_and_rejects_empty_env_var() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        register_stt_provider("  ", FakeSTT, FakeSTTConfig, env_var="X")
    with pytest.raises(ValueError, match="env_var"):
        register_tts_provider("faketts", FakeTTS, FakeTTSConfig, env_var="")


def test_credential_free_provider_supports_shortcuts_factory_plan_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from easycat.cli.diagnose.doctor import _provider_env
    from easycat.cli.scaffold import init as init_module
    from easycat.planning import build_provider_plan
    from easycat.project.schema import VoiceProfile

    monkeypatch.delenv("LOCALSTT_API_KEY", raising=False)
    register_stt_provider(
        "localstt",
        LocalSTT,
        LocalSTTConfig,
        extra="localstt",
        probe_module="localstt",
    )

    provider = create_stt_provider(STTProviderConfig(provider="localstt"))
    config = parse_stt_string("localstt/base")
    plan = build_provider_plan(
        VoiceProfile(
            name="local",
            transport="websocket",
            stt="localstt/base",
            tts="openai",
        ),
        environ={"OPENAI_API_KEY": "x"},
    )

    assert isinstance(provider, LocalSTT)
    assert provider.config == LocalSTTConfig()
    assert config == LocalSTTConfig(model="base")
    assert plan.selected["stt"].required_env is None
    assert plan.missing_env == ()
    assert "localstt" not in _provider_env()
    assert "localstt" not in init_module._provider_to_env_var()


def test_registered_stt_capabilities_drive_endpointing_and_planner() -> None:
    from easycat.config.easy import _stt_uses_native_endpointing
    from easycat.planning._resolution import _decide_catalog_role

    register_stt_provider(
        "fakestt",
        FakeSTT,
        FakeSTTConfig,
        env_var="FAKESTT_API_KEY",
        capabilities=frozenset({"native_endpointing", "word_timestamps"}),
    )
    config = FakeSTTConfig(api_key="k")

    assert _stt_uses_native_endpointing(config) is True
    selection = _decide_catalog_role("stt", config, catalog=STT_CATALOG)
    assert selection.capabilities == frozenset({"native_endpointing", "word_timestamps"})


def test_registered_stt_capability_resolver_can_vary_by_config_or_model() -> None:
    from easycat.planning._resolution import _decide_catalog_role

    def resolve_capabilities(config: object, model: str | None) -> frozenset[str]:
        selected_model = config.model if isinstance(config, FakeSTTConfig) else model
        if selected_model and selected_model.endswith("-native"):
            return frozenset({"native_endpointing"})
        return frozenset()

    register_stt_provider(
        "fakestt",
        FakeSTT,
        FakeSTTConfig,
        env_var="FAKESTT_API_KEY",
        capabilities=frozenset({"word_timestamps"}),
        capability_resolver=resolve_capabilities,
    )

    native = FakeSTTConfig(api_key="k", model="fake-native")
    standard = FakeSTTConfig(api_key="k", model="fake-standard")

    assert STT_CATALOG.capabilities_for_config(native) == frozenset(
        {"native_endpointing", "word_timestamps"}
    )
    assert STT_CATALOG.capabilities_for_config(standard) == frozenset({"word_timestamps"})
    assert STT_CATALOG.capabilities_for("fakestt", model="other-native") == frozenset(
        {"native_endpointing", "word_timestamps"}
    )
    assert _decide_catalog_role("stt", native, catalog=STT_CATALOG).capabilities == frozenset(
        {"native_endpointing", "word_timestamps"}
    )


def test_registered_probe_module_overrides_non_importable_extra_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.util

    from easycat.planning import build_provider_plan
    from easycat.planning.transport_registry import probe_module_for_extra
    from easycat.project.schema import VoiceProfile

    register_stt_provider(
        "fakestt",
        FakeSTT,
        FakeSTTConfig,
        env_var="FAKESTT_API_KEY",
        extra="acme-speech",
        probe_module="acme_speech",
    )

    assert probe_module_for_extra("acme-speech") == "acme_speech"
    real_find_spec = importlib.util.find_spec
    probed: list[str] = []

    def fake_find_spec(name: str, package: str | None = None):
        probed.append(name)
        if name == "acme_speech":
            return object()
        return real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    plan = build_provider_plan(
        VoiceProfile(
            name="extension",
            transport="websocket",
            stt="fakestt",
            tts="openai",
        ),
        environ={"FAKESTT_API_KEY": "x", "OPENAI_API_KEY": "y"},
    )

    assert "acme_speech" in probed
    assert "acme-speech" not in plan.missing_extras


def test_selected_provider_without_explicit_probe_falls_back_to_extra_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.util

    from easycat.planning import build_provider_plan
    from easycat.project.schema import VoiceProfile

    register_stt_provider(
        "fakestt",
        FakeSTT,
        FakeSTTConfig,
        env_var="FAKESTT_API_KEY",
        extra="acme_speech",
    )
    real_find_spec = importlib.util.find_spec
    probed: list[str] = []

    def fake_find_spec(name: str, package: str | None = None):
        probed.append(name)
        if name == "acme_speech":
            return object()
        return real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    plan = build_provider_plan(
        VoiceProfile(
            name="extension",
            transport="websocket",
            stt="fakestt",
            tts="openai",
        ),
        environ={"FAKESTT_API_KEY": "x", "OPENAI_API_KEY": "y"},
    )

    assert "acme_speech" in probed
    assert "acme_speech" not in plan.missing_extras


def test_selected_provider_probe_keeps_identity_when_extra_name_is_shared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.util

    from easycat.planning import build_provider_plan
    from easycat.project.schema import VoiceProfile

    register_stt_provider(
        "fakestt",
        FakeSTT,
        FakeSTTConfig,
        env_var="FAKESTT_API_KEY",
        extra="webrtc",
        probe_module="acme_speech",
    )
    probed: list[str] = []

    def fake_find_spec(name: str, package: str | None = None):
        _ = package
        probed.append(name)
        return object() if name in {"acme_speech", "websockets"} else None

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    plan = build_provider_plan(
        VoiceProfile(
            name="shared-extra",
            transport="websocket",
            stt="fakestt",
            tts="openai",
        ),
        environ={"FAKESTT_API_KEY": "x", "OPENAI_API_KEY": "y"},
    )

    assert "acme_speech" in probed
    assert "aiortc" not in probed
    assert "webrtc" not in plan.missing_extras


# ── Layer 2: entry-point discovery ───────────────────────────────


class _FakeEntryPoint:
    def __init__(self, name: str, registrar) -> None:
        self.name = name
        self._registrar = registrar

    def load(self):
        return self._registrar


def _patch_entry_points(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, list]) -> None:
    def fake_entry_points(*, group: str):
        return mapping.get(group, [])

    monkeypatch.setattr("importlib.metadata.entry_points", fake_entry_points)


def test_entry_point_discovery_registers_stt_and_tts_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_entry_points(
        monkeypatch,
        {
            "easycat.stt_providers": [_FakeEntryPoint("fakestt", _register_fake_stt)],
            "easycat.tts_providers": [_FakeEntryPoint("faketts", _register_fake_tts)],
        },
    )
    object.__setattr__(STT_CATALOG, "_discovered", False)
    object.__setattr__(TTS_CATALOG, "_discovered", False)

    # First factory call triggers discovery.
    assert "fakestt" in available_stt_providers()
    assert "faketts" in available_tts_providers()
    provider = create_stt_provider(STTProviderConfig(provider="fakestt", api_key="k"))
    assert isinstance(provider, FakeSTT)


def test_broken_entry_point_logs_warning_and_does_not_break_factories(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken_registrar() -> None:
        raise RuntimeError("plugin import exploded")

    _patch_entry_points(
        monkeypatch,
        {"easycat.stt_providers": [_FakeEntryPoint("broken", broken_registrar)]},
    )
    object.__setattr__(STT_CATALOG, "_discovered", False)

    with caplog.at_level("WARNING", logger="easycat"):
        names = available_stt_providers()

    assert "broken" not in names
    assert {"openai", "deepgram", "elevenlabs", "cartesia"} <= set(names)
    assert any("entry point" in record.message for record in caplog.records)


# ── Downstream surfaces ──────────────────────────────────────────


def test_registered_provider_surfaces_in_doctor_env_checks() -> None:
    from easycat.cli.diagnose.doctor import _provider_env

    _register_fake_stt()
    env = _provider_env()
    assert env["fakestt"] == "FAKESTT_API_KEY"
    # Built-ins are still covered, deduped by env var.
    assert env["openai"] == "OPENAI_API_KEY"
    assert "openai-realtime" not in env


def test_registered_provider_surfaces_in_scaffold_validation() -> None:
    from easycat.cli.scaffold import init as init_module

    _register_fake_tts()
    assert "faketts" in available_tts_providers()
    assert init_module._provider_to_env_var()["faketts"] == "FAKETTS_API_KEY"
    # Scaffold validation accepts the registered shortcut.
    init_module._validate_provider_spec("faketts/fake-1", available_tts_providers(), kind="TTS")
