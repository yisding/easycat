from __future__ import annotations

import pytest

pytest.importorskip("aiohttp")

import json
import zipfile

from easycat.debugger.server import _bundle_source, _make_app

from ._server_helpers import _build_voice_bundle


async def test_origin_guard_refuses_cross_origin_requests(tmp_path):
    """By default, the origin guard middleware blocks non-loopback
    Origin headers so a malicious page on the local machine can't talk
    to the debugger via CSRF."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)  # allow_remote=False (default)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/api/manifest",
            headers={"Origin": "https://evil.example.com"},
        )
        assert resp.status == 403


async def test_origin_guard_allows_loopback_origin(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/api/manifest",
            headers={
                "Host": "localhost:8765",
                "Origin": "http://localhost:8765",
            },
        )
        assert resp.status == 200


async def test_origin_guard_refuses_different_loopback_port(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/api/manifest",
            headers={
                "Host": "localhost:8765",
                "Origin": "http://localhost:3000",
            },
        )
        assert resp.status == 403


async def test_origin_guard_refuses_different_loopback_hostname_on_post(tmp_path):
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/replay",
            json={"fidelity": "artifact"},
            headers={
                "Host": "127.0.0.1:8765",
                "Origin": "http://localhost:8765",
            },
        )
        assert resp.status == 403


async def test_origin_guard_refuses_dns_rebinding_origin_prefix(tmp_path):
    """Loopback-looking origin prefixes must not match attacker-owned hosts."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/api/manifest",
            headers={"Origin": "http://localhost.attacker.example:8765"},
        )
        assert resp.status == 403


async def test_origin_guard_refuses_dns_rebinding_host_header(tmp_path):
    """Requests addressed to attacker-controlled hostnames are rejected."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/api/manifest",
            headers={"Host": "localhost.attacker.example:8765"},
        )
        assert resp.status == 403


async def test_manifest_strips_filesystem_path(tmp_path):
    """``manifest`` should expose only the bundle's basename, never the
    full filesystem path — bundles often live under user-named dirs."""
    bundle_path = tmp_path / "a-secret-folder" / "user-bundle.zip"
    bundle_path.parent.mkdir()
    inner = await _build_voice_bundle(tmp_path)
    bundle_path.write_bytes(inner.read_bytes())

    source = _bundle_source(bundle_path)
    manifest = source.manifest()
    assert manifest["name"] == "user-bundle.zip"
    assert "a-secret-folder" not in json.dumps(manifest)
    assert "path" not in manifest


def test_check_host_refuses_non_loopback_without_opt_in():
    from easycat.debugger.server import _check_host

    with pytest.raises(RuntimeError, match="non-loopback"):
        _check_host("0.0.0.0", allow_remote=False)
    # Loopback always passes.
    _check_host("127.0.0.1", allow_remote=False)
    _check_host("::1", allow_remote=False)
    _check_host("localhost", allow_remote=False)
    # Explicit opt-in passes.
    _check_host("0.0.0.0", allow_remote=True)


def test_serve_bundle_refuses_non_loopback_without_allow_remote(tmp_path):
    """Public entry point should fail loud rather than silently exposing
    the journal to the network."""
    import easycat.debugger.server as srv

    bundle_path = tmp_path / "x.zip"
    with zipfile.ZipFile(bundle_path, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"format_version": 1}))
        zf.writestr("journal.ndjson", b"")

    with pytest.raises(RuntimeError, match="non-loopback"):
        srv.serve_bundle(bundle_path, host="0.0.0.0", open_browser=False)


async def test_origin_guard_refuses_missing_origin_on_post(tmp_path):
    """A POST with no Origin header is suspicious — block it.  Browsers
    always send Origin on cross-origin POSTs; absence usually means
    a simple-form-POST CSRF or an attacker bypassing CORS."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/replay",
            json={"fidelity": "artifact"},
            # No Origin header at all.
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 403


async def test_origin_guard_refuses_form_encoded_post(tmp_path):
    """State-changing requests must use application/json — a form POST
    sneaks past CORS preflight and could enable simple-form CSRF."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/replay",
            data="fidelity=artifact",
            headers={
                "Host": "localhost:8765",
                "Origin": "http://localhost:8765",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        assert resp.status == 415


async def test_origin_guard_blocks_cross_site_fetch(tmp_path):
    """``Sec-Fetch-Site: cross-site`` is blocked even if Origin happens
    to match a loopback prefix."""
    bundle_path = await _build_voice_bundle(tmp_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/api/manifest",
            headers={
                "Origin": "http://localhost:8765",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        assert resp.status == 403
