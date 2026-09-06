"""``easycat.planning.selection`` — the shared load/plan/coded-issue boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from easycat.errors import EasyCatError
from easycat.planning import provider_plan
from easycat.planning.selection import (
    build_manifest_plan,
    load_selected_profile,
    plan_issues,
    plan_selected_profile,
)
from easycat.project import parse_manifest


def _manifest(body: str, tmp_path: Path) -> Path:
    path = tmp_path / "easycat.toml"
    path.write_text(f'[project]\nname = "sel"\n\n{body}\n', encoding="utf-8")
    return path


def _absent(monkeypatch: pytest.MonkeyPatch, *modules: str) -> None:
    """Make ``provider_plan._module_available`` report *modules* absent."""
    absent = set(modules)
    real = provider_plan._module_available
    monkeypatch.setattr(
        provider_plan,
        "_module_available",
        lambda module: False if module in absent else real(module),
    )


def test_manifest_plan_defect_severity_is_scoped() -> None:
    """U-10: ``[server] auth`` is a warning; a profile token is blocking."""
    server_auth = parse_manifest(
        {
            "project": {"name": "sel"},
            "server": {"auth": "bearer-env:SRV_TOK"},
            "voice": {"default": {"transport": "webrtc"}},
        }
    )
    plan = build_manifest_plan(server_auth, environ={"OPENAI_API_KEY": "sk-stub"})
    (auth_defect,) = plan.defects
    assert auth_defect.code == "EASYCAT_E604"
    assert auth_defect.reason == "unset_reference"
    assert auth_defect.field == "SRV_TOK"
    assert auth_defect.severity == "warning"

    profile_token = parse_manifest(
        {
            "project": {"name": "sel"},
            "voice": {"default": {"transport": "twilio", "token": "bearer-env:TW_TOK"}},
        }
    )
    plan = build_manifest_plan(profile_token, environ={"OPENAI_API_KEY": "sk-stub"})
    (token_defect,) = plan.defects
    assert token_defect.code == "EASYCAT_E604"
    assert token_defect.reason == "unset_reference"
    assert token_defect.severity == "blocking"

    missing_token = parse_manifest(
        {"project": {"name": "sel"}, "voice": {"default": {"transport": "twilio"}}}
    )
    plan = build_manifest_plan(missing_token, environ={"OPENAI_API_KEY": "sk-stub"})
    (incomplete,) = plan.defects
    assert incomplete.code == "EASYCAT_E602"
    assert incomplete.reason == "incomplete_selection"
    assert incomplete.severity == "blocking"
    assert incomplete.field == "[voice.default]"


def test_manifest_plan_drops_a_satisfied_reference() -> None:
    manifest = parse_manifest(
        {
            "project": {"name": "sel"},
            "server": {"auth": "bearer-env:SRV_TOK"},
            "voice": {"default": {"transport": "webrtc"}},
        }
    )

    plan = build_manifest_plan(manifest, environ={"OPENAI_API_KEY": "sk-stub", "SRV_TOK": "value"})

    assert plan.defects == ()


def test_manifest_plan_defects_do_not_block_yet() -> None:
    """PR1 is behavior-preserving for readiness; PR2 owns ``blocking_errors``."""
    manifest = parse_manifest(
        {"project": {"name": "sel"}, "voice": {"default": {"transport": "twilio"}}}
    )

    plan = build_manifest_plan(
        manifest, environ={"OPENAI_API_KEY": "sk-stub", "TWILIO_STREAM_TOKEN_SECRET": "t"}
    )

    assert plan.defects
    assert all(not reason.startswith("incomplete") for reason in plan.blocking_errors())


def test_plan_issues_attribute_gaps_to_pipeline_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _absent(monkeypatch, "aiortc", "livekit")
    manifest = parse_manifest(
        {
            "project": {"name": "sel"},
            "voice": {"default": {"transport": "webrtc", "stt": "deepgram", "tts": "openai"}},
        }
    )

    plan = build_manifest_plan(manifest, environ={"OPENAI_API_KEY": "sk-stub"})
    issues = plan_issues(plan)
    by_field = {issue.field: issue for issue in issues}

    assert by_field["DEEPGRAM_API_KEY"].code == "EASYCAT_E203"
    assert by_field["DEEPGRAM_API_KEY"].role == "stt"
    assert by_field["DEEPGRAM_API_KEY"].severity == "blocking"
    assert by_field["webrtc"].code == "EASYCAT_E202"
    assert by_field["webrtc"].role == "transport"
    assert by_field["webrtc"].severity == "blocking"
    # A gracefully-degrading extra is reported, but only as a warning.
    assert by_field["aec"].severity == "warning"
    assert by_field["aec"].role == "echo_canceller"
    # Blocking issues sort ahead of warnings.
    assert [issue.severity for issue in issues] == sorted(issue.severity for issue in issues)


def test_plan_issues_dedupe_a_shared_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = parse_manifest(
        {
            "project": {"name": "sel"},
            "voice": {"default": {"transport": "websocket", "stt": "openai", "tts": "openai"}},
        }
    )

    issues = plan_issues(build_manifest_plan(manifest, environ={}))

    openai_issues = [issue for issue in issues if issue.field == "OPENAI_API_KEY"]
    assert len(openai_issues) == 1
    assert openai_issues[0].role == "stt"


def test_load_selected_profile_reports_a_missing_manifest(tmp_path: Path) -> None:
    with pytest.raises(EasyCatError) as raised:
        load_selected_profile(tmp_path / "nope.toml", profile="default")

    assert raised.value.code == "EASYCAT_E601"


def test_load_selected_profile_reports_an_unknown_profile(tmp_path: Path) -> None:
    path = _manifest('[voice.default]\ntransport = "webrtc"', tmp_path)

    with pytest.raises(EasyCatError) as raised:
        load_selected_profile(path, profile="nope")

    assert raised.value.code == "EASYCAT_E602"
    assert "available: default" in raised.value.message


def test_plan_selected_profile_maps_an_unknown_backend(tmp_path: Path) -> None:
    path = _manifest('[voice.default]\ntransport = "webrtc"\nvad = "silro"', tmp_path)
    _manifest_obj, voice_profile = load_selected_profile(path, profile="default")

    with pytest.raises(EasyCatError) as raised:
        plan_selected_profile(voice_profile, profile="default", environ={})

    assert raised.value.code == "EASYCAT_E602"
    assert raised.value.context["path"] == "[voice.default]"


def test_plan_selected_profile_passes_an_unknown_provider_through(tmp_path: Path) -> None:
    path = _manifest('[voice.default]\ntransport = "webrtc"\nstt = "opnai"', tmp_path)
    _manifest_obj, voice_profile = load_selected_profile(path, profile="default")

    with pytest.raises(EasyCatError) as raised:
        plan_selected_profile(voice_profile, profile="default", environ={})

    assert raised.value.code == "EASYCAT_E104"
