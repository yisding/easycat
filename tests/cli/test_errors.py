"""Error-code registry + factory + CLI exit-code mapping."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from easycat.cli._errors import exit_code_for
from easycat.errors import (
    EASYCAT_E101,
    EASYCAT_E104,
    EASYCAT_E201,
    EASYCAT_E202,
    EASYCAT_E203,
    EASYCAT_E205,
    EASYCAT_E501,
    EASYCAT_E602,
    REGISTRY,
    EasyCatError,
    EasyConfigError,
    SetupIssue,
    all_codes,
    register,
    suggest_codes,
)
from easycat.planning.selection import selection_error, selection_issue


def test_config_errors_share_the_public_easycat_error_boundary() -> None:
    err = EasyConfigError("agent is required")

    assert isinstance(err, EasyCatError)
    assert isinstance(err, ValueError)
    assert err.code == "EASYCAT_E105"
    assert str(err) == "agent is required"
    assert err.rendered_fix() is not None
    assert exit_code_for(err.code) == 2


def test_every_registered_code_has_factory() -> None:
    """Every code in the registry has a headline that renders."""
    for code, entry in REGISTRY.items():
        assert entry.code == code
        assert entry.headline
        assert entry.cause
        assert entry.fix


def test_duplicate_registration_raises() -> None:
    with pytest.raises(RuntimeError, match="Duplicate"):
        register(
            "EASYCAT_E101",
            "dup",
            cause="x",
            fix="y",
        )


def test_factory_substitutes_context() -> None:
    err = EASYCAT_E101(target="/tmp/demo")
    assert isinstance(err, EasyCatError)
    assert err.code == "EASYCAT_E101"
    assert "/tmp/demo" in err.message
    assert err.context == {"target": "/tmp/demo"}


def test_factory_missing_placeholder_raises() -> None:
    # E101's template references {target!r}; calling without it should
    # raise a clear RuntimeError (caught at dev time, not at runtime).
    with pytest.raises(RuntimeError, match="headline template"):
        EASYCAT_E101()


def test_factory_unused_kwargs_are_stored_not_substituted() -> None:
    """Extra kwargs are kept in ``context`` even if the template ignores them."""
    err = EASYCAT_E104(provider="foo", available="a, b", hint=" Did you mean 'openai'?")
    assert "Did you mean" in err.message
    assert err.context["provider"] == "foo"


def test_rendered_message_carries_fix_and_explain_hint() -> None:
    """A registered error's ``str()`` includes the fix and the explain hint."""
    err = EASYCAT_E101(target="/tmp/demo")
    rendered = str(err)
    assert "EASYCAT_E101: " in rendered
    assert "Fix:" in rendered
    assert "easycat explain EASYCAT_E101" in rendered
    fix = err.rendered_fix()
    assert fix is not None
    assert "Choose a new name" in fix


def test_factory_fix_override_replaces_the_registry_text_everywhere() -> None:
    """U-8: ``fix=`` is the one reserved factory kwarg, and it is not context.

    A code that spans several defect shapes (E602) can only carry generic
    registry advice; a raiser that knows the exact defect overrides it, and the
    SAME string must then reach ``str(exc)``, ``rendered_fix()``, and
    ``SetupIssue.from_error``'s ``fix`` — the byte-identity contract.
    """
    override = "Add token = 'bearer-env:TW_TOK' to `[voice.default]`."
    err = EASYCAT_E602(path="easycat.toml", problem="no token", fix=override)

    assert err.rendered_fix() == override
    assert f"Fix: {override}" in str(err)
    assert "fix" not in err.context
    assert err.context == {"path": "easycat.toml", "problem": "no token"}

    issue = SetupIssue.from_error(err, reason="incomplete_selection")
    assert issue.fix == override

    # The registry entry itself is untouched: ``easycat explain`` still shows
    # the code-level documentation.
    assert "needs a known `transport`" in REGISTRY["EASYCAT_E602"].fix
    assert EASYCAT_E602(path="p", problem="q").rendered_fix() == REGISTRY["EASYCAT_E602"].fix


def test_phone_profile_defect_fix_is_actionable_on_every_surface(tmp_path: Path) -> None:
    """U-9: plan/doctor/startup all read one fix that names the missing token."""
    from easycat.project import load_manifest

    manifest_path = tmp_path / "easycat.toml"
    manifest_path.write_text(
        '[project]\nname = "phone"\n\n[voice.default]\ntransport = "twilio"\n',
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)

    (defect,) = manifest.profile_defects("default")
    fix = defect.rendered_fix()
    assert fix is not None
    assert "token = 'bearer-env:TWILIO_STREAM_TOKEN_SECRET'" in fix
    assert "needs a known `transport`" not in fix

    with pytest.raises(EasyCatError) as raised:
        manifest.to_easyconfig("default", resolve_agent=False)
    assert raised.value.code == "EASYCAT_E602"
    assert raised.value.rendered_fix() == fix


def test_rendered_message_unknown_code_is_bare() -> None:
    """An unregistered code renders ``CODE: message`` with no fix/hint."""
    err = EasyCatError("EASYCAT_E999", "boom")
    rendered = str(err)
    assert rendered == "EASYCAT_E999: boom"
    assert err.rendered_fix() is None


def test_render_survives_braced_fix_missing_context() -> None:
    """A fix template with a placeholder absent from context falls back."""
    code = "EASYCAT_TEST_RENDER"
    register(code, "headline", cause="c", fix="set {missing} now")
    try:
        err = EasyCatError(code, "headline")
        rendered = str(err)
        assert "Fix: set {missing} now" in rendered
        assert "easycat explain EASYCAT_TEST_RENDER" in rendered
    finally:
        REGISTRY.pop(code, None)


def test_optional_extra_errors_show_package_and_repo_install_commands() -> None:
    missing_extra = str(EASYCAT_E202(extra="openai-agents"))
    assert "uv add 'easycat[openai-agents]'" in missing_extra
    assert "uv sync --extra openai-agents --group dev" in missing_extra

    smart_turn = str(EASYCAT_E205())
    assert "uv add 'easycat[smart-turn]'" in smart_turn
    assert "uv sync --extra smart-turn --group dev" in smart_turn

    unknown_provider = str(EASYCAT_E104(provider="depgarm", available="deepgram", hint=""))
    assert "uv add 'easycat[deepgram]'" in unknown_provider
    assert "uv sync --extra deepgram --group dev" in unknown_provider


def test_python_version_error_shows_repo_dev_sync_command() -> None:
    message = str(EASYCAT_E201(found="3.10"))

    assert "uv python install 3.12" in message
    assert "uv sync --python 3.12 --group dev" in message
    assert "uv sync --python 3.12`" not in message


def test_factory_allows_context_key_named_code() -> None:
    """E501's headline has ``{code}``; that context key must not collide."""
    err = EASYCAT_E501(code="E999")
    assert err.code == "EASYCAT_E501"
    assert err.context == {"code": "E999"}
    assert "Unknown error code 'E999'" in err.message


def test_suggest_codes_returns_close_matches() -> None:
    matches = suggest_codes("EASYCAT_E10")
    assert any(m.startswith("EASYCAT_E1") for m in matches)


def test_runtime_and_bundle_ranges_are_registered() -> None:
    """The documented E3xx (runtime) and E4xx (bundle/replay) ranges exist."""
    for code in (
        "EASYCAT_E301",
        "EASYCAT_E302",
        "EASYCAT_E303",
        "EASYCAT_E304",
        "EASYCAT_E305",
        "EASYCAT_E401",
        "EASYCAT_E402",
        "EASYCAT_E403",
    ):
        assert code in REGISTRY


def test_runtime_timeout_errors_carry_registered_codes() -> None:
    """Runtime timeouts are catchable through the public EasyCatError base."""
    from easycat.timeouts import AgentTimeoutError, STTTimeoutError, TTSTimeoutError

    errors = (
        STTTimeoutError("stt", 1.0),
        AgentTimeoutError(1.0),
        TTSTimeoutError("tts", 1.0),
    )
    assert [err.code for err in errors] == [
        "EASYCAT_E301",
        "EASYCAT_E302",
        "EASYCAT_E303",
    ]
    for err in errors:
        assert isinstance(err, EasyCatError)
        assert err.code in REGISTRY
        assert err.context["timeout"] == 1.0


def test_exit_code_mapping() -> None:
    assert exit_code_for("EASYCAT_E101") == 101
    assert exit_code_for("EASYCAT_E102") == 4
    assert exit_code_for("EASYCAT_E103") == 2
    assert exit_code_for("EASYCAT_E203") == 3
    assert exit_code_for("EASYCAT_E501") == 2
    # E-5: DX2 PR2 reports these three codes on new surfaces without changing
    # what a CLI run exits with.
    assert exit_code_for("EASYCAT_E210") == 1
    assert exit_code_for("EASYCAT_E602") == 1
    assert exit_code_for("EASYCAT_E604") == 1
    # Unlisted codes fall back to 1.
    assert exit_code_for("EASYCAT_E999") == 1


# ── SetupIssue: the shared coded projection (DX2) ──────────────────────

_SECRET_SHAPED = "sk-live-secret-token-abcdef1234567890"


def test_setup_issue_projects_registry_text() -> None:
    """U-7: an issue and the raised exception carry byte-identical text."""
    issue = SetupIssue.from_code(
        EASYCAT_E203, reason="missing_env", field="X", role="stt", var="X"
    )
    raised = EASYCAT_E203(var="X")

    assert issue.code == "EASYCAT_E203"
    assert issue.detail == raised.message
    assert issue.fix == raised.rendered_fix()
    assert issue.severity == "blocking"
    assert set(issue.as_dict()) <= {"code", "reason", "field", "role", "detail", "fix", "severity"}
    assert issue.as_dict()["reason"] == "missing_env"
    assert issue.as_dict()["role"] == "stt"


def test_setup_issue_omits_empty_optional_fields() -> None:
    payload = SetupIssue(code="EASYCAT_E203", reason="missing_env").as_dict()

    assert payload == {"code": "EASYCAT_E203", "reason": "missing_env", "severity": "blocking"}


def test_setup_issue_missing_substitution_falls_back_to_template() -> None:
    """U-8: a context-less error must not explode the projection."""
    issue = SetupIssue.from_error(EasyCatError("EASYCAT_E203", ""), reason="missing_env")

    assert issue.code == "EASYCAT_E203"
    assert issue.fix == REGISTRY["EASYCAT_E203"].fix


def test_selection_error_preserves_easycat_codes() -> None:
    """S-1: a coded error already names the right cause; pass it through."""
    coded = EASYCAT_E104(provider="x", available="openai", hint="")

    assert selection_error(coded, profile="p") is coded
    assert selection_error(coded, profile="p").code == "EASYCAT_E104"


def test_selection_error_maps_value_error_to_e602() -> None:
    """S-2: the planner's bare ValueError becomes the coded manifest error."""
    err = selection_error(ValueError("Unknown VAD backend 'silro'"), profile="p")

    assert err.code == "EASYCAT_E602"
    assert err.context["path"] == "[voice.p]"
    assert "silro" in err.message


def test_selection_issue_redacts_a_passed_through_easycat_error() -> None:
    """S-3: the redaction ``SetupIssue.from_error`` structurally cannot do."""
    coded = EASYCAT_E104(provider=_SECRET_SHAPED, available="openai", hint="")

    issue = selection_issue(coded, profile="p")

    assert issue.code == "EASYCAT_E104"
    assert issue.reason == "unresolvable_profile"
    assert _SECRET_SHAPED not in issue.detail
    assert _SECRET_SHAPED not in issue.fix
    assert "[REDACTED_SECRET]" in issue.detail


def test_selection_error_redacts_a_secret_shaped_planner_message() -> None:
    err = selection_error(ValueError(f"Unknown VAD backend {_SECRET_SHAPED!r}"), profile="p")

    assert err.code == "EASYCAT_E602"
    assert _SECRET_SHAPED not in err.message
    assert "[REDACTED_SECRET]" in err.message


# ── DX2 PR2: one cause, one code, on every surface ────────────────────


@dataclass(frozen=True)
class _SameCause:
    """One manifest defect and the code each surface must report for it."""

    id: str
    body: str
    code: str
    absent: tuple[str, ...] = ()
    plan_field: str = ""
    plan_role: str = ""
    doctor_row: str = ""
    raises: bool = False
    startup_extra_module: str = ""


_SAME_CAUSE_CASES: tuple[_SameCause, ...] = (
    _SameCause(
        id="missing-credential",
        body='transport = "websocket"\nstt = "deepgram"\n',
        code="EASYCAT_E203",
        plan_field="DEEPGRAM_API_KEY",
        plan_role="stt",
        doctor_row="env_deepgram",
    ),
    _SameCause(
        id="missing-extra",
        body='transport = "webrtc"\n',
        code="EASYCAT_E202",
        absent=("aiortc",),
        plan_field="webrtc",
        plan_role="transport",
        doctor_row="extra_webrtc",
        # The startup column drives the REAL WebRTC call site
        # (``_webrtc_audio.OutboundAudioSource.create_track`` ->
        # ``require_module("aiortc", extra="webrtc", …)``) with ``aiortc``
        # forced absent, so dropping ``extra=`` there — which would make the
        # startup error uncoded — turns this row red. A synthetic module name
        # would prove only what E-7 already proves.
        startup_extra_module="aiortc",
    ),
    _SameCause(
        id="invalid-provider-stt",
        body='transport = "websocket"\nstt = "opnai"\n',
        code="EASYCAT_E104",
        raises=True,
    ),
    _SameCause(
        id="invalid-provider-vad",
        body='transport = "websocket"\nvad = "silro"\n',
        code="EASYCAT_E602",
        raises=True,
    ),
    _SameCause(
        id="incomplete-selection",
        body='transport = "twilio"\n',
        code="EASYCAT_E602",
        plan_field="[voice.default]",
        doctor_row="selection_incomplete_selection",
    ),
    _SameCause(
        id="unset-reference",
        body='transport = "twilio"\ntoken = "bearer-env:TW_TOK"\n',
        code="EASYCAT_E604",
        plan_field="TW_TOK",
        doctor_row="env_tw_tok",
    ),
)


def _hide_module(monkeypatch: pytest.MonkeyPatch, module: str) -> None:
    """Make the REAL ``require_module`` see *module* as not installed.

    Patches the seam ``easycat._extras.require_module`` itself consults —
    ``sys.modules`` plus ``importlib.util.find_spec`` — rather than stubbing
    ``require_module``, so the production call site's own ``extra=`` argument is
    what the assertion reads. Identical in the credential-free lane and in an
    extras lane that really has ``aiortc`` installed.
    """
    import importlib.util

    real_find_spec = importlib.util.find_spec
    monkeypatch.delitem(sys.modules, module, raising=False)
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *args, **kwargs: (
            None if name == module else real_find_spec(name, *args, **kwargs)
        ),
    )


def _force_modules(monkeypatch: pytest.MonkeyPatch, absent: tuple[str, ...]) -> None:
    """Pin extras availability at the planner's single private seam.

    Every extra check flows through ``provider_plan._module_available``, so
    forcing it makes these assertions identical in the credential-free lane and
    in an extras lane. Everything not named in *absent* reads as present, so a
    case only ever goes red for the defect it is about.
    """
    from easycat.planning import provider_plan

    missing = set(absent)
    monkeypatch.setattr(provider_plan, "_module_available", lambda module: module not in missing)


@pytest.mark.parametrize("case", _SAME_CAUSE_CASES, ids=lambda case: case.id)
def test_same_cause_reports_one_code(
    case: _SameCause, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The DX2 acceptance case: plan, doctor, and startup name ONE code.

    Each row builds a single manifest and asserts the same ``EASYCAT_Exxx``
    from ``easycat plan --json``, ``easycat doctor --manifest … --json``, and
    the raise a running application hits for the same defect.
    """
    from typer.testing import CliRunner

    from easycat.cli._app import _register_commands, app
    from easycat.project import load_manifest

    _register_commands()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.setenv("NO_COLOR", "1")
    for var in ("DEEPGRAM_API_KEY", "TW_TOK"):
        monkeypatch.delenv(var, raising=False)
    _force_modules(monkeypatch, case.absent)
    manifest_path = tmp_path / "easycat.toml"
    manifest_path.write_text(
        '[project]\nname = "same-cause"\n\n[voice.default]\n' + case.body, encoding="utf-8"
    )
    runner = CliRunner()

    # ── plan surface ──
    planned = runner.invoke(app, ["plan", "--manifest", str(manifest_path), "--json"])
    plan_payload = json.loads(planned.stdout)
    if case.raises:
        assert planned.exit_code != 0
        assert plan_payload["code"] == case.code
    else:
        issue = next(i for i in plan_payload["issues"] if i["field"] == case.plan_field)
        assert issue["code"] == case.code
        assert issue.get("role", "") == case.plan_role
        assert issue["fix"]
        assert plan_payload["has_blocking_errors"] is True

    # ── doctor surface ──
    diagnosed = runner.invoke(
        app, ["doctor", "--manifest", str(manifest_path), "--json", "--environment", "production"]
    )
    doctor_payload = json.loads(diagnosed.stdout)
    if case.raises:
        assert diagnosed.exit_code != 0
        assert doctor_payload["code"] == case.code
    else:
        assert diagnosed.exit_code == 1
        row = next(r for r in doctor_payload["checks"] if r["name"] == case.doctor_row)
        assert row["status"] == "fail"
        assert row["code"] == case.code
        assert row.get("role", "") == case.plan_role

    # ── startup surface ──
    if case.startup_extra_module:
        from easycat.transports._webrtc_audio import OutboundAudioSource

        _hide_module(monkeypatch, case.startup_extra_module)
        with pytest.raises(ImportError) as import_info:
            OutboundAudioSource().create_track()
        assert import_info.value.code == case.code  # type: ignore[attr-defined]
        assert import_info.value.context["extra"] == "webrtc"  # type: ignore[attr-defined]
    else:
        with pytest.raises(EasyCatError) as raise_info:
            load_manifest(manifest_path).to_easyconfig("default", resolve_agent=False)
        assert raise_info.value.code == case.code


def test_setup_issues_are_stable_and_deduped(monkeypatch: pytest.MonkeyPatch) -> None:
    """E-1: deterministic order, no duplicates, earliest pipeline role wins."""
    from easycat.planning.provider_plan import ProviderPlan, ProviderSelection
    from easycat.planning.selection import plan_issues

    def _selection(role: str, extra: str | None, env: str | None) -> ProviderSelection:
        return ProviderSelection(
            role=role,  # type: ignore[arg-type]
            provider=f"{role}-provider",
            model=None,
            config_type=f"{role.title()}Config",
            extra=extra,
            required_env=env,
            capabilities=frozenset(),
        )

    plan = ProviderPlan(
        profile="default",
        # ``tts`` also needs SHARED_KEY, and ``transport`` also needs the
        # ``webrtc`` extra — both must be reported once, on the earlier role.
        selected={
            "stt": _selection("stt", "stt-extra", "SHARED_KEY"),
            "tts": _selection("tts", "webrtc", "SHARED_KEY"),
            "transport": _selection("transport", "webrtc", None),
        },
        missing_env=("SHARED_KEY",),
        missing_extras=("stt-extra", "webrtc"),
        warnings=(),
    )

    issues = plan_issues(plan)

    # Ordered by (severity, pipeline role, code, field): both ``stt`` rows come
    # before the ``tts`` one, and within ``stt`` E202 precedes E203.
    assert [(i.reason, i.field, i.role) for i in issues] == [
        ("missing_extra", "stt-extra", "stt"),
        ("missing_env", "SHARED_KEY", "stt"),
        ("missing_extra", "webrtc", "tts"),
    ]
    assert plan_issues(plan) == issues


def test_related_codes_link_credential_extra_and_manifest_failures() -> None:
    """E-2: ``easycat explain`` routes a reader across the same failure family."""
    assert "EASYCAT_E203" in REGISTRY["EASYCAT_E602"].related
    assert "EASYCAT_E202" in REGISTRY["EASYCAT_E602"].related
    assert "EASYCAT_E202" in REGISTRY["EASYCAT_E210"].related
    assert "EASYCAT_E602" in REGISTRY["EASYCAT_E202"].related


def test_no_new_error_codes_were_added() -> None:
    """E-3: the compat guard for "add optional fields, not codes".

    DX2 reports existing causes with a shared record; it registers nothing. A
    new code here is a deliberate act that must update this baseline (and the
    ``easycat explain`` docs) in the same commit.
    """
    assert set(all_codes()) == {
        "EASYCAT_E101",
        "EASYCAT_E102",
        "EASYCAT_E103",
        "EASYCAT_E104",
        "EASYCAT_E105",
        "EASYCAT_E201",
        "EASYCAT_E202",
        "EASYCAT_E203",
        "EASYCAT_E204",
        "EASYCAT_E205",
        "EASYCAT_E206",
        "EASYCAT_E207",
        "EASYCAT_E208",
        "EASYCAT_E209",
        "EASYCAT_E210",
        "EASYCAT_E301",
        "EASYCAT_E302",
        "EASYCAT_E303",
        "EASYCAT_E304",
        "EASYCAT_E305",
        "EASYCAT_E401",
        "EASYCAT_E402",
        "EASYCAT_E403",
        "EASYCAT_E404",
        "EASYCAT_E501",
        "EASYCAT_E601",
        "EASYCAT_E602",
        "EASYCAT_E603",
        "EASYCAT_E604",
        "EASYCAT_E605",
    }
