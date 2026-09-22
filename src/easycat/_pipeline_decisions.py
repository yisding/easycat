"""Pure pipeline decisions shared by session construction and static planning.

Stdlib-only by contract. ``easycat.planning`` may not import ``easycat.config``
(``tests/planning/test_boundary.py:33`` asserts ``import easycat.planning``
never loads ``easycat.config._factory``) and ``easycat.config`` must not import
``easycat.planning`` (layering: the planner is the diagnostic projection *over*
configuration), so the decisions both sides make live here, below both. This
module contains no ``import easycat...`` statement at all — no provider
construction, no SDK import, no env read, no network, no allocation.

Consumers:

* :mod:`easycat.config._factory` — construction. It calls the instance
  predicates, :func:`noise_reduction_enabled`, :func:`echo_cancellation_enabled`,
  :func:`auto_turn_from_stt_final` and :func:`vad_stage_enabled`.
* :mod:`easycat.planning._resolution` — preview. It calls the same instance
  predicates, :func:`noise_reduction_enabled`, :func:`is_push_to_talk`,
  :func:`auto_turn_from_stt_final` and :func:`vad_stage_enabled`.
* The leaf factories ``easycat.vad.factory.create_vad``,
  ``easycat.noise_reduction.create_noise_reducer`` and
  ``easycat.echo_cancellation.create_echo_canceller`` — they call
  :func:`has_provider_shape` with the matching method tuple, so the "is this
  already a provider?" duck check the planner mirrors is the same object, not a
  hand-kept copy.
* :func:`easycat.helpers._wired_summary` — the startup banner. It calls
  :func:`noise_reduction_enabled` so its "noise-reduction=on/off" line cannot
  disagree with what ``create_session`` wired.

:func:`echo_cancellation_enabled` is the deliberate exception: it is called from
``config/_factory.py`` only, because the planner's superficially similar
expression answers a *different* question (see that function's docstring).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

# The duck-typed method tuples that discriminate a live provider *instance*
# from a provider *config*. Transcribed from the five near-identical predicates
# ``config/_factory.py`` used to carry, and re-typed by hand in the planner and
# in the three leaf factories — all of which now read these tuples instead.
# ``tests/config/test_pipeline_decisions.py`` pins them against an independent
# transcription and against the ``easycat.providers`` protocols.
STT_INSTANCE_METHODS: tuple[str, ...] = (
    "start_stream",
    "send_audio",
    "commit_segment",
    "end_stream",
    "events",
)
TTS_INSTANCE_METHODS: tuple[str, ...] = ("synthesize", "stop", "cancel")
VAD_INSTANCE_METHODS: tuple[str, ...] = ("process", "configure")
NOISE_REDUCER_INSTANCE_METHODS: tuple[str, ...] = ("process",)
ECHO_CANCELLER_INSTANCE_METHODS: tuple[str, ...] = ("process", "feed_reference")

#: ``TurnMode.PUSH_TO_TALK.value``. Compared by value so no caller has to import
#: :mod:`easycat.turn_manager` (which pulls ~52 modules including the journal
#: stack) just to answer "is this push-to-talk?".
PUSH_TO_TALK_MODE_VALUE = "push_to_talk"


def has_provider_shape(value: Any, methods: Sequence[str]) -> bool:
    """Whether ``value`` exposes every method in ``methods`` as a callable.

    The bare duck check, class objects included. It is what the leaf factories
    (``create_vad`` / ``create_noise_reducer`` / ``create_echo_canceller``) call
    to decide "is this already a provider?" — literally this function — so the
    planner, whose job is to predict them, applies the same one.
    :func:`is_provider_instance` is the stricter variant ``config/_factory.py``
    uses.
    """
    return all(callable(getattr(value, name, None)) for name in methods)


def is_provider_instance(value: Any, methods: Sequence[str]) -> bool:
    """Whether ``value`` is a live OBJECT exposing every method in ``methods``.

    :func:`has_provider_shape` plus a class-object guard: ``config/_factory.py``
    has always refused to treat a provider *class* as a live provider, and
    dropping the guard would route a class into ``inject_event_bus``.
    """
    return not isinstance(value, type) and has_provider_shape(value, methods)


def is_stt_provider_instance(value: Any) -> bool:
    """Whether ``value`` is a live STT provider rather than an STT config."""
    return is_provider_instance(value, STT_INSTANCE_METHODS)


def is_tts_provider_instance(value: Any) -> bool:
    """Whether ``value`` is a live TTS provider rather than a TTS config."""
    return is_provider_instance(value, TTS_INSTANCE_METHODS)


def is_vad_provider_instance(value: Any) -> bool:
    """Whether ``value`` is a live VAD provider rather than a VAD config."""
    return is_provider_instance(value, VAD_INSTANCE_METHODS)


def is_noise_reducer_instance(value: Any) -> bool:
    """Whether ``value`` is a live noise reducer rather than a reducer config."""
    return is_provider_instance(value, NOISE_REDUCER_INSTANCE_METHODS)


def is_echo_canceller_instance(value: Any) -> bool:
    """Whether ``value`` is a live echo canceller rather than an AEC config."""
    return is_provider_instance(value, ECHO_CANCELLER_INSTANCE_METHODS)


def noise_reduction_enabled(*, enable_noise_reduction: Any, noise_reduction: Any) -> bool:
    """The single expression that decides whether a reducer is built at all.

    An explicit ``noise_reduction=`` value opts in on its own, so the flag only
    has to carry the "default reducer, please" case.
    """
    return bool(enable_noise_reduction or noise_reduction is not None)


def echo_cancellation_enabled(echo_cancellation: Any, *, config_cls: type) -> bool:
    """``SessionConfig.enable_echo_cancellation`` for a resolved AEC spec.

    The rule is ``isinstance(spec, config_cls) and spec.enabled``. The
    ``isinstance`` gate is load-bearing and deliberately NOT a bare
    ``getattr(spec, "enabled", False)`` over any spec. ``config_cls`` is passed
    in (the caller supplies
    :class:`easycat.echo_cancellation.EchoCancellationConfig`) so this module
    keeps its stdlib-only contract.

    PRESERVED QUIRK: a *registered third-party* AEC config and a live injected
    ``EchoCanceller`` both report ``False`` here even though ``create_session``
    built and wired a canceller for them. Changing that is a separate
    behaviour-change PR; using ``getattr`` instead would silently flip it.

    This is NOT the same question the planner asks. ``provider_plan``'s
    ``getattr(cfg, "enabled", False)`` decides only whether the ``aec`` extra is
    a *blocking gap*, and is reached only for an ``EchoCancellationConfig`` or
    ``None`` — so its spelling is correct there and must not be merged with this
    one.
    """
    if not isinstance(echo_cancellation, config_cls):
        return False
    # ``config_cls`` is passed in, so its ``enabled`` attribute is read
    # defensively rather than assumed by a static type.
    return bool(getattr(echo_cancellation, "enabled", False))


def is_push_to_talk(turn_mode: Any) -> bool:
    """Whether ``turn_mode`` is ``TurnMode.PUSH_TO_TALK`` or its serialized value.

    Compared by ``.value`` so the planner never imports
    :mod:`easycat.turn_manager`.
    """
    return bool(getattr(turn_mode, "value", turn_mode) == PUSH_TO_TALK_MODE_VALUE)


def auto_turn_from_stt_final(
    *,
    push_to_talk: bool,
    smart_turn_enabled: bool,
    voicemail_detector_enabled: bool,
    stt_native_endpointing: bool,
) -> bool:
    """Whether turn boundaries come from STT finals, so the VAD stage is skipped.

    True for STT providers that do their own endpointing (Deepgram Flux,
    Cartesia ink-2, ElevenLabs realtime VAD) — unless an explicit endpointing
    choice overrides it: push-to-talk, smart-turn, or a telephony voicemail
    detector all keep EasyCat's own VAD + commit path.
    """
    if push_to_talk or smart_turn_enabled or voicemail_detector_enabled:
        return False
    return stt_native_endpointing


def vad_stage_enabled(*, auto_turn_from_stt_final: bool) -> bool:
    """Whether the VAD stage runs at all (the STT owns endpointing when it does not)."""
    return not auto_turn_from_stt_final
