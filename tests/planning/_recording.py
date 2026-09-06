"""Shared harness for the DX1-1 resolution-parity characterization tests.

Captures what ``create_session`` actually resolves at the seven pipeline-role
factory seams, so a test can compare it against the static
:func:`easycat.planning.build_provider_plan` preview without executing any
real provider constructor.

Imports no production module beyond ``easycat.config`` / ``easycat.config._factory``
(plus ``easycat.planning`` for the ``ProviderPlan`` type hint).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

from easycat.config import EasyConfig, _factory

if TYPE_CHECKING:
    from easycat.planning import ProviderPlan
    from easycat.session._session import Session

# Sentinel distinguishing "the leaf constructor never fired" from "it fired
# with a captured value of None" (no role spec is ever legitimately None).
_UNSET = object()


class _FakeSTT:
    async def start_stream(self) -> None:
        pass

    async def send_audio(self, _chunk: object) -> None:
        pass

    async def commit_segment(self) -> None:
        pass

    async def end_stream(self) -> None:
        pass

    async def events(self):
        if False:  # pragma: no cover - shape-only async generator
            yield None


class _FakeTTS:
    async def synthesize(self, _text: str):
        if False:  # pragma: no cover - shape-only async generator
            yield None

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass


class _FakeVAD:
    def configure(self, **_kwargs: object) -> None:
        pass

    async def process(self, _chunk: object):
        if False:  # pragma: no cover - shape-only async generator
            yield None


class _FakeNoiseReducer:
    async def process(self, chunk: object) -> object:
        return chunk


class _FakeEchoCanceller:
    async def process(self, chunk: object) -> object:
        return chunk

    def feed_reference(self, _chunk: object) -> None:
        pass


class _FakeTransport:
    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive_audio(self):
        return
        yield  # pragma: no cover - shape-only async generator

    async def send_audio(self, _chunk: object) -> bool:
        return True

    async def clear_audio(self) -> None:
        pass

    def version_info(self) -> dict[str, str]:
        return {"provider": "fake"}


# role name -> (patched attribute name on easycat.config._factory, fake factory)
#
# The ``transport`` role is deliberately ABSENT here: ``_create_transport``
# (``_factory.py:147``) is a role wrapper, not a leaf — it owns the
# ``isinstance(config, TransportLike)`` injected-instance branch and the
# ``config._event_bus`` injection, unlike ``_create_stt``/``_create_vad``/… whose
# predicates sit above the leaf they call. Patching it out would delete both from
# the code under test and turn every ``built.transport_spec is config.transport``
# assertion into a tautology of this recorder. ``_patch_transport_leaves`` below
# patches the per-config-type factories *inside* it instead.
_LEAF_TARGETS: tuple[tuple[str, str, type], ...] = (
    ("stt", "create_stt_provider", _FakeSTT),
    ("stt", "create_stt_provider_from_config", _FakeSTT),
    ("tts", "create_tts_provider", _FakeTTS),
    ("tts", "create_tts_provider_from_config", _FakeTTS),
    ("vad", "create_vad", _FakeVAD),
    ("noise", "create_noise_reducer", _FakeNoiseReducer),
    ("echo", "create_echo_canceller", _FakeEchoCanceller),
)


def _patch_transport_leaves(monkeypatch: pytest.MonkeyPatch, record: Any) -> None:
    """Record the transport role BELOW ``_create_transport``, not instead of it.

    ``_transport_factories()`` (``_factory.py:116``) is the real leaf: it is
    called only after ``_create_transport`` has already handled an injected
    ``TransportLike`` instance and the ``_event_bus`` injection, so replacing its
    per-config-type entries leaves both of those branches executing for real
    while still keeping every built-in transport SDK out of the test.
    """
    real_transport_factories = _factory._transport_factories

    def _fake_transport_factories() -> dict[type, Any]:
        return {
            config_type: (lambda config, event_bus: record(config))
            for config_type in real_transport_factories()
        }

    monkeypatch.setattr(_factory, "_transport_factories", _fake_transport_factories)


@dataclass(frozen=True)
class ConstructedInputs:
    """What ``create_session`` actually resolved, captured at the factory seams."""

    stt_spec: Any
    tts_spec: Any
    vad_spec: Any | None
    noise_spec: Any | None
    echo_spec: Any | None
    transport_spec: Any
    called: tuple[str, ...]
    session_config: Any
    session: Session

    @property
    def vad(self) -> Any | None:
        return self.session_config.vad

    @property
    def enable_vad(self) -> bool:
        return bool(self.session_config.enable_vad)

    @property
    def enable_echo_cancellation(self) -> bool:
        return bool(self.session_config.enable_echo_cancellation)

    @property
    def auto_turn_from_stt_final(self) -> bool:
        return bool(self.session_config.auto_turn_from_stt_final)


def capture_construction(
    monkeypatch: pytest.MonkeyPatch,
    config: EasyConfig,
    *,
    passthrough: frozenset[str] = frozenset(),
) -> ConstructedInputs:
    """Run the real ``create_session`` with every role constructor recorded.

    Roles named in ``passthrough`` run their real factory instead (used by the
    degrade-vs-raise rows that need the real provider construction to actually
    raise or degrade). A role whose leaf never fires (a live injected
    instance, or the VAD/noise-reducer stage being skipped entirely) falls
    back to the value ``_make_session_config`` actually received, which
    preserves identity for injected instances and is ``None`` for a skipped
    stage.
    """
    captured: dict[str, Any] = {
        "stt": _UNSET,
        "tts": _UNSET,
        "vad": _UNSET,
        "noise": _UNSET,
        "echo": _UNSET,
        "transport": _UNSET,
    }
    called: list[str] = []

    def _recorder(role: str, fake_cls: type):
        def _record(*args: object, **kwargs: object) -> object:
            spec = args[0] if args else kwargs.get("config")
            captured[role] = spec
            called.append(role)
            return fake_cls()

        return _record

    for role, attr_name, fake_cls in _LEAF_TARGETS:
        if role in passthrough:
            continue
        monkeypatch.setattr(_factory, attr_name, _recorder(role, fake_cls))

    if "transport" not in passthrough:
        _patch_transport_leaves(monkeypatch, _recorder("transport", _FakeTransport))

    session_configs: list[Any] = []
    real_make_session_config = _factory._make_session_config

    def _wrap_make_session_config(*args: object, **kwargs: object) -> object:
        session_config = real_make_session_config(*args, **kwargs)
        session_configs.append(session_config)
        return session_config

    monkeypatch.setattr(_factory, "_make_session_config", _wrap_make_session_config)

    session = _factory.create_session(config)
    assert len(session_configs) == 1
    session_config = session_configs[0]

    def _resolved(role: str, built_value: Any) -> Any:
        return captured[role] if captured[role] is not _UNSET else built_value

    return ConstructedInputs(
        stt_spec=_resolved("stt", session_config.stt),
        tts_spec=_resolved("tts", session_config.tts),
        vad_spec=_resolved("vad", session_config.vad),
        noise_spec=_resolved("noise", session_config.noise_reducer),
        echo_spec=_resolved("echo", session_config.echo_canceller),
        transport_spec=_resolved("transport", session_config.transport),
        called=tuple(called),
        session_config=session_config,
        session=session,
    )


def assert_preview_matches_construction(
    plan: ProviderPlan, built: ConstructedInputs, config: EasyConfig
) -> None:
    """The role-by-role invariants both paths must agree on."""
    stt_selection = plan.selected["stt"]
    assert stt_selection.config_type == type(built.stt_spec).__name__
    tts_selection = plan.selected["tts"]
    assert tts_selection.config_type == type(built.tts_spec).__name__

    for selection, spec in ((stt_selection, built.stt_spec), (tts_selection, built.tts_spec)):
        model_field = getattr(type(spec), "MODEL_FIELD", "model")
        assert selection.model == getattr(spec, model_field, None)

    assert (plan.selected["vad"].provider == "off") == (built.vad is None)
    assert (plan.selected["noise_reducer"].provider == "off") == (
        built.session_config.noise_reducer is None
    )
    assert (plan.selected["echo_canceller"].provider == "livekit") == (
        built.enable_echo_cancellation
    )
    assert plan.selected["transport"].config_type == type(built.transport_spec).__name__
    assert plan.selected["agent"].provider == ("python" if config.agent is not None else "none")

    # Invariant 8, narrowed to what this FAKE-constructor harness can actually
    # prove, and written as an EXCLUSION so a blocking category added later (for
    # example DX1-5's ``missing_backend:``) is covered here by default instead of
    # slipping past an allow-list. Only ``missing_extra:`` is exempt: every leaf
    # provider constructor is patched to a stub that always "succeeds", so a
    # missing-EXTRA blocking gap is untestable here by construction — real
    # extra-vs-raise parity is covered separately by
    # ``test_construction_contracts.py``, which runs the REAL factories. A
    # missing-ENV credential gap is different: ``EasyConfig`` raises
    # ``EASYCAT_E203`` at *construction* (before any factory patch takes
    # effect), so a successful ``capture_construction`` call already proves no
    # required credential was missing — if the plan disagreed here, that would
    # be a genuine divergence worth catching (and
    # ``test_inline_credential_constructs_while_the_plan_blocks_on_missing_env``
    # characterizes the one shape where it does).
    unexplained = [
        error for error in plan.blocking_errors() if not error.startswith("missing_extra:")
    ]
    assert not unexplained, unexplained
