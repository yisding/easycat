"""Cross-family aggregation for EasyCat's audio-provider catalogs.

Provider-family modules import only :mod:`easycat._provider_catalog`, which
contains the registration primitive. Consumers that need a global view use
this module so STT, TTS, and VAD remain independent import families.
"""

from __future__ import annotations

from easycat._provider_catalog import ProviderCatalog


def stt_tts_catalogs() -> tuple[ProviderCatalog, ProviderCatalog]:
    """Return the discovered STT and TTS catalogs."""
    from easycat.stt.factory import _CATALOG as stt_catalog
    from easycat.tts.factory import _CATALOG as tts_catalog

    stt_catalog.discover()
    tts_catalog.discover()
    return (stt_catalog, tts_catalog)


def provider_catalogs() -> tuple[ProviderCatalog, ...]:
    """Return every discovered provider catalog across the audio pipeline."""
    from easycat.echo_cancellation import _CATALOG as echo_canceller_catalog
    from easycat.noise_reduction import _CATALOG as noise_reducer_catalog
    from easycat.vad.factory import _CATALOG as vad_catalog

    catalogs = (
        *stt_tts_catalogs(),
        vad_catalog,
        noise_reducer_catalog,
        echo_canceller_catalog,
    )
    for catalog in catalogs:
        catalog.discover()
    return catalogs


def provider_names() -> frozenset[str]:
    """Every registered provider name, merged across audio-pipeline catalogs."""
    return frozenset(name for catalog in provider_catalogs() for name in catalog.providers)


def _provider_metadata_catalogs() -> tuple[ProviderCatalog, ...]:
    """Order catalogs so speech-provider metadata wins name collisions."""
    catalogs = provider_catalogs()
    audio_stages = tuple(catalog for catalog in catalogs if catalog.kind not in {"STT", "TTS"})
    speech = tuple(catalog for catalog in catalogs if catalog.kind in {"STT", "TTS"})
    return (*audio_stages, *speech)


def provider_env_vars() -> dict[str, str]:
    """Credentialed provider → env var, with STT/TTS names authoritative."""
    return {
        name: env_var
        for catalog in _provider_metadata_catalogs()
        for name, env_var in catalog.env_vars.items()
        if env_var is not None
    }


def provider_extras() -> dict[str, str]:
    """Provider → install extra, with STT/TTS names authoritative."""
    return {
        name: extra
        for catalog in _provider_metadata_catalogs()
        for name, extra in catalog.extras.items()
    }


def provider_probe_modules() -> dict[str, str]:
    """Install extra → explicit import probe declared by a provider."""
    return {
        extra: probe_module
        for catalog in provider_catalogs()
        for name, extra in catalog.extras.items()
        if extra and (probe_module := catalog.probe_modules.get(name)) is not None
    }


def credential_env_vars() -> dict[str, str]:
    """Provider → env var, deduplicated by credential."""
    merged = provider_env_vars()
    deduped: dict[str, str] = {}
    claimed_vars: set[str] = set()
    for provider in sorted(merged):
        env_var = merged[provider]
        if env_var in claimed_vars:
            continue
        deduped[provider] = env_var
        claimed_vars.add(env_var)
    return deduped


def sensitive_api_domains() -> tuple[str, ...]:
    """Sorted union of every provider API domain, for URL redaction."""
    return tuple(
        sorted(
            {
                domain
                for catalog in provider_catalogs()
                for domains in catalog.api_domains.values()
                for domain in domains
            }
        )
    )
