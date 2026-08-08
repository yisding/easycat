"""Downstream type-checking contracts for EasyConfig preset ergonomics."""

from __future__ import annotations

from dataclasses import fields

from easycat import EasyConfig
from easycat.config.easy import _EasyConfigPresetKwargs


def test_preset_keyword_schema_tracks_every_easyconfig_constructor_field() -> None:
    """A new dataclass field must also become discoverable on every preset."""
    constructor_fields = {item.name for item in fields(EasyConfig) if item.init}
    typed_fields = set(_EasyConfigPresetKwargs.__required_keys__) | set(
        _EasyConfigPresetKwargs.__optional_keys__
    )

    assert _EasyConfigPresetKwargs.__required_keys__ == frozenset()
    assert typed_fields == constructor_fields
