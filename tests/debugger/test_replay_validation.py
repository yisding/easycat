"""Debugger replay request validation that does not require aiohttp."""

from __future__ import annotations

import pytest

from easycat.debugger._sources import DebuggerSource, _validated_replay_kwargs
from easycat.debugger.server import _DebuggerRoutes


@pytest.mark.parametrize("timing", [None, True, [], "slow"])
def test_replay_validation_rejects_invalid_timing(timing: object) -> None:
    with pytest.raises(ValueError, match="timing"):
        _validated_replay_kwargs({"timing": timing})


def test_replay_validation_preserves_valid_timing() -> None:
    assert _validated_replay_kwargs({"timing": "wall"}) == {"timing": "wall"}


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("fidelity", []),
        ("fidelity", "unknown"),
        ("tool_policy", {}),
        ("tool_policy", "unknown"),
    ],
)
def test_replay_validation_rejects_invalid_enum_controls(name: str, value: object) -> None:
    with pytest.raises(ValueError, match=name):
        _validated_replay_kwargs({name: value})


def test_replay_validation_preserves_valid_enum_controls() -> None:
    assert _validated_replay_kwargs({"fidelity": "simulated", "tool_policy": "stub"}) == {
        "fidelity": "simulated",
        "tool_policy": "stub",
    }


@pytest.mark.parametrize(
    ("name", "value"),
    [("fidelity", []), ("tool_policy", {})],
)
async def test_replay_route_maps_unhashable_enum_controls_to_bad_request(
    name: str, value: object
) -> None:
    class Web:
        @staticmethod
        def json_response(payload: object, *, status: int) -> tuple[int, object]:
            return status, payload

    class Request:
        async def json(self) -> dict[str, object]:
            return {name: value}

    source = DebuggerSource(
        label="test",
        _records_fn=list,
        _artifact_fn=lambda _ref: None,
        _manifest_fn=lambda: {"supports_replay": True},
    )
    routes = _DebuggerRoutes(
        source,
        web=Web,
        ws_msg_type=None,
        allow_remote=False,
        registry=None,
    )

    status, payload = await routes.replay(Request())

    assert status == 400
    assert isinstance(payload, dict)
    assert payload["error_code"] == "BAD_REQUEST"
    assert name in str(payload["message"])
