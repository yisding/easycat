"""Tests for provider Protocol definitions — verify structural subtyping works."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from threading import Event as ThreadEvent
from threading import Thread

import pytest

from easycat._provider_catalog import ProviderCatalog, ProviderSpec
from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.errors import EasyCatError, EasyConfigError
from easycat.events import (
    Event,
    STTEvent,
    STTEventType,
    TTSEvent,
    TTSEventType,
    VADStartSpeaking,
)
from easycat.providers import (
    NoiseReducer,
    STTProvider,
    Transport,
    TransportLike,
    TTSProvider,
    VADProvider,
)
from easycat.tts.input import TTSInput

# ── Stub implementations ──────────────────────────────────────────


_STUB_VERSION = {
    "provider": "stub",
    "model": "unknown",
    "api_version": "unknown",
    "sdk_version": "unknown",
}


class StubSTT:
    async def start_stream(self) -> None:
        pass

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def commit_segment(self) -> bool:
        return True

    async def end_stream(self) -> None:
        pass

    async def events(self) -> AsyncIterator[STTEvent]:
        yield STTEvent(type=STTEventType.FINAL, text="stub")

    def version_info(self) -> dict[str, str]:
        return _STUB_VERSION


class StubTTS:
    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        yield TTSEvent(
            type=TTSEventType.AUDIO,
            audio=AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K),
        )

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass

    def version_info(self) -> dict[str, str]:
        return _STUB_VERSION


class StubVAD:
    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        yield VADStartSpeaking()

    def configure(
        self,
        *,
        min_speech_duration_ms: int = 250,
        min_silence_duration_ms: int = 150,
        sensitivity: float = 0.5,
    ) -> None:
        pass

    def version_info(self) -> dict[str, str]:
        return _STUB_VERSION


class StubNoiseReducer:
    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk

    def version_info(self) -> dict[str, str]:
        return _STUB_VERSION


class StubTransport:
    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        yield AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K)

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def clear_audio(self) -> None:
        pass

    def version_info(self) -> dict[str, str]:
        return _STUB_VERSION


# ── Protocol conformance tests ────────────────────────────────────


def test_stub_stt_is_stt_provider():
    assert isinstance(StubSTT(), STTProvider)


def test_stub_tts_is_tts_provider():
    assert isinstance(StubTTS(), TTSProvider)


def test_stub_vad_is_vad_provider():
    assert isinstance(StubVAD(), VADProvider)


def test_stub_noise_reducer_is_noise_reducer():
    assert isinstance(StubNoiseReducer(), NoiseReducer)


def test_stub_transport_is_transport():
    assert isinstance(StubTransport(), Transport)


class LegacyTransport:
    """A custom transport satisfying the audio contract but lacking version_info()."""

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        yield AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K)

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def clear_audio(self) -> None:
        pass


def test_legacy_transport_lacks_version_info_fails_full_protocol():
    # The full Transport protocol now requires version_info(); a transport that
    # predates that contract no longer satisfies isinstance(..., Transport).
    assert not isinstance(LegacyTransport(), Transport)


def test_legacy_transport_satisfies_transport_like():
    # ...but it still matches the narrow audio contract used to discriminate a
    # pre-built transport instance from a transport config in _create_transport.
    assert isinstance(LegacyTransport(), TransportLike)
    assert isinstance(StubTransport(), TransportLike)


class _NoClearTransport:
    """A transport with no ``clear_audio`` — outbound buffering is optional."""

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        yield AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K)

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    def version_info(self) -> dict[str, str]:
        return _STUB_VERSION


def test_transport_without_clear_audio_satisfies_protocol():
    # ``clear_audio`` is an optional capability discovered structurally by the
    # Session; omitting it must not fail the runtime_checkable Transport protocol.
    assert isinstance(_NoClearTransport(), Transport)


class _CatalogProvider:
    def __init__(self, config: "_CatalogConfig") -> None:
        self.config = config


class _OtherCatalogProvider:
    def __init__(self, config: "_CatalogConfig") -> None:
        self.config = config


@dataclass
class _CatalogConfig:
    api_key: str = ""
    option: str = ""
    event_bus: object | None = None


def _catalog_kwargs() -> dict:
    return {
        "specs": {
            "known": ProviderSpec(
                _CatalogProvider,
                _CatalogConfig,
                "KNOWN_API_KEY",
                "known",
                ("known.example",),
            )
        },
        "kind": "Test",
    }


def test_provider_catalog_normalizes_and_validates_names():
    catalog = ProviderCatalog(**_catalog_kwargs())

    assert catalog.validate_name(" KNOWN ") == "known"
    with pytest.raises(EasyCatError, match="Did you mean 'known'") as exc_info:
        catalog.validate_name("knwn")
    assert exc_info.value.code == "EASYCAT_E104"


def test_provider_catalog_creates_named_provider():
    catalog = ProviderCatalog(**_catalog_kwargs())

    provider = catalog.create_provider(
        "known",
        params={"api_key": "nested", "option": "value"},
        api_key="top-level",
    )

    assert provider.config == _CatalogConfig(api_key="top-level", option="value")


def test_provider_catalog_rejects_missing_key_and_invalid_params():
    catalog = ProviderCatalog(**_catalog_kwargs())

    with pytest.raises(EasyConfigError, match="API key is required") as missing:
        catalog.create_provider("known")
    assert isinstance(missing.value, EasyCatError)
    with pytest.raises(EasyConfigError, match="API key is required"):
        catalog.create_provider("known", api_key=" \t ")

    with pytest.raises(EasyConfigError, match="Invalid params") as invalid:
        catalog.create_provider("known", params={"unknown": True})
    assert isinstance(invalid.value, EasyCatError)


def test_provider_catalog_parse_rejects_blank_override_and_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = ProviderCatalog(**_catalog_kwargs())

    with pytest.raises(EasyCatError) as override_error:
        catalog.parse_string(
            "known/model",
            api_key_overrides={"KNOWN_API_KEY": "   "},
        )
    assert override_error.value.code == "EASYCAT_E203"

    monkeypatch.setenv("KNOWN_API_KEY", "\t ")
    with pytest.raises(EasyCatError) as env_error:
        catalog.parse_string("known/model")
    assert env_error.value.code == "EASYCAT_E203"


def test_provider_catalog_injects_or_preserves_event_bus():
    catalog = ProviderCatalog(**_catalog_kwargs())
    injected = object()
    existing = object()

    created = catalog.create_from_config(_CatalogConfig(api_key="key"), injected)
    preserved = catalog.create_from_config(
        _CatalogConfig(api_key="key", event_bus=existing), object()
    )

    assert created.config.event_bus is injected
    assert preserved.config.event_bus is existing


def test_provider_catalog_discovery_is_atomic_across_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_started = ThreadEvent()
    release_callback = ThreadEvent()
    second_started = ThreadEvent()
    second_finished = ThreadEvent()
    results: list[tuple[str, ...]] = []
    errors: list[BaseException] = []

    catalog = ProviderCatalog(
        specs={},
        kind="Test",
        entry_point_group="easycat.test_providers",
    )

    class _EntryPoint:
        name = "slow"

        def load(self):
            def register() -> None:
                callback_started.set()
                if not release_callback.wait(timeout=2):
                    raise RuntimeError("test entry-point callback was not released")
                catalog.register(
                    "plugin",
                    _CatalogProvider,
                    _CatalogConfig,
                    api_domains=("api.plugin.test",),
                )

            return register

    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda *, group: [_EntryPoint()] if group == catalog.entry_point_group else [],
    )

    def discover(
        *,
        started: ThreadEvent | None = None,
        finished: ThreadEvent | None = None,
    ) -> None:
        if started is not None:
            started.set()
        try:
            catalog.discover()
            results.append(
                tuple(domain for domains in catalog.api_domains.values() for domain in domains)
            )
        except BaseException as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            errors.append(exc)
        finally:
            if finished is not None:
                finished.set()

    first = Thread(target=discover)
    second = Thread(
        target=discover,
        kwargs={"started": second_started, "finished": second_finished},
    )
    first.start()
    assert callback_started.wait(timeout=1)
    second.start()
    assert second_started.wait(timeout=1)
    assert not second_finished.wait(timeout=0.1)

    release_callback.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert results == [("api.plugin.test",), ("api.plugin.test",)]
    assert catalog.api_domains["plugin"] == ("api.plugin.test",)


def test_provider_catalog_direct_registration_waits_for_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_started = ThreadEvent()
    release_callback = ThreadEvent()
    registration_started = ThreadEvent()
    registration_finished = ThreadEvent()
    catalog = ProviderCatalog(
        specs={},
        kind="Test",
        entry_point_group="easycat.test_providers",
    )

    class _EntryPoint:
        name = "slow"

        def load(self):
            def register() -> None:
                callback_started.set()
                assert release_callback.wait(timeout=2)
                catalog.register("discovered", _CatalogProvider, _CatalogConfig)

            return register

    monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kwargs: [_EntryPoint()])

    discovery = Thread(target=catalog.discover)

    def register_directly() -> None:
        registration_started.set()
        catalog.register("direct", _CatalogProvider, _CatalogConfig)
        registration_finished.set()

    registration = Thread(target=register_directly)
    discovery.start()
    assert callback_started.wait(timeout=1)
    registration.start()
    assert registration_started.wait(timeout=1)
    assert not registration_finished.wait(timeout=0.1)

    release_callback.set()
    discovery.join(timeout=2)
    registration.join(timeout=2)

    assert not discovery.is_alive()
    assert not registration.is_alive()
    assert set(catalog.providers) == {"direct", "discovered"}


def test_provider_catalog_discovery_is_reentrant_on_owner_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = ProviderCatalog(
        specs={},
        kind="Test",
        entry_point_group="easycat.test_providers",
    )
    callback_calls = 0

    class _EntryPoint:
        name = "recursive"

        def load(self):
            def register() -> None:
                nonlocal callback_calls
                callback_calls += 1
                catalog.discover()
                catalog.register("plugin", _CatalogProvider, _CatalogConfig)

            return register

    monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kwargs: [_EntryPoint()])

    catalog.discover()
    catalog.discover()

    assert callback_calls == 1
    assert catalog.providers["plugin"] == (_CatalogProvider, _CatalogConfig)
    assert catalog._discovered is True


def test_provider_catalog_retries_entry_point_enumeration_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = ProviderCatalog(
        specs={},
        kind="Test",
        entry_point_group="easycat.test_providers",
    )
    attempts = 0

    class _EntryPoint:
        name = "retry"

        def load(self):
            return lambda: catalog.register("plugin", _CatalogProvider, _CatalogConfig)

    def entry_points(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("metadata unavailable")
        return [_EntryPoint()]

    monkeypatch.setattr("importlib.metadata.entry_points", entry_points)

    catalog.discover()
    assert catalog._discovered is False
    assert catalog.providers == {}

    catalog.discover()
    assert attempts == 2
    assert catalog._discovered is True
    assert catalog.providers["plugin"] == (_CatalogProvider, _CatalogConfig)


@pytest.mark.parametrize("api_key", ["", " \t "])
def test_provider_catalog_rejects_unusable_key_from_concrete_config(api_key: str) -> None:
    catalog = ProviderCatalog(**_catalog_kwargs())

    with pytest.raises(ValueError, match="API key is required"):
        catalog.create_from_config(_CatalogConfig(api_key=api_key), object())


def test_provider_catalog_rejects_config_class_mapped_to_different_provider():
    catalog = ProviderCatalog(**_catalog_kwargs())

    with pytest.raises(ValueError, match="config class '_CatalogConfig'.*different"):
        catalog.register("other", _OtherCatalogProvider, _CatalogConfig)

    assert "other" not in catalog.providers
    assert catalog.provider_for_config(_CatalogConfig) is _CatalogProvider


def test_provider_catalog_rejects_ambiguous_initial_specs():
    specs = {
        "first": ProviderSpec(
            _CatalogProvider,
            _CatalogConfig,
            None,
            "",
            (),
        ),
        "second": ProviderSpec(
            _OtherCatalogProvider,
            _CatalogConfig,
            None,
            "",
            (),
        ),
    }

    with pytest.raises(ValueError, match="config class '_CatalogConfig'.*different"):
        ProviderCatalog(specs=specs, kind="Test")


def test_provider_catalog_rejects_initial_alias_with_different_metadata():
    specs = {
        "plain": ProviderSpec(
            _CatalogProvider,
            _CatalogConfig,
            None,
            "",
            (),
        ),
        "native": ProviderSpec(
            _CatalogProvider,
            _CatalogConfig,
            None,
            "",
            (),
            capabilities=frozenset({"native_endpointing"}),
        ),
    }

    with pytest.raises(ValueError, match="alias 'native'.*identical metadata"):
        ProviderCatalog(specs=specs, kind="Test")


def test_provider_catalog_allows_alias_for_same_provider_and_config():
    catalog = ProviderCatalog(**_catalog_kwargs())

    catalog.register(
        "known-alias",
        _CatalogProvider,
        _CatalogConfig,
        env_var="KNOWN_API_KEY",
        extra="known",
        api_domains=("known.example",),
    )

    assert catalog.providers["known-alias"] == (_CatalogProvider, _CatalogConfig)
    assert catalog.provider_for_config(_CatalogConfig) is _CatalogProvider


def test_provider_catalog_rejects_dynamic_alias_with_different_metadata():
    catalog = ProviderCatalog(**_catalog_kwargs())

    with pytest.raises(ValueError, match="alias 'known-native'.*identical metadata"):
        catalog.register(
            "known-native",
            _CatalogProvider,
            _CatalogConfig,
            env_var="KNOWN_API_KEY",
            extra="known",
            api_domains=("known.example",),
            capabilities=frozenset({"native_endpointing"}),
        )

    assert "known-native" not in catalog.providers
