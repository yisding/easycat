"""Debugger replay request validation that does not require aiohttp."""

from __future__ import annotations

import pytest

from easycat.debugger._sources import _validated_replay_kwargs


@pytest.mark.parametrize("timing", [None, True, [], "slow"])
def test_replay_validation_rejects_invalid_timing(timing: object) -> None:
    with pytest.raises(ValueError, match="timing"):
        _validated_replay_kwargs({"timing": timing})


def test_replay_validation_preserves_valid_timing() -> None:
    assert _validated_replay_kwargs({"timing": "wall"}) == {"timing": "wall"}
