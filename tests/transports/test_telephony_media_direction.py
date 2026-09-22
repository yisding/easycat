"""Shared telephony-media direction marker (``call_event_direction``, gh 1157)."""

from __future__ import annotations

import pytest

from easycat.session import CallIdentity
from easycat.transports._telephony_media import call_event_direction


@pytest.mark.parametrize("direction", ["inbound", "outbound"])
def test_parsed_direction_is_passed_through(direction: str) -> None:
    identity = CallIdentity(direction=direction)  # type: ignore[arg-type]
    assert call_event_direction(identity) == direction


def test_unknown_direction_maps_to_none() -> None:
    """``"unknown"`` is not provably inbound, so the guards must still adopt it."""
    assert call_event_direction(CallIdentity(direction="unknown")) is None


def test_missing_identity_maps_to_none_without_raising() -> None:
    """A ``CallEnded`` emitted before any ``start`` frame was parsed must not crash."""
    assert call_event_direction(None) is None
    assert call_event_direction(CallIdentity()) is None
    assert call_event_direction(object()) is None
