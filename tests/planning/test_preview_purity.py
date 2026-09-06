"""DX1-1 characterization — the planner stays pure, repeatable, and secret-free.

Covers the roadmap's "repeated previews neither mutate input nor allocate
runtime resources" and "diagnostic output contains credential names and
presence, never values" acceptance gates.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from dataclasses import fields

import pytest

from easycat.config import EasyConfig
from easycat.planning import build_provider_plan
from easycat.validation.redaction import contains_unredacted_sensitive_text


class _Agent:
    async def run(self, text: str) -> str:
        return "ok"


def _selection_to_dict(selection: object) -> dict[str, object]:
    from easycat.cli.plan import _selection_to_dict as _to_dict

    return _to_dict(selection)


# ── Repeated preview is pure ────────────────────────────────────────────


def test_repeated_preview_is_equal_and_leaves_the_config_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    config = EasyConfig(agent=_Agent(), debug="off")

    # Snapshot by VALUE (a detached deep copy), not by live reference: storing a
    # reference and comparing it against the same live attribute afterwards
    # compares an object with itself, so only whole-field REPLACEMENT (the id()
    # half) could ever be caught — never the likelier failure, a planner that
    # mutates a nested config object in place.
    def _copy(value: object) -> tuple[object, bool]:
        """Return ``(detached copy, is it comparable by value)``.

        A field holding a live collaborator (the agent here) deep-copies fine
        but compares by identity, so a copy of it can never equal the original;
        those fields keep the ``id()`` identity check alone.
        """
        try:
            copied = copy.deepcopy(value)
        except (TypeError, ValueError, copy.Error):  # pragma: no cover - defensive
            return value, False
        return copied, bool(copied == value)

    def _live() -> dict[str, tuple[object, int]]:
        return {
            f.name: (getattr(config, f.name), id(getattr(config, f.name))) for f in fields(config)
        }

    before = {name: (*_copy(value), obj_id) for name, (value, obj_id) in _live().items()}
    plan1 = build_provider_plan(config)
    plan2 = build_provider_plan(config)
    after = _live()

    # Guard the guard: at least the nested provider/turn dataclasses must be
    # value-comparable, or the mutation check below would silently degrade to
    # the identity check it replaces.
    assert sum(comparable for _copied, comparable, _id in before.values()) >= 3

    assert plan1 == plan2
    for name, (value_before, comparable, id_before) in before.items():
        value_after, id_after = after[name]
        assert id_after == id_before, f"{name} was replaced by a new object"
        if not comparable:
            continue
        assert value_after == value_before, f"{name} was mutated in place"


# ── No provider constructor / heavy SDK reachable from the preview ─────


def test_preview_never_calls_a_role_constructor_easyconfig(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seven leaf constructors REACHABLE from the planner never fire.

    Patched on their DEFINITION modules, not on ``easycat.config._factory``:
    patching ``_factory``'s copies would prove nothing, because
    ``build_provider_plan`` never imports ``easycat.config._factory`` at all
    (``tests/planning/test_boundary.py`` asserts exactly that), so those
    patched names would be unreachable from the code under test.
    """
    import easycat.echo_cancellation as echo_mod
    import easycat.noise_reduction as noise_mod
    import easycat.stt.factory as stt_mod
    import easycat.tts.factory as tts_mod
    import easycat.vad.factory as vad_mod
    from easycat.config import _factory

    def boom(*_a: object, **_k: object) -> object:
        raise AssertionError("the planner must never construct a provider")

    monkeypatch.setattr(stt_mod, "create_stt_provider", boom)
    monkeypatch.setattr(stt_mod, "create_stt_provider_from_config", boom)
    monkeypatch.setattr(tts_mod, "create_tts_provider", boom)
    monkeypatch.setattr(tts_mod, "create_tts_provider_from_config", boom)
    monkeypatch.setattr(vad_mod, "create_vad", boom)
    monkeypatch.setattr(noise_mod, "create_noise_reducer", boom)
    monkeypatch.setattr(echo_mod, "create_echo_canceller", boom)
    # Belt-and-braces only: there is no transport constructor outside
    # ``_factory``, and today's planner cannot reach this name at all (the
    # subprocess peer below proves ``easycat.config._factory`` never even
    # loads). It is patched so a future planner that grows a transport
    # construction call fails here instead of silently allocating one.
    monkeypatch.setattr(_factory, "_create_transport", boom)

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    config = EasyConfig(agent=_Agent(), debug="off")
    build_provider_plan(config)  # must not raise


def test_preview_never_calls_a_role_constructor_voiceprofile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import easycat.echo_cancellation as echo_mod
    import easycat.noise_reduction as noise_mod
    import easycat.stt.factory as stt_mod
    import easycat.tts.factory as tts_mod
    import easycat.vad.factory as vad_mod
    from easycat.project.schema import VoiceProfile

    def boom(*_a: object, **_k: object) -> object:
        raise AssertionError("the planner must never construct a provider")

    monkeypatch.setattr(stt_mod, "create_stt_provider", boom)
    monkeypatch.setattr(stt_mod, "create_stt_provider_from_config", boom)
    monkeypatch.setattr(tts_mod, "create_tts_provider", boom)
    monkeypatch.setattr(tts_mod, "create_tts_provider_from_config", boom)
    monkeypatch.setattr(vad_mod, "create_vad", boom)
    monkeypatch.setattr(noise_mod, "create_noise_reducer", boom)
    monkeypatch.setattr(echo_mod, "create_echo_canceller", boom)

    profile = VoiceProfile(name="default", transport="local", vad="silero")
    build_provider_plan(profile, environ={"OPENAI_API_KEY": "x"})  # must not raise


def test_preview_never_loads_the_construction_path() -> None:
    """Subprocess peer: a rebound name in this interpreter cannot fake it."""
    code = (
        "import sys; "
        "from easycat.planning import build_provider_plan; "
        "from easycat.project.schema import VoiceProfile; "
        "p = VoiceProfile(name='default', transport='local', vad='silero'); "
        "build_provider_plan(p, environ={'OPENAI_API_KEY': 'x'}); "
        "print('easycat.config._factory' in sys.modules, "
        "'easycat.session._session' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False False", result.stdout


# ── No credential leakage ───────────────────────────────────────────────


def test_preview_output_carries_no_credential_values_easyconfig(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-" + "x" * 40
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTTConfig

    config = EasyConfig(stt=OpenAIRealtimeSTTConfig(api_key=secret), agent=_Agent(), debug="off")
    plan = build_provider_plan(config)

    dumped = json.dumps({role: _selection_to_dict(sel) for role, sel in plan.selected.items()})
    assert secret not in repr(plan)
    assert secret not in dumped
    assert not contains_unredacted_sensitive_text(dumped)


def test_preview_output_carries_no_credential_values_server_manifest(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manifest/server path carries a different secret: the profile token.

    ``VoiceServer.plan_payload()`` resolves a manifest ``VoiceProfile``, whose
    fields (``name``/``transport``/``agent``/``stt``/``tts``/``vad``/``debug``/
    ``path``/``stream_url``/``token``) have no ``api_key`` — so asserting the
    EasyConfig secret above against it would be vacuous. The token is the one
    field on that path that carries a credential, so the profile under test is a
    ``twilio`` one, where ``token`` is REQUIRED (``project/manifest.py`` raises
    ``EASYCAT_E602`` without it) and is resolved from the environment into the
    transport config. Both projections are checked: the server payload built
    from the raw profile, and the plan built from the resolved ``EasyConfig``,
    whose object graph really does contain the secret value.
    """
    from easycat.project.loader import load_manifest
    from easycat.server import VoiceServer

    resolved_token = "sk-live-secret-token-abcdef1234567890"
    monkeypatch.setenv("TWILIO_STREAM_TOKEN_SECRET", resolved_token)
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", resolved_token)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")

    manifest_path = tmp_path / "easycat.toml"
    manifest_path.write_text(
        """\
[project]
name = "preview-purity-test"

[server]
host = "127.0.0.1"
port = 0
auth = "bearer-env:EASYCAT_SERVE_TOKEN"

[voice.default]
transport = "twilio"
stt = "openai/realtime"
tts = "openai"
vad = "silero"
token = "bearer-env:TWILIO_STREAM_TOKEN_SECRET"
""",
        encoding="utf-8",
    )
    server = VoiceServer.from_manifest(manifest_path)
    payload = server.plan_payload()
    dumped = json.dumps(payload)

    assert payload["selected"], "the profile must resolve, or this asserts nothing"
    assert resolved_token not in dumped
    assert not contains_unredacted_sensitive_text(dumped)

    # The EasyConfig the manifest resolves DOES hold the secret (the token is
    # closed over by the transport's stream-token validator), so the plan built
    # from it is the stronger of the two projections.
    manifest = load_manifest(manifest_path)
    resolved_config = manifest.to_easyconfig("default", resolve_agent=False)
    plan = build_provider_plan(resolved_config, environ={"OPENAI_API_KEY": "sk-stub"})
    plan_dumped = json.dumps(
        {role: _selection_to_dict(sel) for role, sel in plan.selected.items()}
    )
    assert resolved_token not in plan_dumped
    assert resolved_token not in repr(plan)
    assert not contains_unredacted_sensitive_text(plan_dumped)
