"""Error-code registry + factory + CLI exit-code mapping."""

from __future__ import annotations

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
    REGISTRY,
    EasyCatError,
    EasyConfigError,
    SetupIssue,
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
