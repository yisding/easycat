"""DX1-1 characterization — real degrade-vs-raise construction matches the plan.

Unlike ``test_resolution_parity.py`` (which fakes every leaf provider
constructor), this file runs the REAL factory for one role at a time and
checks that the planner's ``degrades_to_passthrough`` capability predicts
whether ``create_session``/the bare factory degrades to a passthrough
provider or raises. Every row characterizes behaviour reproducible at this
baseline with no extras installed; a row skips only when its probe module IS
importable (the row assumes it is absent — on a fully installed machine the
backend actually builds, which is a different, already-covered code path).

The two ``krisp`` rows are split in half on purpose: the CONSTRUCTION half
(``create_vad`` / ``create_noise_reducer`` raises without the SDK) is a plain
assertion that holds today and after every DX1 PR, while the PLAN half (the plan
must block) was the D2 divergence. D2 is now FIXED — the planner reports a
selected backend whose SDK is absent as a ``missing_backends`` gap — so both
halves assert, and the ``xfail(strict=True)`` markers are gone.
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


class _DuckVAD:
    """A live injected VAD: an ``extra``-less selection under ANY planner rule."""

    def configure(self, **_kwargs: object) -> None:
        pass

    async def process(self, _chunk: object):
        if False:  # pragma: no cover - shape-only async generator
            yield None


def _plan(**overrides: object) -> object:
    # Default to an INJECTED VAD instance so a noise-reducer/AEC row is not
    # incidentally blocked by the UNRELATED default vad="auto" backend missing
    # its silero-vad extra in this dev-group-only environment. An injected
    # instance is the neutral choice specifically because it takes the
    # ``_resolution._injected_decision`` branch (capabilities={"injected"},
    # extra=None) and is not a BACKEND at all, so it stays gap-free under DX1-5's
    # rules too — unlike vad="krisp", which is gap-free only because of D2, the
    # very bug DX1-5 removes. A row that characterizes VAD itself overrides
    # ``vad=``.
    kwargs: dict[str, object] = {
        "openai_api_key": "sk-test",
        "transport": WebSocketTransportConfig(),
        "vad": _DuckVAD(),
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

    # ``auto`` only earns the "degrades_to_passthrough" capability when the
    # policy is NOT "error" (``_resolution._decide_noise_reducer`` reads
    # ``fallback_policy`` off the explicit config), so an auto+error reducer is a
    # genuine blocking gap on the same ``rnnoise`` extra its passthrough peer
    # only warns about.
    plan = _plan(
        enable_noise_reduction=True,
        noise_reduction=NoiseReducerConfig(backend="auto", fallback_policy="error"),
    )
    assert plan.has_blocking_errors, "auto+error never degrades, so the plan must block"
    assert "rnnoise" in plan.missing_extras
    assert "degrades_to_passthrough" not in plan.selected["noise_reducer"].capabilities


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


@pytest.mark.parametrize("fallback_policy", ["passthrough", "error"])
def test_noise_reducer_krisp_raises_without_the_sdk(fallback_policy: str) -> None:
    """The construction half of D2, asserted unconditionally.

    ``create_noise_reducer`` raises for a krisp backend without the SDK today
    AND after DX1-5, which changes only the planner — so this half must stay a
    plain passing assertion, never folded into the plan-half row below.
    """
    _skip_if_installed("krisp_audio")
    with pytest.raises(RuntimeError):
        create_noise_reducer(NoiseReducerConfig(backend="krisp", fallback_policy=fallback_policy))


@pytest.mark.parametrize("fallback_policy", ["passthrough", "error"])
def test_noise_reducer_krisp_plan_blocks(fallback_policy: str) -> None:
    """The plan half of D2: a selection the session cannot build must block.

    ``NOISE_REDUCER_BACKENDS["krisp"]`` declares ``extra=None`` because Krisp
    ships no PyPI package, so no missing-extra check can ever fire for it. It
    declares ``probe_module="krisp_audio"`` instead and an absent SDK is reported
    as an unbuildable BACKEND. (This row carried an ``xfail(strict=True)`` until
    the fix landed.) The policy does not matter: an explicit ``krisp`` backend
    never degrades to passthrough — the construction half above proves it raises
    under both.
    """
    _skip_if_installed("krisp_audio")
    plan = _plan(
        enable_noise_reduction=True,
        noise_reduction=NoiseReducerConfig(backend="krisp", fallback_policy=fallback_policy),
    )
    assert plan.has_blocking_errors, plan.selected["noise_reducer"]
    assert plan.missing_backends == ("noise_reducer:krisp",)
    assert "missing_backend:noise_reducer:krisp" in plan.blocking_errors()


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


def test_vad_krisp_raises_without_the_sdk() -> None:
    """The construction half of D2, asserted unconditionally (see above)."""
    _skip_if_installed("krisp_audio")
    with pytest.raises(RuntimeError):
        create_vad(VADConfig(backend="krisp"))


def test_vad_krisp_plan_blocks() -> None:
    """The plan half of D2: a selection the session cannot build must block.

    ``VAD_BACKENDS["krisp"]`` declares ``extra=None`` (no PyPI package) and
    ``probe_module="krisp_audio"``, so the absent SDK is reported as an
    unbuildable BACKEND rather than a missing extra. (This row carried an
    ``xfail(strict=True)`` until the fix landed.)
    """
    _skip_if_installed("krisp_audio")
    plan = _plan(vad=VADConfig(backend="krisp"))
    assert plan.has_blocking_errors, plan.selected["vad"]
    assert plan.missing_backends == ("vad:krisp",)
    assert "missing_backend:vad:krisp" in plan.blocking_errors()


def test_vad_auto_raises_and_blocks_on_silero_vad_when_the_whole_union_is_absent() -> None:
    _skip_if_installed("onnxruntime")
    _skip_if_installed("ten_vad")
    _skip_if_installed("krisp_audio")
    with pytest.raises(RuntimeError):
        create_vad(VADConfig(backend="auto"))

    plan = _plan(vad=VADConfig(backend="auto"))
    assert plan.has_blocking_errors
    assert "silero-vad" in plan.missing_extras
