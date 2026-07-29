"""Registration contracts for VAD, noise-reduction, and echo-cancellation stages."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest

from easycat import (
    AudioChunk,
    Event,
    available_vad_providers,
    create_vad,
    register_vad_provider,
)
from easycat._provider_catalog import provider_env_vars, provider_extras
from easycat.config import EasyConfig
from easycat.echo_cancellation import (
    _CATALOG as ECHO_CATALOG,
)
from easycat.echo_cancellation import (
    available_echo_canceller_providers,
    create_echo_canceller,
    parse_echo_canceller_string,
    register_echo_canceller_provider,
)
from easycat.noise_reduction import (
    _CATALOG as NOISE_CATALOG,
)
from easycat.noise_reduction import (
    available_noise_reducer_providers,
    create_noise_reducer,
    parse_noise_reducer_string,
    register_noise_reducer_provider,
)
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTTConfig
from easycat.tts.openai_tts import OpenAITTSConfig
from easycat.vad.factory import _CATALOG as VAD_CATALOG
from easycat.vad.factory import parse_vad_string


@dataclass
class FakeVADConfig:
    model: str = "energy-v1"


class FakeVAD:
    def __init__(self, config: FakeVADConfig) -> None:
        self.config = config

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        del chunk
        if False:
            yield Event()

    def configure(
        self,
        *,
        min_speech_duration_ms: int = 250,
        min_silence_duration_ms: int = 150,
        sensitivity: float = 0.5,
    ) -> None:
        del min_speech_duration_ms, min_silence_duration_ms, sensitivity

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "fake-vad",
            "model": self.config.model,
            "api_version": "v1",
            "sdk_version": "none",
        }


@dataclass
class FakeNoiseConfig:
    model: str = "denoise-v1"


class FakeNoiseReducer:
    def __init__(self, config: FakeNoiseConfig) -> None:
        self.config = config

    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "fake-noise",
            "model": self.config.model,
            "api_version": "v1",
            "sdk_version": "none",
        }


@dataclass
class FakeEchoConfig:
    model: str = "echo-v1"


class FakeEchoCanceller:
    def __init__(self, config: FakeEchoConfig) -> None:
        self.config = config

    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk

    def feed_reference(self, chunk: AudioChunk) -> None:
        del chunk

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "fake-echo",
            "model": self.config.model,
            "api_version": "v1",
            "sdk_version": "none",
        }


@pytest.fixture(autouse=True)
def restore_audio_stage_catalogs():
    snapshots = []
    for catalog in (VAD_CATALOG, NOISE_CATALOG, ECHO_CATALOG):
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


def _easy_config(**kwargs) -> EasyConfig:
    return EasyConfig(
        stt=OpenAIRealtimeSTTConfig(api_key="test"),
        tts=OpenAITTSConfig(api_key="test"),
        debug="off",
        **kwargs,
    )


def _register_fake_vad() -> None:
    register_vad_provider(
        "fake-vad",
        FakeVAD,
        FakeVADConfig,
        extra="fake-audio",
        probe_module="fake_audio",
        capabilities=frozenset({"offline"}),
    )


def _register_fake_noise() -> None:
    register_noise_reducer_provider(
        "fake-noise",
        FakeNoiseReducer,
        FakeNoiseConfig,
        capabilities=frozenset({"offline"}),
    )


def _register_fake_echo() -> None:
    register_echo_canceller_provider(
        "fake-echo",
        FakeEchoCanceller,
        FakeEchoConfig,
        capabilities=frozenset({"full_duplex"}),
    )


def test_registered_vad_reaches_shortcut_factory_easyconfig_and_plan() -> None:
    from easycat.planning import build_provider_plan
    from easycat.planning.transport_registry import probe_module_for_extra

    _register_fake_vad()

    parsed = parse_vad_string("fake-vad/energy-v2")
    provider = create_vad(parsed)
    config = _easy_config(vad="fake-vad/energy-v3")
    plan = build_provider_plan(config, environ={})

    assert "fake-vad" in available_vad_providers()
    assert parsed == FakeVADConfig(model="energy-v2")
    assert isinstance(provider, FakeVAD)
    assert isinstance(config.vad, FakeVADConfig)
    assert plan.selected["vad"].provider == "fake-vad"
    assert plan.selected["vad"].model == "energy-v3"
    assert plan.selected["vad"].extra == "fake-audio"
    assert plan.selected["vad"].capabilities == frozenset({"offline"})
    assert "fake-audio" in plan.missing_extras
    assert probe_module_for_extra("fake-audio") == "fake_audio"


def test_registered_vad_round_trips_through_manifest_and_profile_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from easycat.planning import build_provider_plan
    from easycat.project.loader import parse_manifest

    _register_fake_vad()
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    manifest = parse_manifest(
        {"voice": {"default": {"transport": "websocket", "vad": "fake-vad/energy-v4"}}}
    )

    config = manifest.to_easyconfig("default", resolve_agent=False)
    plan = build_provider_plan(manifest.profile("default"), environ={"OPENAI_API_KEY": "test"})

    assert config.vad == FakeVADConfig(model="energy-v4")
    assert plan.selected["vad"].provider == "fake-vad"
    assert plan.selected["vad"].model == "energy-v4"


def test_registered_noise_and_echo_stages_reach_easyconfig_factories_and_plan() -> None:
    from easycat.planning import build_provider_plan

    _register_fake_noise()
    _register_fake_echo()

    noise_config = parse_noise_reducer_string("fake-noise/denoise-v2")
    echo_config = parse_echo_canceller_string("fake-echo/echo-v2")
    config = _easy_config(
        noise_reduction="fake-noise/denoise-v3",
        echo_cancellation="fake-echo/echo-v3",
    )
    plan = build_provider_plan(config, environ={})

    assert "fake-noise" in available_noise_reducer_providers()
    assert "fake-echo" in available_echo_canceller_providers()
    assert isinstance(create_noise_reducer(noise_config), FakeNoiseReducer)
    assert isinstance(create_echo_canceller(echo_config), FakeEchoCanceller)
    assert config.noise_reduction == FakeNoiseConfig(model="denoise-v3")
    assert config.echo_cancellation == FakeEchoConfig(model="echo-v3")
    assert plan.selected["noise_reducer"].provider == "fake-noise"
    assert plan.selected["echo_canceller"].provider == "fake-echo"


def test_live_audio_stage_instances_keep_identity_and_are_reported_accurately() -> None:
    from easycat.config._factory import _create_vad, _resolve_echo_canceller
    from easycat.config._factory import _resolve_noise_reducer as resolve_noise
    from easycat.planning import build_provider_plan

    vad = FakeVAD(FakeVADConfig())
    noise = FakeNoiseReducer(FakeNoiseConfig())
    echo = FakeEchoCanceller(FakeEchoConfig())
    config = _easy_config(vad=vad, noise_reduction=noise, echo_cancellation=echo)
    plan = build_provider_plan(config, environ={})

    assert create_vad(vad) is vad
    assert create_noise_reducer(noise) is noise
    assert create_echo_canceller(echo) is echo
    assert _create_vad(vad) is vad
    assert resolve_noise(noise) is noise
    assert _resolve_echo_canceller(echo) is echo
    assert plan.selected["vad"].provider == "FakeVAD"
    assert plan.selected["noise_reducer"].provider == "FakeNoiseReducer"
    assert plan.selected["echo_canceller"].provider == "FakeEchoCanceller"
    assert plan.selected["vad"].capabilities == frozenset({"injected"})


class _FakeEntryPoint:
    def __init__(self, name: str, registrar) -> None:
        self.name = name
        self._registrar = registrar

    def load(self):
        return self._registrar


def test_audio_stage_entry_points_discover_all_three_catalogs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapping = {
        "easycat.vad_providers": [_FakeEntryPoint("fake-vad", _register_fake_vad)],
        "easycat.noise_reducer_providers": [_FakeEntryPoint("fake-noise", _register_fake_noise)],
        "easycat.echo_canceller_providers": [_FakeEntryPoint("fake-echo", _register_fake_echo)],
    }

    def fake_entry_points(*, group: str):
        return mapping.get(group, [])

    monkeypatch.setattr("importlib.metadata.entry_points", fake_entry_points)
    for catalog in (VAD_CATALOG, NOISE_CATALOG, ECHO_CATALOG):
        object.__setattr__(catalog, "_discovered", False)

    assert "fake-vad" in available_vad_providers()
    assert "fake-noise" in available_noise_reducer_providers()
    assert "fake-echo" in available_echo_canceller_providers()


def test_audio_stage_registration_rejects_builtin_names() -> None:
    with pytest.raises(ValueError, match="reserved"):
        register_vad_provider("silero", FakeVAD, FakeVADConfig)
    with pytest.raises(ValueError, match="reserved"):
        register_noise_reducer_provider("rnnoise", FakeNoiseReducer, FakeNoiseConfig)
    with pytest.raises(ValueError, match="reserved"):
        register_echo_canceller_provider("livekit", FakeEchoCanceller, FakeEchoConfig)


def test_audio_stage_metadata_cannot_overwrite_speech_provider_names() -> None:
    register_vad_provider(
        "deepgram",
        FakeVAD,
        FakeVADConfig,
        env_var="FAKE_VAD_API_KEY",
        extra="fake-audio",
    )

    assert provider_env_vars()["deepgram"] == "DEEPGRAM_API_KEY"
    assert provider_extras()["deepgram"] == "deepgram"
