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
