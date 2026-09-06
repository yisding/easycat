"""Unit coverage for the planner core (M6b).

Asserts: STT/TTS selection reads catalog metadata; the 5 net-new roles resolve
from the declarative tables; missing_env/missing_extras populate WITHOUT
instantiating providers (find_spec, not require_module); capabilities are the
declared frozensets; and incompatible-combo warnings surface.
"""

from __future__ import annotations

import importlib.util

import pytest

from easycat.config import EasyConfig
from easycat.planning import ProviderPlan, ProviderSelection, build_provider_plan
from easycat.project.schema import VoiceProfile
from easycat.vad import VADConfig


class _Agent:
    async def run(self, text: str) -> str:
        return "ok"


def _profile(**overrides: object) -> VoiceProfile:
    kwargs: dict[str, object] = {"name": "default", "transport": "local"}
    kwargs.update(overrides)
    return VoiceProfile(**kwargs)  # type: ignore[arg-type]


def test_stt_tts_selection_reads_catalog_metadata() -> None:
    plan = build_provider_plan(
        _profile(stt="deepgram", tts="elevenlabs"),
        environ={"DEEPGRAM_API_KEY": "x", "ELEVENLABS_API_KEY": "y"},
    )
    stt = plan.selected["stt"]
    assert stt.provider == "deepgram"
    assert stt.config_type == "DeepgramSTTConfig"
    assert stt.extra == "deepgram"
    assert stt.required_env == "DEEPGRAM_API_KEY"

    tts = plan.selected["tts"]
    assert tts.provider == "elevenlabs"
    assert tts.config_type == "ElevenLabsTTSConfig"
    assert tts.required_env == "ELEVENLABS_API_KEY"


def test_stt_tts_default_to_openai_when_unset() -> None:
    plan = build_provider_plan(_profile(), environ={"OPENAI_API_KEY": "x"})
    assert plan.selected["stt"].provider == "openai-realtime"
    assert plan.selected["tts"].provider == "openai"
    assert plan.selected["stt"].required_env == "OPENAI_API_KEY"


def test_model_token_is_parsed_from_shortcut() -> None:
    plan = build_provider_plan(
        _profile(stt="deepgram/nova-2"),
        environ={"DEEPGRAM_API_KEY": "x", "OPENAI_API_KEY": "y"},
    )
    assert plan.selected["stt"].model == "nova-2"


def test_five_net_new_roles_resolve_from_tables() -> None:
    plan = build_provider_plan(
        _profile(transport="webrtc", vad="silero", agent="python:app:make"),
        environ={"OPENAI_API_KEY": "x"},
    )
    assert plan.selected["transport"].provider == "webrtc"
    assert plan.selected["transport"].extra == "webrtc"
    assert plan.selected["vad"].provider == "silero"
    assert plan.selected["vad"].extra == "silero-vad"
    assert plan.selected["agent"].provider == "python"
    assert plan.selected["agent"].extra is None
    assert plan.selected["noise_reducer"].provider == "off"
    # webrtc/browser preset auto-enables echo cancellation.
    assert plan.selected["echo_canceller"].provider == "livekit"


def test_capabilities_are_declared_frozensets() -> None:
    plan = build_provider_plan(_profile(transport="webrtc"), environ={"OPENAI_API_KEY": "x"})
    transport = plan.selected["transport"]
    assert isinstance(transport.capabilities, frozenset)
    assert "browser" in transport.capabilities


@pytest.mark.parametrize(
    ("shortcut", "expected"),
    [
        ("deepgram/flux-general-en", True),
        ("deepgram/nova-2", False),
        ("cartesia/ink-2", True),
        ("cartesia/ink-whisper", False),
        ("elevenlabs", True),
    ],
)
def test_stt_catalog_capabilities_follow_selected_model(
    shortcut: str,
    expected: bool,
) -> None:
    provider = shortcut.partition("/")[0]
    env_var = {
        "deepgram": "DEEPGRAM_API_KEY",
        "cartesia": "CARTESIA_API_KEY",
        "elevenlabs": "ELEVENLABS_API_KEY",
    }[provider]
    plan = build_provider_plan(
        _profile(stt=shortcut),
        environ={env_var: "x", "OPENAI_API_KEY": "y"},
    )

    assert ("native_endpointing" in plan.selected["stt"].capabilities) is expected


def test_missing_env_detected_without_instantiating_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No keys at all -> openai stt+tts report OPENAI_API_KEY missing.
    plan = build_provider_plan(_profile(), environ={})
    assert "OPENAI_API_KEY" in plan.missing_env
    assert plan.has_blocking_errors


def test_missing_extra_uses_find_spec_not_require_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_find_spec = importlib.util.find_spec
    seen: list[str] = []

    def fake_find_spec(name: str, package: object = None):
        seen.append(name)
        if name == "aiortc":
            return None
        return real_find_spec(name, package)  # type: ignore[arg-type]

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    plan = build_provider_plan(_profile(transport="webrtc"), environ={"OPENAI_API_KEY": "x"})
    assert "webrtc" in plan.missing_extras
    assert plan.has_blocking_errors
    # find_spec was used to probe the extra (not require_module).
    assert "aiortc" in seen


def test_empty_dependency_extras_are_never_missing() -> None:
    # deepgram/elevenlabs/cartesia are marker extras with no importable package;
    # they must never be reported as a missing extra.
    plan = build_provider_plan(
        _profile(stt="deepgram", tts="cartesia"),
        environ={"DEEPGRAM_API_KEY": "x", "CARTESIA_API_KEY": "y", "OPENAI_API_KEY": "z"},
    )
    assert "deepgram" not in plan.missing_extras
    assert "cartesia" not in plan.missing_extras


def test_openai_audio_providers_do_not_require_optional_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Built-in OpenAI STT/TTS use core HTTP/WebSocket dependencies only."""
    real_find_spec = importlib.util.find_spec
    seen: list[str] = []

    def fake_find_spec(name: str, package: object = None):
        seen.append(name)
        if name == "openai":
            return None
        return real_find_spec(name, package)  # type: ignore[arg-type]

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    plan = build_provider_plan(
        _profile(transport="websocket", stt="openai-realtime", tts="openai"),
        environ={"OPENAI_API_KEY": "x"},
    )

    assert plan.selected["stt"].extra == "openai"
    assert plan.selected["tts"].extra == "openai"
    assert "openai" not in plan.missing_extras
    assert "openai" not in seen


def test_unknown_vad_shortcut_raises_not_silent_auto_fallback() -> None:
    # Regression: an unknown vad shortcut must NOT silently fall back to the
    # ``auto`` metadata while keeping the bad name. ``create_vad`` /
    # ``to_easyconfig`` reject it, so the planner must too — otherwise the plan
    # would look clean for a profile that crashes on the first connection.
    with pytest.raises(ValueError, match="Unknown VAD backend 'not-a-backend'"):
        build_provider_plan(
            _profile(transport="websocket", vad="not-a-backend"),
            environ={"OPENAI_API_KEY": "x"},
        )


def test_unknown_vad_backend_still_raises_when_the_stage_is_skipped() -> None:
    # The VAD role is reported ``off`` for a native-endpointing STT, but the
    # backend is RESOLVED before it is disabled: an unresolvable profile must
    # stay unresolvable regardless of who owns endpointing, or ``easycat plan``
    # would print a clean plan for a manifest ``to_easyconfig`` rejects.
    environ = {"OPENAI_API_KEY": "x", "DEEPGRAM_API_KEY": "y"}
    # Pin the premise: without it this row would keep passing (``_decide_vad``
    # raises on every path) even if ``deepgram/flux-general-en`` stopped
    # declaring ``native_endpointing``, and would silently stop guarding the
    # resolve-then-disable ordering it exists for.
    skipped = build_provider_plan(
        _profile(transport="websocket", stt="deepgram/flux-general-en", vad="silero"),
        environ=environ,
    )
    assert skipped.selected["vad"].provider == "off"

    with pytest.raises(ValueError, match="Unknown VAD backend 'not-a-backend'"):
        build_provider_plan(
            _profile(transport="websocket", stt="deepgram/flux-general-en", vad="not-a-backend"),
            environ=environ,
        )


def test_twilio_combo_emits_warning_not_blocking() -> None:
    plan = build_provider_plan(
        _profile(transport="twilio", stt="openai", tts="openai"),
        environ={"OPENAI_API_KEY": "x"},
    )
    assert plan.warnings  # at least the auto-align note
    # The warning is NOT a blocking error.
    assert "transport_twilio_audio_format_auto_aligned" in plan.warnings


def test_plan_from_easyconfig_input() -> None:
    config = EasyConfig(
        stt="openai",
        tts="openai",
        vad=VADConfig(backend="silero"),
        openai_api_key="sk-x",
        agent=_Agent(),
        debug="off",
    )
    plan = build_provider_plan(config, environ={"OPENAI_API_KEY": "sk-x"})
    assert plan.selected["stt"].config_type == "OpenAISTTConfig"
    assert plan.selected["vad"].provider == "silero"
    assert plan.selected["agent"].provider == "python"


def test_provider_plan_dataclasses_are_frozen() -> None:
    sel = ProviderSelection(
        role="stt",
        provider="openai",
        model=None,
        config_type="OpenAISTTConfig",
        extra="openai",
        required_env="OPENAI_API_KEY",
        capabilities=frozenset(),
    )
    with pytest.raises(Exception):
        sel.provider = "deepgram"  # type: ignore[misc]

    plan = ProviderPlan(
        profile="default",
        selected={"stt": sel},
        missing_env=(),
        missing_extras=(),
        warnings=(),
    )
    assert not plan.has_blocking_errors
    assert plan.blocking_errors() == ()


class _VADLike:
    def configure(self, **_kwargs: object) -> None: ...

    async def process(self, _chunk: object) -> object: ...


def test_class_object_is_described_the_way_create_vad_treats_it() -> None:
    """A provider CLASS keeps its current, factory-matching verdict.

    ``create_vad`` (like ``create_noise_reducer`` / ``create_echo_canceller``)
    duck-checks the method names WITHOUT a class-object guard and hands a
    matching class straight back, so the planner must describe it the same way.
    ``config/_factory.py``'s stricter ``_is_vad_provider_instance`` disagrees —
    that divergence is pre-existing and belongs to a behaviour-change PR, not to
    the structural collapse.
    """
    config = EasyConfig(
        stt="openai",
        tts="openai",
        vad=_VADLike,  # the CLASS, not an instance
        openai_api_key="sk-x",
        agent=_Agent(),
        debug="off",
    )
    selection = build_provider_plan(config, environ={"OPENAI_API_KEY": "sk-x"}).selected["vad"]
    # ``type(_VADLike).__name__`` is ``"type"`` — an unflattering but faithful
    # record of what the injected branch reports for a class today.
    assert selection.provider == "type"
    assert selection.capabilities == frozenset({"injected"})


def test_instance_is_treated_as_an_injected_provider() -> None:
    config = EasyConfig(
        stt="openai",
        tts="openai",
        vad=_VADLike(),
        openai_api_key="sk-x",
        agent=_Agent(),
        debug="off",
    )
    selection = build_provider_plan(config, environ={"OPENAI_API_KEY": "sk-x"}).selected["vad"]
    assert selection.provider == "_VADLike"
    assert selection.capabilities == frozenset({"injected"})
    assert selection.extra is None


# ── The one selection projection ─────────────────────────────────────

#: The keys every JSON surface publishes per role. Spelled out here, once, so a
#: field added to ``selection_to_dict`` has to be added to this list too.
_SELECTION_PAYLOAD_KEYS = {
    "role",
    "provider",
    "model",
    "config_type",
    "extra",
    "required_env",
    "capabilities",
}


def test_selection_to_dict_is_the_projection_both_json_surfaces_use() -> None:
    """``easycat plan --json`` and the server's ``/plan`` payload cannot drift.

    Both surfaces reach this projection through
    :func:`easycat.planning.plan_to_dict`, and ``cli.plan._selection_to_dict`` is
    a thin alias kept for in-tree and out-of-tree callers. The server assertions
    in ``tests/server/test_plan_endpoint.py`` need aiohttp, so this
    credential-free row is what actually executes the shared projection and pins
    its key set on a dev-group-only machine.
    """
    from easycat.cli.plan import _selection_to_dict
    from easycat.planning import selection_to_dict

    config = EasyConfig(
        stt="openai",
        tts="openai",
        vad=VADConfig(backend="silero"),
        openai_api_key="sk-x",
        agent=_Agent(),
        debug="off",
    )
    plan = build_provider_plan(config, environ={"OPENAI_API_KEY": "sk-x"})
    for selection in plan.selected.values():
        payload = selection_to_dict(selection)
        assert set(payload) == _SELECTION_PAYLOAD_KEYS
        assert _selection_to_dict(selection) == payload
        # JSON-ready: ``capabilities`` is a sorted list, never a frozenset.
        assert payload["capabilities"] == sorted(selection.capabilities)


_PLAN_PAYLOAD_KEYS = {
    "profile",
    "selected",
    "missing_env",
    "missing_extras",
    "missing_backends",
    "warnings",
    "blocking_errors",
    "has_blocking_errors",
}


def test_plan_to_dict_is_the_plan_projection_both_json_surfaces_use() -> None:
    """The plan-LEVEL peer of the selection projection above.

    ``easycat plan --json`` spreads this dict into its envelope and
    ``VoiceServer.plan_payload`` spreads it under ``manifest_loaded``, so a
    plan-level field added to one surface lands on both. The key set is pinned
    here because the server rows that would otherwise catch a drift need aiohttp.
    """
    from easycat.planning import plan_to_dict, selection_to_dict

    config = EasyConfig(
        stt="openai",
        tts="openai",
        vad=VADConfig(backend="silero"),
        openai_api_key="sk-x",
        agent=_Agent(),
        debug="off",
    )
    plan = build_provider_plan(config, environ={"OPENAI_API_KEY": "sk-x"})
    payload = plan_to_dict(plan)

    assert set(payload) == _PLAN_PAYLOAD_KEYS
    assert payload["profile"] == plan.profile
    # Every gap tuple is a JSON-ready list, never a tuple.
    for key in ("missing_env", "missing_extras", "missing_backends", "warnings"):
        assert isinstance(payload[key], list), key
    assert payload["blocking_errors"] == list(plan.blocking_errors())
    assert payload["has_blocking_errors"] is plan.has_blocking_errors
    # Roles go through the SAME per-selection projection, not a second copy.
    assert payload["selected"] == {
        role: selection_to_dict(selection) for role, selection in plan.selected.items()
    }


def test_plan_to_dict_key_set_matches_the_server_empty_plan_branches() -> None:
    """``/plan``'s top-level key set must not depend on which branch answered.

    ``VoiceServer.plan_payload`` has two branches with no resolved plan (a
    factory-only server, an unresolvable profile) that build their payload by
    hand from ``_empty_plan_gaps()``. A client reading ``missing_backends`` on the
    unresolvable-profile path — the diagnostic case — must not get a ``KeyError``.
    """
    from easycat.server.voice_server import _empty_plan_gaps

    hand_built = {"profile", "selected", *_empty_plan_gaps(), "blocking_errors"}
    hand_built.add("has_blocking_errors")
    assert hand_built == _PLAN_PAYLOAD_KEYS
