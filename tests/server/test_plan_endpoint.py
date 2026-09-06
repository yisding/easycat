"""M6b ``/plan`` endpoint + parity-gated ``/health/ready`` readiness wiring.

Covers: the read-only ``/plan`` route (manifest-backed and factory-only), the
M6b ``/health/ready`` manifest-loaded + plan-no-blocking-errors checks (503 when
the plan has blocking errors), the factory-only server keeping the M4
``"skipped"`` placeholders, and the M4 boundary that ``health.py`` never imports
the planner at module load.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

try:
    import aiohttp
except ModuleNotFoundError:  # pragma: no cover - optional extra
    aiohttp = None  # type: ignore[assignment]

from easycat.server import VoiceServer, VoiceServerConfig

_HAS_AIOHTTP = aiohttp is not None

#: Only the tests that actually drive an HTTP client need the extra. The
#: ``plan_payload`` / ``capabilities_payload`` rows call plain sync methods on a
#: ``VoiceServer``, so they MUST run in the credential-free lane — they are the
#: only executing coverage of the server's copy of the per-role selection
#: projection (``easycat.planning.selection_to_dict``).
_requires_aiohttp = pytest.mark.skipif(not _HAS_AIOHTTP, reason="aiohttp not installed")

# A realistic secret-shaped token (``sk-...``, 24+ chars) for the token-leak
# assertions so they exercise ``redact_value``'s value-policy safety net, not
# just the structural guarantee that the resolved token is never stored.
_RESOLVED_TOKEN = "sk-live-secret-token-abcdef1234567890"


class _FakeSession:
    async def start(self) -> None:
        pass

    async def stop(self, *, force: bool = False) -> None:
        pass


def _write_manifest(
    tmp_path: Path,
    *,
    stt: str = "openai/realtime",
    vad: str = "silero",
    transport: str = "webrtc",
) -> Path:
    manifest = tmp_path / "easycat.toml"
    manifest.write_text(
        "\n".join(
            [
                "[project]",
                'name = "plan-endpoint-test"',
                "",
                "[server]",
                'host = "127.0.0.1"',
                "port = 0",
                'auth = "bearer-env:EASYCAT_SERVE_TOKEN"',
                "",
                "[voice.default]",
                f'transport = "{transport}"',
                f'stt = "{stt}"',
                'tts = "openai"',
                f'vad = "{vad}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return manifest


@pytest.fixture
async def client() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


def _base_url(server: VoiceServer) -> str:
    address = server.http_address
    assert address is not None
    host, port = address
    return f"http://{host}:{port}"


# ── /plan endpoint ───────────────────────────────────────────────────


def test_health_module_does_not_import_planner() -> None:
    # The M4 boundary: importing health.py must NOT pull the planner. The planner
    # is imported lazily inside VoiceServer.health()/plan_payload(), never here.
    import subprocess
    import sys

    code = "import sys; import easycat.server.health; print('easycat.planning' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False", result.stdout


def test_plan_payload_factory_only_server_is_empty() -> None:
    server = VoiceServer(
        VoiceServerConfig(host="127.0.0.1", port=0),
        session_factory=lambda _t: _FakeSession(),
    )
    payload = server.plan_payload()
    assert payload["selected"] == {}
    assert payload["has_blocking_errors"] is False


def test_plan_payload_from_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", _RESOLVED_TOKEN)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.setattr(
        "easycat.planning._resolution._default_module_available", lambda _name: True
    )
    server = VoiceServer.from_manifest(_write_manifest(tmp_path))
    payload = server.plan_payload()
    assert set(payload["selected"]) == {
        "stt",
        "tts",
        "vad",
        "transport",
        "agent",
        "noise_reducer",
        "echo_canceller",
    }
    assert payload["selected"]["transport"]["provider"] == "webrtc"
    assert payload["has_blocking_errors"] is False
    # The server builds each role dict through ``easycat.planning``'s shared
    # projection, so its keys are exactly the ones ``easycat plan --json``
    # publishes (pinned in ``tests/planning/test_provider_plan.py``).
    for role_payload in payload["selected"].values():
        assert set(role_payload) == {
            "role",
            "provider",
            "model",
            "config_type",
            "extra",
            "required_env",
            "capabilities",
        }
    # No resolved token appears anywhere in the payload.
    import json

    assert _RESOLVED_TOKEN not in json.dumps(payload)


@_requires_aiohttp
@pytest.mark.integration_socket
async def test_plan_route_returns_200(
    client: aiohttp.ClientSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", _RESOLVED_TOKEN)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    server = VoiceServer.from_manifest(_write_manifest(tmp_path))
    # Override host/port to ephemeral loopback so the test binds safely.
    server.config.port = 0
    await server.start()
    try:
        async with client.get(f"{_base_url(server)}/plan") as resp:
            assert resp.status == 401

        auth_headers = {"Authorization": f"Bearer {_RESOLVED_TOKEN}"}
        async with client.get(f"{_base_url(server)}/plan", headers=auth_headers) as resp:
            assert resp.status == 200
            body = await resp.json()
        assert body["profile"] == "default"
        assert "stt" in body["selected"]
        assert _RESOLVED_TOKEN not in await _text_of(
            client, f"{_base_url(server)}/plan", headers=auth_headers
        )
    finally:
        await server.stop()


async def _text_of(
    client: aiohttp.ClientSession, url: str, *, headers: dict[str, str] | None = None
) -> str:
    async with client.get(url, headers=headers) as resp:
        return await resp.text()


@_requires_aiohttp
@pytest.mark.integration_socket
async def test_plan_route_factory_only_returns_empty(
    client: aiohttp.ClientSession,
) -> None:
    server = VoiceServer(
        VoiceServerConfig(host="127.0.0.1", port=0),
        session_factory=lambda _t: _FakeSession(),
    )
    await server.start()
    try:
        async with client.get(f"{_base_url(server)}/plan") as resp:
            assert resp.status == 200
            body = await resp.json()
        assert body["selected"] == {}
    finally:
        await server.stop()


# ── /health/ready M6b wiring ─────────────────────────────────────────


@_requires_aiohttp
@pytest.mark.integration_socket
async def test_ready_200_when_manifest_plan_clean(
    client: aiohttp.ClientSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", "tok")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.setattr(
        "easycat.planning._resolution._default_module_available", lambda _name: True
    )
    server = VoiceServer.from_manifest(_write_manifest(tmp_path))
    server.config.port = 0
    await server.start()
    try:
        async with client.get(f"{_base_url(server)}/health/ready") as resp:
            assert resp.status == 200
        # The /health payload now reports live manifest/providers sub-checks.
        async with client.get(f"{_base_url(server)}/health") as resp:
            health = await resp.json()
        assert health["checks"]["manifest"] == {"status": "ok"}
        assert health["checks"]["providers"] == {"status": "ok"}
    finally:
        await server.stop()


@_requires_aiohttp
@pytest.mark.integration_socket
async def test_ready_503_when_plan_has_blocking_errors(
    client: aiohttp.ClientSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", "tok")
    # deepgram STT but NO DEEPGRAM_API_KEY -> the plan has a blocking error.
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    server = VoiceServer.from_manifest(_write_manifest(tmp_path, stt="deepgram"))
    server.config.port = 0
    await server.start()
    try:
        async with client.get(f"{_base_url(server)}/health/ready") as resp:
            assert resp.status == 503
            body = await resp.json()
            assert "plan_has_blocking_errors" in body["reasons"]
        async with client.get(f"{_base_url(server)}/health") as resp:
            health = await resp.json()
        assert health["checks"]["providers"] == {"status": "degraded"}
    finally:
        await server.stop()


def test_ready_is_ready_for_a_native_endpointing_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DX1-D1: readiness must not block on a VAD the session never builds.

    ``deepgram/flux-general-en`` declares ``native_endpointing``, so
    ``create_session`` drives turns from STT FINAL events and constructs no VAD
    at all. Before the fix ``/health/ready`` reported ``plan_has_blocking_errors``
    on the absent ``silero-vad`` extra for a deployment that starts fine.

    Uses a ``websocket`` transport so the profile itself pulls no install
    extra, and forces the VAD probe modules absent rather than depending on the
    checkout: a maintainer who ran ``uv sync --extra silero-vad`` has
    ``onnxruntime`` importable, and an ambient probe would make the
    ``deepgram/nova-2`` control's "extra is still missing" assertion fail there.
    The control proves the ``off`` verdict is not vacuous — the same extra IS
    blocking for a profile that does build a VAD.
    """
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", _RESOLVED_TOKEN)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-stub")
    monkeypatch.setattr(
        "easycat.planning._resolution._default_module_available",
        lambda name: name not in {"onnxruntime", "ten_vad", "krisp_audio"},
    )

    control_dir = tmp_path / "control"
    control_dir.mkdir()
    native_dir = tmp_path / "native"
    native_dir.mkdir()

    control = VoiceServer.from_manifest(
        _write_manifest(control_dir, stt="deepgram/nova-2", transport="websocket")
    ).plan_payload()
    assert control["selected"]["vad"]["provider"] == "silero"
    assert "missing_extra:silero-vad" in control["blocking_errors"]
    assert control["has_blocking_errors"] is True

    native = VoiceServer.from_manifest(
        _write_manifest(native_dir, stt="deepgram/flux-general-en", transport="websocket")
    )
    payload = native.plan_payload()
    assert payload["selected"]["vad"]["provider"] == "off"
    assert payload["selected"]["vad"]["capabilities"] == ["disabled"]
    assert payload["missing_extras"] == []
    assert payload["has_blocking_errors"] is False
    # The tuple ``VoiceServerHealth`` turns into the 200/503 verdict
    # (``server/health.py`` reads ``plan_has_blocking_errors``). Asserted
    # directly because the HTTP peers in this file need aiohttp, which the dev
    # group does not install.
    assert native._manifest_readiness() == (True, ())


def test_plan_payload_unresolvable_backend_returns_blocking_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An unknown vad backend makes the planner RAISE. ``plan_payload`` must
    # surface that as a structured plan-with-blocking-errors, never propagate
    # (the /plan route would otherwise 500 the diagnostic endpoint).
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", _RESOLVED_TOKEN)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    server = VoiceServer.from_manifest(_write_manifest(tmp_path, vad="silro"))
    payload = server.plan_payload()
    assert payload["has_blocking_errors"] is True
    assert payload["selected"] == {}
    assert payload["manifest_loaded"] is True
    assert any("plan_unresolvable" in err for err in payload["blocking_errors"])


def test_capabilities_payload_unresolvable_backend_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", _RESOLVED_TOKEN)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    server = VoiceServer.from_manifest(_write_manifest(tmp_path, vad="silro"))
    caps = server.capabilities_payload()
    assert caps["roles"] == {}
    assert caps["all_capabilities"] == []


@_requires_aiohttp
@pytest.mark.integration_socket
async def test_plan_and_capabilities_endpoints_200_when_unresolvable(
    client: aiohttp.ClientSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: an unknown vad backend makes the planner RAISE; /plan and
    # /capabilities must return 200 with structured data, NOT 500.
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", "tok")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    server = VoiceServer.from_manifest(_write_manifest(tmp_path, vad="silro"))
    server.config.port = 0
    await server.start()
    try:
        auth_headers = {"Authorization": "Bearer tok"}
        async with client.get(f"{_base_url(server)}/plan", headers=auth_headers) as resp:
            assert resp.status == 200
            body = await resp.json()
            assert body["has_blocking_errors"] is True
            assert body["selected"] == {}
        async with client.get(f"{_base_url(server)}/capabilities", headers=auth_headers) as resp:
            assert resp.status == 200
            caps = await resp.json()
            assert caps["roles"] == {}
    finally:
        await server.stop()


@_requires_aiohttp
@pytest.mark.integration_socket
async def test_ready_503_when_manifest_plan_unresolvable(
    client: aiohttp.ClientSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: an unresolvable profile (here a typo'd STT shortcut) makes the
    # planner RAISE. A readiness probe must report a structured not-ready
    # response, NOT a 500 — a raised health check breaks k8s probes outright.
    monkeypatch.setenv("EASYCAT_SERVE_TOKEN", "tok")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    server = VoiceServer.from_manifest(_write_manifest(tmp_path, stt="opnai"))
    server.config.port = 0
    await server.start()
    try:
        async with client.get(f"{_base_url(server)}/health/ready") as resp:
            assert resp.status == 503
            body = await resp.json()
            assert "plan_has_blocking_errors" in body["reasons"]
        # /health degrades the providers sub-check rather than 500-ing.
        async with client.get(f"{_base_url(server)}/health") as resp:
            assert resp.status == 200
            health = await resp.json()
        assert health["checks"]["manifest"] == {"status": "ok"}
        assert health["checks"]["providers"] == {"status": "degraded"}
    finally:
        await server.stop()


@_requires_aiohttp
@pytest.mark.integration_socket
async def test_factory_only_keeps_m4_skipped_placeholders(
    client: aiohttp.ClientSession,
) -> None:
    server = VoiceServer(
        VoiceServerConfig(host="127.0.0.1", port=0),
        session_factory=lambda _t: _FakeSession(),
    )
    await server.start()
    try:
        async with client.get(f"{_base_url(server)}/health") as resp:
            health = await resp.json()
        # A factory-only server keeps the M4 placeholders (M6b not evaluated).
        assert health["checks"]["manifest"] == {"status": "skipped"}
        assert health["checks"]["providers"] == {"status": "skipped"}
        async with client.get(f"{_base_url(server)}/health/ready") as resp:
            assert resp.status == 200
    finally:
        await server.stop()
