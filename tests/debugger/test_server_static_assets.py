from __future__ import annotations

import pytest

pytest.importorskip("aiohttp")

import pathlib


def test_static_index_blocks_protocol_relative_urls():
    """Round-3 follow-up: ``_sanitiseUrl`` in the SPA must reject
    ``//evil.com`` (protocol-relative cross-origin)."""
    static_path = (
        pathlib.Path(__file__).resolve().parent.parent.parent
        / "src/easycat/debugger/static/index.html"
    )
    text = static_path.read_text(encoding="utf-8")
    assert r"if (/^\/\//.test(trimmed)) return" in text, (
        "_sanitiseUrl missing protocol-relative reject"
    )
    # Single-leading-slash same-origin path remains allowed via the
    # ``\\/[^/]`` pattern in the safe-scheme regex.
    assert r"\/[^/]" in text


def test_static_index_force_destructive_check():
    """Round-3 follow-up: the JS ``isDestructive`` check must include
    ``force`` so a future force toggle can't bypass the confirm dialog."""
    static_path = (
        pathlib.Path(__file__).resolve().parent.parent.parent
        / "src/easycat/debugger/static/index.html"
    )
    text = static_path.read_text(encoding="utf-8")
    assert "isDestructive" in text
    assert "|| force" in text


def test_static_index_has_issues_tab_without_innerhtml():
    """The Issues tab must exist and be built with safe DOM helpers only.

    ``renderIssuesView`` fetches ``/api/issues`` and renders severity cards;
    it must never reach for ``innerHTML``/``outerHTML``/``insertAdjacentHTML``
    so untrusted bundle content can't inject markup.
    """
    static_path = (
        pathlib.Path(__file__).resolve().parent.parent.parent
        / "src/easycat/debugger/static/index.html"
    )
    text = static_path.read_text(encoding="utf-8")
    assert 'data-tab="issues"' in text, "Issues tab missing from the tab strip"
    assert 'id="issues-view"' in text, "Issues view section missing"
    assert "function renderIssuesView" in text
    assert "issues: renderIssuesView" in text, "Issues tab not wired into TAB_LOADERS"
    assert '"/api/issues"' in text, "renderIssuesView must fetch /api/issues"
    # Guard against actual unsafe DOM usage (the word may appear in comments
    # warning against it, so match assignment/call syntax, not bare mentions).
    for forbidden in (".innerHTML", ".outerHTML", ".insertAdjacentHTML"):
        assert forbidden not in text, f"SPA must never use {forbidden}"


def test_static_index_pipeline_includes_turn_stage():
    """Round-4 follow-up: the SVG pipeline graph must enumerate all 8
    stages, including ``turn`` (SmartTurn endpointing).  Previously the
    array silently omitted it."""
    static_path = (
        pathlib.Path(__file__).resolve().parent.parent.parent
        / "src/easycat/debugger/static/index.html"
    )
    text = static_path.read_text(encoding="utf-8")
    assert 'id: "turn"' in text and 'label: "Turn"' in text, (
        "PIPELINE_STAGES missing the turn (SmartTurn) node"
    )


def test_static_index_has_shareable_deeplink_hash():
    """The SPA must encode tab/turn/seq into the URL hash and decode it back.

    A copied URL should reopen on the same record.  The decoder validates a
    turn id against the SAME ``[A-Za-z0-9_\\-]{1,128}`` regex the server
    enforces (server.py ``_TURN_ID_OK``), writes the hash via
    ``history.replaceState``/``location.hash`` only, and guards its own writes
    against the ``hashchange`` listener so there is no feedback loop.  Like the
    rest of the SPA it must never reach for innerHTML-family DOM.
    """
    static_path = (
        pathlib.Path(__file__).resolve().parent.parent.parent
        / "src/easycat/debugger/static/index.html"
    )
    text = static_path.read_text(encoding="utf-8")
    assert "function _serializeHash" in text
    assert "function _applyHashToState" in text
    assert "history.replaceState" in text
    assert "location.hash" in text
    assert 'window.addEventListener("hashchange"' in text
    # The decoder mirrors the server's turn-id validation regex verbatim.
    assert r"/^[A-Za-z0-9_\-]{1,128}$/" in text
    # The feedback-loop guard must be present and consulted before writing.
    assert "_suppressHashWrite" in text
    assert "if (_suppressHashWrite) return" in text
    for forbidden in (".innerHTML", ".outerHTML", ".insertAdjacentHTML"):
        assert forbidden not in text, f"SPA must never use {forbidden}"


def test_static_index_has_copy_replay_command_button():
    """The Timeline turn actions must offer a copyable ``easycat replay``
    command scoped to the turn, built with safe DOM helpers and copied via the
    clipboard (with an ``alert`` fallback) — never injected as markup."""
    static_path = (
        pathlib.Path(__file__).resolve().parent.parent.parent
        / "src/easycat/debugger/static/index.html"
    )
    text = static_path.read_text(encoding="utf-8")
    assert "Copy replay cmd" in text
    assert "function _copyReplayCommand" in text
    assert "easycat replay" in text
    assert "--turn" in text
    assert "navigator.clipboard" in text


def test_static_index_has_save_test_case_button():
    """The Timeline and Transcript views must offer a "Save as test case"
    button that POSTs to ``/api/export?turn=`` via ``_saveTestCase``, built
    with safe DOM helpers only (no innerHTML family)."""
    static_path = (
        pathlib.Path(__file__).resolve().parent.parent.parent
        / "src/easycat/debugger/static/index.html"
    )
    text = static_path.read_text(encoding="utf-8")
    assert "Save as test case" in text
    assert "function _saveTestCase" in text
    assert "/api/export?turn=" in text
    assert "encodeURIComponent(turnId)" in text
    for forbidden in (".innerHTML", ".outerHTML", ".insertAdjacentHTML"):
        assert forbidden not in text, f"SPA must never use {forbidden}"


def _static_dir() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent.parent / "src/easycat/debugger/static"


def test_waveform_js_exposes_namespace_without_unsafe_dom():
    """``waveform.js`` must expose the ``EasyCatWaveform`` namespace and build
    its DOM with safe helpers only (no innerHTML family)."""
    text = (_static_dir() / "waveform.js").read_text(encoding="utf-8")
    assert "EasyCatWaveform" in text
    assert "renderTurnWaveform" in text
    assert "renderLiveWaveform" in text
    for forbidden in (".innerHTML", ".outerHTML", ".insertAdjacentHTML"):
        assert forbidden not in text, f"waveform.js must never use {forbidden}"


def test_index_html_loads_waveform_script():
    """The SPA must load ``/static/waveform.js`` so the namespace is present
    before the inline script wires the Timeline/Live strips."""
    text = (_static_dir() / "index.html").read_text(encoding="utf-8")
    assert '<script src="/static/waveform.js"></script>' in text
    # The strips are wired through the global namespace from the inline script.
    assert "window.EasyCatWaveform" in text


async def test_static_waveform_js_served_with_correct_content_type(tmp_path):
    """The static route serves ``waveform.js`` as JavaScript."""
    from easycat.debug.bundle import RunBundle
    from easycat.debugger.server import _bundle_source, _make_app

    from ._server_helpers import _build_voice_bundle

    bundle_path = await _build_voice_bundle(tmp_path)
    RunBundle.load(bundle_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)

    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/static/waveform.js")
        assert resp.status == 200
        ctype = resp.headers["Content-Type"]
        assert "javascript" in ctype or "ecmascript" in ctype, ctype
        body = await resp.text()
        assert "EasyCatWaveform" in body
