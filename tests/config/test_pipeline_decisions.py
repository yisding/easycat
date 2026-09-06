"""Unit coverage for the shared pure pipeline decisions.

``easycat._pipeline_decisions`` is the stdlib-only leaf both session
construction (``easycat.config._factory``) and static planning
(``easycat.planning._resolution``) call, so each rule has exactly one
implementation. These tests are that implementation's contract — in particular
the method tuples, which used to be transcribed by hand in two places.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass
from typing import Any

import pytest

from easycat import _pipeline_decisions as decisions
from easycat.echo_cancellation import EchoCancellationConfig, PassthroughAEC
from easycat.noise_reduction import NoiseReducerConfig
from easycat.turn_manager import TurnMode

_ROLE_PREDICATES: dict[str, tuple[Any, tuple[str, ...]]] = {
    "stt": (decisions.is_stt_provider_instance, decisions.STT_INSTANCE_METHODS),
    "tts": (decisions.is_tts_provider_instance, decisions.TTS_INSTANCE_METHODS),
    "vad": (decisions.is_vad_provider_instance, decisions.VAD_INSTANCE_METHODS),
    "noise_reducer": (
        decisions.is_noise_reducer_instance,
        decisions.NOISE_REDUCER_INSTANCE_METHODS,
    ),
    "echo_canceller": (
        decisions.is_echo_canceller_instance,
        decisions.ECHO_CANCELLER_INSTANCE_METHODS,
    ),
}

_ROLES = tuple(_ROLE_PREDICATES)


def _shaped_class(methods: tuple[str, ...]) -> type:
    """A class exposing exactly ``methods`` as callables."""
    return type("Shaped", (), {name: (lambda self, *a, **k: None) for name in methods})


@pytest.mark.parametrize("role", _ROLES)
def test_instance_predicates_accept_a_full_shape(role: str) -> None:
    predicate, methods = _ROLE_PREDICATES[role]
    assert predicate(_shaped_class(methods)()) is True


@pytest.mark.parametrize(
    ("role", "dropped"),
    [(role, method) for role, (_p, methods) in _ROLE_PREDICATES.items() for method in methods],
)
def test_instance_predicates_reject_a_partial_shape(role: str, dropped: str) -> None:
    predicate, methods = _ROLE_PREDICATES[role]
    partial = tuple(name for name in methods if name != dropped)
    assert predicate(_shaped_class(partial)()) is False


@pytest.mark.parametrize("role", _ROLES)
def test_instance_predicates_reject_a_class_object(role: str) -> None:
    # ``config/_factory.py``'s predicates guard with ``not isinstance(v, type)``
    # so a provider CLASS is never routed into ``inject_event_bus``.
    predicate, methods = _ROLE_PREDICATES[role]
    assert predicate(_shaped_class(methods)) is False


@pytest.mark.parametrize("role", _ROLES)
def test_provider_shape_accepts_a_class_object(role: str) -> None:
    # The looser predicate the leaf factories apply (``create_vad`` and friends
    # hand a shape-matching CLASS straight back), which the planner mirrors so
    # its verdict predicts theirs.
    _predicate, methods = _ROLE_PREDICATES[role]
    assert decisions.has_provider_shape(_shaped_class(methods), methods) is True
    assert decisions.has_provider_shape(_shaped_class(methods)(), methods) is True
    assert decisions.has_provider_shape(None, methods) is False


@pytest.mark.parametrize("role", _ROLES)
def test_instance_predicates_reject_a_config_and_a_string(role: str) -> None:
    predicate, _methods = _ROLE_PREDICATES[role]
    assert predicate(None) is False
    assert predicate("silero") is False
    assert predicate(NoiseReducerConfig()) is False


def test_noise_reduction_enabled_table() -> None:
    cfg = NoiseReducerConfig()
    assert (
        decisions.noise_reduction_enabled(enable_noise_reduction=False, noise_reduction=None)
        is False
    )
    assert (
        decisions.noise_reduction_enabled(enable_noise_reduction=True, noise_reduction=None)
        is True
    )
    assert (
        decisions.noise_reduction_enabled(enable_noise_reduction=False, noise_reduction=cfg)
        is True
    )
    assert (
        decisions.noise_reduction_enabled(enable_noise_reduction=True, noise_reduction=cfg) is True
    )


@dataclass
class _ThirdPartyAECConfig:
    """A registered third-party AEC config shape (no EchoCancellationConfig base)."""

    enabled: bool = False
    fallback_policy: str = "passthrough"


def test_echo_cancellation_enabled_table() -> None:
    enabled = decisions.echo_cancellation_enabled
    cls = EchoCancellationConfig
    assert enabled(EchoCancellationConfig(enabled=True), config_cls=cls) is True
    assert enabled(EchoCancellationConfig(enabled=False), config_cls=cls) is False
    assert enabled(None, config_cls=cls) is False
    assert enabled(PassthroughAEC(), config_cls=cls) is False
    # The D4 row that pins the ``isinstance`` rule: a third-party AEC config with
    # ``enabled=True`` reports False, because ``SessionConfig`` has always read
    # ``isinstance(spec, EchoCancellationConfig) and spec.enabled``. The
    # planner's ``getattr(cfg, "enabled", False)`` spelling would return True
    # here — that is a different question and must not be merged with this one.
    assert enabled(_ThirdPartyAECConfig(enabled=True), config_cls=cls) is False


def test_is_push_to_talk_table() -> None:
    assert decisions.is_push_to_talk(TurnMode.PUSH_TO_TALK) is True
    assert decisions.is_push_to_talk(TurnMode.VAD) is False
    assert decisions.is_push_to_talk("push_to_talk") is True
    assert decisions.is_push_to_talk(None) is False


@pytest.mark.parametrize("push_to_talk", [False, True])
@pytest.mark.parametrize("smart_turn_enabled", [False, True])
@pytest.mark.parametrize("voicemail_detector_enabled", [False, True])
@pytest.mark.parametrize("stt_native_endpointing", [False, True])
def test_auto_turn_from_stt_final_table(
    push_to_talk: bool,
    smart_turn_enabled: bool,
    voicemail_detector_enabled: bool,
    stt_native_endpointing: bool,
) -> None:
    overridden = push_to_talk or smart_turn_enabled or voicemail_detector_enabled
    expected = False if overridden else stt_native_endpointing
    result = decisions.auto_turn_from_stt_final(
        push_to_talk=push_to_talk,
        smart_turn_enabled=smart_turn_enabled,
        voicemail_detector_enabled=voicemail_detector_enabled,
        stt_native_endpointing=stt_native_endpointing,
    )
    assert result is expected
    # The VAD stage runs exactly when the STT is NOT driving turn boundaries.
    assert decisions.vad_stage_enabled(auto_turn_from_stt_final=result) is not expected


def test_module_imports_nothing_from_easycat() -> None:
    """The stdlib-only contract, checked cheaply on the source itself.

    An ``easycat`` import here would let the shared leaf drag either side's
    dependencies into the other and break
    ``tests/planning/test_boundary.py``'s subprocess checks.
    """
    tree = ast.parse(inspect.getsource(decisions))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)
    assert not [name for name in imported if name.split(".")[0] == "easycat"], imported
