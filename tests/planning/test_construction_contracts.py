"""DX1-1 characterization — real degrade-vs-raise construction matches the plan.

Unlike ``test_resolution_parity.py`` (which fakes every leaf provider
constructor), this file runs the REAL factory for one role at a time and
checks that the planner's ``degrades_to_passthrough`` capability predicts
whether ``create_session``/the bare factory degrades to a passthrough
provider or raises. Every row characterizes behaviour reproducible at this
baseline with no extras installed; a row skips only when its probe module IS
importable (the row assumes it is absent — on a fully installed machine the
backend actually builds, which is a different, already-covered code path).
"""

from __future__ import annotations

import importlib.util

import pytest

from easycat.config import EasyConfig
from easycat.echo_cancellation import EchoCancellationConfig, create_echo_canceller
from easycat.noise_reduction import NoiseReducerConfig, create_noise_reducer
from easycat.planning import build_provider_plan
from easycat.transports.websocket import WebSocketTransportConfig
from easycat.vad import VADConfig
from easycat.vad.factory import create_vad


class _Agent:
    async def run(self, text: str) -> str:
        return "ok"


def _skip_if_installed(module: str) -> None:
    try:
        available = importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        available = False
    if available:
        pytest.skip(f"{module} is installed; this row characterizes its absence")


def _plan(**overrides: object) -> object:
    # Default vad="krisp" (no pip extra either way) so a noise-reducer/AEC row
    # is not incidentally blocked by the UNRELATED default vad="auto" backend
    # missing its silero-vad extra in this dev-group-only environment; a row
    # that characterizes VAD itself overrides ``vad=`` explicitly.
    kwargs: dict[str, object] = {
        "openai_api_key": "sk-test",
        "transport": WebSocketTransportConfig(),
        "vad": VADConfig(backend="krisp"),
        "agent": _Agent(),
        "debug": "off",
    }
    kwargs.update(overrides)
    return build_provider_plan(EasyConfig(**kwargs), environ={"OPENAI_API_KEY": "sk-test"})


# ── Noise reducer ─────────────────────────────────────────────────────


def test_noise_reducer_auto_passthrough_degrades_when_pyrnnoise_missing() -> None:
    _skip_if_installed("pyrnnoise")
    reducer = create_noise_reducer(
        NoiseReducerConfig(backend="auto", fallback_policy="passthrough")
    )
    assert type(reducer).__name__ == "PassthroughNoiseReducer"

    plan = _plan(enable_noise_reduction=True)
    assert not plan.has_blocking_errors, plan.blocking_errors()
    assert any("degraded" in warning for warning in plan.warnings)


def test_noise_reducer_auto_error_raises_when_pyrnnoise_missing() -> None:
    _skip_if_installed("pyrnnoise")
    with pytest.raises(RuntimeError):
        create_noise_reducer(NoiseReducerConfig(backend="auto", fallback_policy="error"))

    # The planner has no ``fallback_policy`` knob to read from a bare
    # ``enable_noise_reduction=True`` (it only distinguishes "auto" from an
    # explicit backend); an explicit config carries the policy, but only the
    # BACKEND choice ("auto" vs not) determines the "degrades_to_passthrough"
    # capability (see planning/provider_plan.py:360). So the plan side of this
    # row is characterized via the explicit-config rows below instead.


def test_noise_reducer_explicit_rnnoise_raises_regardless_of_fallback_policy() -> None:
    _skip_if_installed("pyrnnoise")
    for policy in ("passthrough", "error"):
        with pytest.raises(RuntimeError):
            create_noise_reducer(NoiseReducerConfig(backend="rnnoise", fallback_policy=policy))
        plan = _plan(
            enable_noise_reduction=True,
            noise_reduction=NoiseReducerConfig(backend="rnnoise", fallback_policy=policy),
        )
        assert plan.has_blocking_errors, "an explicit backend never degrades"
        assert "rnnoise" in plan.missing_extras


def test_noise_reducer_krisp_raises_but_has_no_extra_to_block_on() -> None:
    """D2: the plan reports ready (no pip extra) even though this raises."""
    _skip_if_installed("krisp_audio")
    for policy in ("passthrough", "error"):
        with pytest.raises(RuntimeError):
            create_noise_reducer(NoiseReducerConfig(backend="krisp", fallback_policy=policy))
        plan = _plan(
            enable_noise_reduction=True,
            noise_reduction=NoiseReducerConfig(backend="krisp", fallback_policy=policy),
        )
        assert not plan.has_blocking_errors, plan.blocking_errors()


# ── Echo canceller ────────────────────────────────────────────────────


def test_echo_canceller_enabled_passthrough_degrades_when_livekit_missing() -> None:
    _skip_if_installed("livekit")
    canceller = create_echo_canceller(
        EchoCancellationConfig(enabled=True, fallback_policy="passthrough")
    )
    assert type(canceller).__name__ == "PassthroughAEC"

    plan = _plan(echo_cancellation=EchoCancellationConfig(enabled=True))
    assert not plan.has_blocking_errors, plan.blocking_errors()
    assert any("degraded" in warning for warning in plan.warnings)


def test_echo_canceller_enabled_error_raises_when_livekit_missing() -> None:
    _skip_if_installed("livekit")
    with pytest.raises(RuntimeError):
        create_echo_canceller(EchoCancellationConfig(enabled=True, fallback_policy="error"))

    plan = _plan(echo_cancellation=EchoCancellationConfig(enabled=True, fallback_policy="error"))
    assert plan.has_blocking_errors
    assert "aec" in plan.missing_extras


@pytest.mark.parametrize("fallback_policy", ["passthrough", "error"])
def test_echo_canceller_disabled_is_always_passthrough_never_blocking(
    fallback_policy: str,
) -> None:
    canceller = create_echo_canceller(
        EchoCancellationConfig(enabled=False, fallback_policy=fallback_policy)
    )
    assert type(canceller).__name__ == "PassthroughAEC"

    plan = _plan(echo_cancellation=EchoCancellationConfig(enabled=False))
    assert not plan.has_blocking_errors, plan.blocking_errors()


# ── VAD ───────────────────────────────────────────────────────────────


def test_vad_silero_raises_and_blocks_on_its_own_extra() -> None:
    _skip_if_installed("onnxruntime")
    with pytest.raises((RuntimeError, ImportError)):
        create_vad(VADConfig(backend="silero"))

    plan = _plan(vad=VADConfig(backend="silero"))
    assert plan.has_blocking_errors
    assert "silero-vad" in plan.missing_extras


def test_vad_funasr_raises_and_blocks_on_its_own_extra() -> None:
    _skip_if_installed("onnxruntime")
    with pytest.raises((RuntimeError, ImportError)):
        create_vad(VADConfig(backend="funasr"))

    plan = _plan(vad=VADConfig(backend="funasr"))
    assert plan.has_blocking_errors
    assert "funasr-vad" in plan.missing_extras


def test_vad_ten_raises_and_blocks_on_its_own_extra() -> None:
    _skip_if_installed("ten_vad")
    with pytest.raises((RuntimeError, ImportError)):
        create_vad(VADConfig(backend="ten"))

    plan = _plan(vad=VADConfig(backend="ten"))
    assert plan.has_blocking_errors
    assert "ten-vad" in plan.missing_extras


def test_vad_krisp_raises_but_has_no_extra_to_block_on() -> None:
    """D2: the plan reports ready (no pip extra) even though this raises."""
    _skip_if_installed("krisp_audio")
    with pytest.raises(RuntimeError):
        create_vad(VADConfig(backend="krisp"))

    plan = _plan(vad=VADConfig(backend="krisp"))
    assert not plan.has_blocking_errors, plan.blocking_errors()


def test_vad_auto_raises_and_blocks_on_silero_vad_when_the_whole_union_is_absent() -> None:
    _skip_if_installed("onnxruntime")
    _skip_if_installed("ten_vad")
    _skip_if_installed("krisp_audio")
    with pytest.raises(RuntimeError):
        create_vad(VADConfig(backend="auto"))

    plan = _plan(vad=VADConfig(backend="auto"))
    assert plan.has_blocking_errors
    assert "silero-vad" in plan.missing_extras
