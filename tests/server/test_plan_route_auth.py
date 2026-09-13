"""Authorization regression tests for the ``/plan`` route handler."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from easycat.planning import provider_plan
from easycat.planning.selection import build_manifest_plan, plan_body
from easycat.server import BearerTokenAuth, VoiceServer, VoiceServerConfig
from easycat.server.routes import register_plan_route


class _FakeWeb(ModuleType):
    @staticmethod
    def json_response(payload: dict[str, Any], *, status: int = 200) -> SimpleNamespace:
        return SimpleNamespace(payload=payload, status=status)


class _FakeRouter:
    def __init__(self) -> None:
        self.handler: Any | None = None

    def add_get(self, path: str, handler: Any) -> None:
        assert path == "/plan"
        self.handler = handler


class _FakeApp:
    def __init__(self) -> None:
        self.router = _FakeRouter()


class _FakeSession:
    async def start(self) -> None:
        pass

    async def stop(self, *, force: bool = False) -> None:
        pass


class _Request:
    def __init__(self, authorization: str | None = None) -> None:
        self.headers = {}
        if authorization is not None:
            self.headers["Authorization"] = authorization
        self.query = {}


def _install_fake_aiohttp(monkeypatch: pytest.MonkeyPatch) -> None:
    aiohttp = ModuleType("aiohttp")
    aiohttp.web = _FakeWeb("aiohttp.web")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "aiohttp", aiohttp)
    monkeypatch.setitem(sys.modules, "aiohttp.web", aiohttp.web)


def _plan_handler() -> Any:
    server = VoiceServer(
        VoiceServerConfig(
            host="127.0.0.1",
            port=0,
            auth=BearerTokenAuth("secret-token"),
        ),
        session_factory=lambda _transport: _FakeSession(),
    )
    app = _FakeApp()
    register_plan_route(app, server)
    assert app.router.handler is not None
    return app.router.handler


@pytest.mark.asyncio
async def test_plan_route_rejects_missing_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_aiohttp(monkeypatch)
    response = await _plan_handler()(_Request())
    assert response.status == 401
    assert response.payload == {"error": "Missing or invalid bearer token"}


@pytest.mark.asyncio
async def test_plan_route_allows_valid_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_aiohttp(monkeypatch)
    response = await _plan_handler()(_Request("Bearer secret-token"))
    assert response.status == 200
    assert response.payload["selected"] == {}


# ── DX2 PR2: the coded ``issues`` array, without aiohttp ──────────────
#
# ``tests/server/test_plan_endpoint.py`` is skipped whole when ``aiohttp`` is
# absent, and ``aiohttp`` is in the telephony/webrtc/debugger extras, never in
# the dev group. The coded and readiness assertions therefore live HERE, where
# they actually run in the credential-free lane; that module keeps the HTTP
# status-code twins.

_SECRET_SHAPED = "sk-live-secret-token-abcdef1234567890"


def _write_manifest(
    tmp_path: Path,
    *,
    stt: str = "openai/realtime",
    vad: str = "silero",
    transport: str = "webrtc",
    server_auth: str | None = 'auth = "bearer-env:EASYCAT_SERVE_TOKEN"',
    token: str | None = None,
) -> Path:
    lines = [
        "[project]",
        'name = "plan-auth-test"',
        "",
        "[server]",
        'host = "127.0.0.1"',
        "port = 0",
    ]
    if server_auth is not None:
        lines.append(server_auth)
    lines += [
        "",
        "[voice.default]",
        f'transport = "{transport}"',
        f'stt = "{stt}"',
        f'vad = "{vad}"',
    ]
    if token is not None:
        lines.append(f'token = "{token}"')
    manifest = tmp_path / "easycat.toml"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest


def _extras_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report every probe module present at the planner's single seam.

    Without this a phone/webrtc profile would go red for a missing extra in a
    dev-group environment, and the readiness assertions would pass for the
    wrong reason.
    """
    monkeypatch.setattr(provider_plan, "_module_available", lambda _module: True)


def test_plan_payload_shares_the_cli_body_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A-1: the server body IS the CLI body plus two server-only keys."""
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", _SECRET_SHAPED)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    _extras_present(monkeypatch)
    manifest_path = _write_manifest(tmp_path)
    server = VoiceServer.from_manifest(manifest_path)

    from easycat.project import load_manifest

    plan = build_manifest_plan(load_manifest(manifest_path), profile="default")

    assert set(server.plan_payload()) == set(plan_body(plan)) | {"manifest_loaded", "issues"}


def test_plan_payload_selected_matches_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A-2: one ``ProviderSelection`` -> JSON shape, not two."""
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", _SECRET_SHAPED)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    _extras_present(monkeypatch)
    manifest_path = _write_manifest(tmp_path)
    server = VoiceServer.from_manifest(manifest_path)

    from typer.testing import CliRunner

    from easycat.cli._app import _register_commands, app

    _register_commands()
    payload = json.loads(
        CliRunner().invoke(app, ["plan", "--manifest", str(manifest_path), "--json"]).stdout
    )

    assert payload["selected"] == server.plan_payload()["selected"]
    assert payload["issues"] == server.plan_payload()["issues"]


def test_plan_payload_unresolvable_keeps_the_legacy_reason_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A-3: the wire-compat guard — prefix preserved, code additive."""
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", _SECRET_SHAPED)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    server = VoiceServer.from_manifest(_write_manifest(tmp_path, vad="silro"))

    payload = server.plan_payload()

    assert payload["blocking_errors"][0].startswith("plan_unresolvable: ")
    assert "silro" in payload["blocking_errors"][0]
    assert payload["has_blocking_errors"] is True
    assert payload["selected"] == {}
    assert payload["manifest_loaded"] is True
    assert payload["issues"][0]["code"] == "EASYCAT_E602"
    assert payload["issues"][0]["reason"] == "unresolvable_profile"


def test_capabilities_payload_reports_the_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A-4: ``/capabilities`` stops dropping the reason it already knows."""
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", _SECRET_SHAPED)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    server = VoiceServer.from_manifest(_write_manifest(tmp_path, stt="opnai"))

    caps = server.capabilities_payload()

    assert caps["roles"] == {}
    assert caps["all_capabilities"] == []
    assert caps["issues"][0]["code"] == "EASYCAT_E104"


def test_plan_payload_never_contains_a_resolved_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A-5: the planner reads names, never values."""
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", _SECRET_SHAPED)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    _extras_present(monkeypatch)
    server = VoiceServer.from_manifest(_write_manifest(tmp_path))

    assert _SECRET_SHAPED not in json.dumps(server.plan_payload())


@pytest.mark.parametrize("field", ["vad", "stt"])
def test_plan_payload_issue_text_is_redacted(
    field: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A-6: both ``selection_error`` branches redact before reaching a body.

    ``vad`` exercises the planner's bare ``ValueError``; ``stt`` exercises the
    ``EASYCAT_E104`` PASS-THROUGH branch, whose message interpolates the raw
    manifest value. Case ``stt`` is the one that fails if ``_resolve_profile_plan``
    reaches for ``SetupIssue.from_error(selection_error(...))``.
    """
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", "tok")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    kwargs = {field: _SECRET_SHAPED}
    server = VoiceServer.from_manifest(_write_manifest(tmp_path, **kwargs))  # type: ignore[arg-type]

    plan_dump = json.dumps(server.plan_payload())
    caps_dump = json.dumps(server.capabilities_payload())

    assert _SECRET_SHAPED not in plan_dump
    assert "[REDACTED_SECRET]" in plan_dump
    assert _SECRET_SHAPED not in caps_dump
    assert "[REDACTED_SECRET]" in caps_dump


def test_unset_server_auth_does_not_block_the_profile_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A-7: a server-scope gap is reported, never blocking.

    ``VoiceServer.from_manifest`` refuses to construct when ``[server] auth``
    points at an unset var, so this builds the plan directly — the same call
    ``plan_payload`` makes — and asserts the severity scoping that keeps
    ``tests/cli/test_json_schema.py::test_plan_envelope`` green.
    """
    monkeypatch.delenv("SRV_TOK", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    _extras_present(monkeypatch)
    manifest_path = _write_manifest(tmp_path, server_auth='auth = "bearer-env:SRV_TOK"')

    from easycat.project import load_manifest

    plan = build_manifest_plan(load_manifest(manifest_path), profile="default")
    body = plan_body(plan)

    assert body["has_blocking_errors"] is False
    assert not [reason for reason in body["blocking_errors"] if "SRV_TOK" in reason]
    from easycat.planning.selection import plan_issues

    issue = next(i for i in plan_issues(plan) if i.field == "SRV_TOK")
    assert issue.code == "EASYCAT_E604"
    assert issue.severity == "warning"


def test_ready_is_red_when_a_phone_profile_has_no_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V-1: the flagship correctness fix, proven without aiohttp.

    A ``twilio`` profile with no ``token`` reports READY today and then raises
    ``EASYCAT_E602`` on the FIRST connection. Extras are faked present so the
    redness is the token, not a missing install extra.
    """
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", "tok")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    _extras_present(monkeypatch)
    server = VoiceServer.from_manifest(
        _write_manifest(tmp_path, transport="twilio", stt="openai/realtime")
    )

    health = asyncio.run(server.health())

    assert health.is_ready() is False
    assert "plan_has_blocking_errors" in health.readiness_failures()
    assert health.plan_blocking_errors is not None
    assert "incomplete_selection:[voice.default]" in health.plan_blocking_errors
    assert not [r for r in health.plan_blocking_errors if r.startswith("missing_extra:")]
