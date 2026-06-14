from __future__ import annotations

import pytest

pytest.importorskip("aiohttp")

import pathlib


def test_static_index_has_first_class_overview_and_record_pager():
    static_path = (
        pathlib.Path(__file__).resolve().parent.parent.parent
        / "src/easycat/debugger/static/index.html"
    )
    text = static_path.read_text(encoding="utf-8")
    assert 'data-tab="overview"' in text
    assert 'id="overview-view"' in text
    assert "function renderOverviewView()" in text
    assert "Recommended next steps" in text
    assert "Slowest turns" in text
    assert 'id="records-prev-btn"' in text
    assert 'id="records-next-btn"' in text
    assert 'id="records-tail-btn"' in text
    assert "function selectRecordBySeq(seq)" in text
    # The boot decodes the URL deep-link hash first and falls back to the
    # Overview dashboard as the default tab when no hash is present (WP13).
    assert '_applyHashToState() || "overview"' in text


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


def test_static_index_has_annotation_controls_in_parity_with_python():
    """The transcript view must build per-turn reviewer-verdict controls and
    POST them to ``/api/annotate`` with safe DOM helpers only.

    The hard-coded JS failure-type list must match
    ``debug/annotations.FAILURE_TYPES`` exactly (single-source parity), and
    the controls must be gated on ``supports_annotate`` so live sessions
    never render them.
    """
    from easycat.debug.annotations import FAILURE_TYPES

    static_path = (
        pathlib.Path(__file__).resolve().parent.parent.parent
        / "src/easycat/debugger/static/index.html"
    )
    text = static_path.read_text(encoding="utf-8")
    assert "function _annotationRow" in text
    assert '"/api/annotate"' in text
    assert '"/api/annotations"' in text
    assert "supports_annotate" in text, "annotation controls must be gated on supports_annotate"
    # Every Python failure type appears verbatim in the SPA's hard-coded list.
    for failure_type in FAILURE_TYPES:
        assert f'"{failure_type}"' in text, f"SPA missing failure type {failure_type!r}"
    # No unsafe DOM anywhere in the SPA, including the new controls.
    for forbidden in (".innerHTML", ".outerHTML", ".insertAdjacentHTML"):
        assert forbidden not in text, f"SPA must never use {forbidden}"


def test_static_index_wires_aec_tab_to_external_module():
    """The AEC tab and view shell live in index.html, but the render layer was
    extracted to ``/static/aec.js`` (FWP10).

    index.html keeps the tab strip entry, the ``#aec-view`` section, loads the
    external script, and delegates the TAB_LOADERS slot to
    ``window.EasyCatAec.renderAecView`` — the heavy render functions no longer
    live inline.  The whole SPA still never reaches for innerHTML-family DOM.
    """
    static_path = (
        pathlib.Path(__file__).resolve().parent.parent.parent
        / "src/easycat/debugger/static/index.html"
    )
    text = static_path.read_text(encoding="utf-8")
    assert 'data-tab="aec"' in text, "AEC tab missing from the tab strip"
    assert 'id="aec-view"' in text, "AEC view section missing"
    assert '<script src="/static/aec.js"></script>' in text, "aec.js not loaded"
    assert "window.EasyCatAec" in text, "AEC tab not delegated to the external module"
    assert "renderAecView" in text, "AEC tab not wired into TAB_LOADERS"
    # The render functions moved out of index.html into aec.js.
    assert "function renderAecView" not in text, "renderAecView must move to aec.js"
    assert "function _aecTrackStrip" not in text, "_aecTrackStrip must move to aec.js"
    assert "function _paintAecErle" not in text, "_paintAecErle must move to aec.js"
    assert "function _aecSwimlane" not in text, "_aecSwimlane must move to aec.js"
    # No unsafe DOM anywhere in the SPA shell.
    for forbidden in (".innerHTML", ".outerHTML", ".insertAdjacentHTML"):
        assert forbidden not in text, f"SPA must never use {forbidden}"


def test_static_aec_js_has_view_without_innerhtml():
    """``aec.js`` must hold the three-track AEC render layer with safe DOM only.

    It builds the three aligned strips (mic-in / reference / post-AEC), the
    ERLE canvas painter, the FSM swimlane, and the VAD what-if controls via
    ``el()``/canvas; it fetches ``/api/aec/<turn>`` and posts to
    ``/api/aec/<turn>/vad-whatif``; and it exposes the ``EasyCatAec`` namespace.
    Like the rest of the SPA it never touches innerHTML-family DOM.
    """
    text = (_static_dir() / "aec.js").read_text(encoding="utf-8")
    assert "EasyCatAec" in text
    assert "function renderAecView" in text
    assert "renderAecView: renderAecView" in text, "EasyCatAec must export renderAecView"
    assert '"/api/aec/"' in text, "renderAecView must fetch /api/aec/<turn>"
    assert "/vad-whatif?threshold=" in text, "VAD what-if POST missing"
    # The three aligned strips, the ERLE canvas painter, and the FSM swimlane.
    assert "function _aecTrackStrip" in text
    assert "function _paintAecErle" in text
    assert "function _aecSwimlane" in text
    assert "turn_state_changed" in text, "FSM swimlane must read turn_state_changed records"
    # The third (reference) strip is rendered when reference frames exist.
    assert '"reference", "reference"' in text, "reference track strip missing"
    for forbidden in (".innerHTML", ".outerHTML", ".insertAdjacentHTML"):
        assert forbidden not in text, f"aec.js must never use {forbidden}"


def test_index_html_loads_aec_script():
    """The SPA must load ``/static/aec.js`` so the namespace is present before
    the inline TAB_LOADERS delegates the AEC tab to it."""
    text = (_static_dir() / "index.html").read_text(encoding="utf-8")
    assert '<script src="/static/aec.js"></script>' in text
    assert "window.EasyCatAec" in text


async def test_static_aec_js_served_with_correct_content_type(tmp_path):
    """The static route serves ``aec.js`` as JavaScript."""
    from easycat.debug.bundle import RunBundle
    from easycat.debugger.server import _bundle_source, _make_app

    from ._server_helpers import _build_voice_bundle

    bundle_path = await _build_voice_bundle(tmp_path)
    RunBundle.load(bundle_path)
    source = _bundle_source(bundle_path)
    app = _make_app(source)

    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/static/aec.js")
        assert resp.status == 200
        ctype = resp.headers["Content-Type"]
        assert "javascript" in ctype or "ecmascript" in ctype, ctype
        body = await resp.text()
        assert "EasyCatAec" in body


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
