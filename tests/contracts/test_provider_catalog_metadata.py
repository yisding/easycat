"""Guard: the provider catalogs are the single source of provider metadata.

Doctor's env checks, scaffold's extras/env hints, validation's provider
markers, and redaction's sensitive-URL regex all derive from the STT/TTS
``ProviderCatalog`` instances. This guard keeps every registered provider
fully described and keeps doctor's check set equal to the catalog.
"""

from __future__ import annotations

import pytest

from easycat._provider_catalog import (
    credential_env_vars,
    provider_env_vars,
    stt_tts_catalogs,
)
from easycat.cli.diagnose import doctor

pytestmark = pytest.mark.contract


def test_every_registered_provider_has_catalog_metadata_and_doctor_tracks_it() -> None:
    for catalog in stt_tts_catalogs():
        for provider in catalog.providers:
            label = f"{catalog.kind} provider {provider!r}"
            assert catalog.env_vars[provider], f"{label} has no credential env var"
            assert catalog.extras[provider], f"{label} has no install extra"
            assert catalog.api_domains[provider], f"{label} has no API domains"
            assert all(catalog.api_domains[provider]), f"{label} has an empty API domain"

    # Doctor checks exactly the catalog's credential set: one check per
    # distinct credential env var, and a probe URL for each checked
    # built-in provider (discovered third-party providers carry no
    # static probe URL and are skipped by the reachability check).
    provider_env = doctor._provider_env()
    assert provider_env == credential_env_vars()
    assert set(doctor._PROVIDER_PROBE_URL) == set(provider_env)
    assert sorted(provider_env.values()) == sorted(set(provider_env_vars().values()))
