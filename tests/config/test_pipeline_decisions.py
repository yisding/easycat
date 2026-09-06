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
import sys
from dataclasses import dataclass
from typing import Any

import pytest

from easycat import _pipeline_decisions as decisions
from easycat import providers
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


# The method tuples, transcribed a SECOND time by hand. ``_shaped_class`` builds
# its fixtures from the leaf's own constants, so without an independent spelling
# a typo (``"sendaudio"``) would grow a matching attribute on the fixture and
# every predicate test would still pass.
_EXPECTED_METHODS: dict[str, tuple[str, ...]] = {
    "stt": ("start_stream", "send_audio", "commit_segment", "end_stream", "events"),
    "tts": ("synthesize", "stop", "cancel"),
    "vad": ("process", "configure"),
    "noise_reducer": ("process",),
    "echo_canceller": ("process", "feed_reference"),
}

_ROLE_PROTOCOLS: dict[str, type] = {
    "stt": providers.STTProvider,
    "tts": providers.TTSProvider,
    "vad": providers.VADProvider,
    "noise_reducer": providers.NoiseReducer,
    "echo_canceller": providers.EchoCanceller,
}


@pytest.mark.parametrize("role", _ROLES)
def test_method_tuples_match_an_independent_transcription(role: str) -> None:
    _predicate, methods = _ROLE_PREDICATES[role]
    assert methods == _EXPECTED_METHODS[role]


@pytest.mark.parametrize("role", _ROLES)
def test_method_tuples_name_real_members_of_the_provider_protocol(role: str) -> None:
    """A typo cannot hide behind ``_shaped_class``: the names must exist upstream.

    ``easycat.providers`` is the protocol every provider implements, so a method
    the leaf's tuple names must be declared there. (The tuples are the SUBSET the
    duck check needs, so a protocol may legitimately carry more.)
    """
    _predicate, methods = _ROLE_PREDICATES[role]
    declared = {name for name in vars(_ROLE_PROTOCOLS[role]) if not name.startswith("_")}
    assert set(methods) <= declared, sorted(set(methods) - declared)


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


_LEAF_FACTORY_ROLES = ("vad", "noise_reducer", "echo_canceller")


def _leaf_factory(role: str) -> Any:
    from easycat.echo_cancellation import create_echo_canceller
    from easycat.noise_reduction import create_noise_reducer
    from easycat.vad.factory import create_vad

    return {
        "vad": create_vad,
        "noise_reducer": create_noise_reducer,
        "echo_canceller": create_echo_canceller,
    }[role]


@pytest.mark.parametrize("role", _LEAF_FACTORY_ROLES)
def test_leaf_factories_hand_back_exactly_the_shared_shape(role: str) -> None:
    """``create_vad`` / ``create_noise_reducer`` / ``create_echo_canceller`` agree.

    Each used to re-spell its own method tuple inline; they now call
    :func:`has_provider_shape` with the constant above. This is the behavioural
    half of that claim: an object matching the tuple is returned unchanged, and
    dropping any one method takes it off the injected path entirely.
    """
    _predicate, methods = _ROLE_PREDICATES[role]
    factory = _leaf_factory(role)

    injected = _shaped_class(methods)()
    assert factory(injected) is injected

    for dropped in methods:
        partial = _shaped_class(tuple(n for n in methods if n != dropped))()
        with pytest.raises(ValueError):
            factory(partial)


@pytest.mark.parametrize("role", _LEAF_FACTORY_ROLES)
def test_leaf_factories_hand_back_a_shape_matching_class(role: str) -> None:
    # The reason the planner mirrors ``has_provider_shape`` and not the stricter
    # ``is_provider_instance``: these factories have no class-object guard.
    _predicate, methods = _ROLE_PREDICATES[role]
    shaped = _shaped_class(methods)
    assert _leaf_factory(role)(shaped) is shaped


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


def _import_violations(source: str) -> list[str]:
    """Every import in ``source`` that is not a plain absolute stdlib import.

    RELATIVE imports are reported rather than resolved: ``from .turn_manager
    import TurnMode`` is the tempting line to add to the leaf (which keeps
    ``PUSH_TO_TALK_MODE_VALUE`` precisely to avoid it), and an absolute-name
    filter cannot see it — ``node.module`` is ``"turn_manager"`` for that form
    and ``None`` for ``from . import turn_manager``.
    """
    violations: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            violations.extend(
                alias.name
                for alias in node.names
                if alias.name.split(".")[0] not in sys.stdlib_module_names
            )
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                violations.append("." * node.level + (node.module or ""))
            elif node.module is not None and node.module.split(".")[0] not in (
                sys.stdlib_module_names
            ):
                violations.append(node.module)
    return violations


def test_module_imports_only_the_standard_library() -> None:
    """The stdlib-only contract, checked cheaply on the source itself.

    An ``easycat`` import here would let the shared leaf drag either side's
    dependencies into the other and break ``tests/planning/test_boundary.py``'s
    subprocess checks.
    """
    assert _import_violations(inspect.getsource(decisions)) == []


def test_the_import_guard_catches_the_lines_it_exists_to_catch() -> None:
    """The guard itself, on the three forms an absolute-name filter would miss."""
    assert _import_violations("from .turn_manager import TurnMode") == [".turn_manager"]
    assert _import_violations("from . import config") == ["."]
    assert _import_violations("from easycat.config import EasyConfig") == ["easycat.config"]
    assert _import_violations("import numpy") == ["numpy"]
    assert _import_violations("from collections.abc import Sequence\nimport ast\n") == []
