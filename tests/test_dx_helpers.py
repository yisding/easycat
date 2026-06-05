"""Tests for the DX helpers added from peripheral-dx-onboarding.md."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from easycat import EasyConfig, SessionConfig, require_env
from easycat.config import _resolve_easycat_log_level
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTTConfig
from easycat.transports.local import LocalTransportConfig
from easycat.transports.twilio_media import TwilioTransportConfig
from easycat.transports.webrtc import WebRTCTransportConfig

REPO_ROOT = Path(__file__).resolve().parents[1]

# ── EASYCAT_LOG_LEVEL ─────────────────────────────────────────────


def test_log_level_env_respected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EASYCAT_LOG_LEVEL", "warning")
    assert _resolve_easycat_log_level(default=logging.DEBUG) == logging.WARNING


def test_log_level_unknown_falls_back_to_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EASYCAT_LOG_LEVEL", "loud")
    assert _resolve_easycat_log_level(default=logging.INFO) == logging.INFO


def test_log_level_unset_returns_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EASYCAT_LOG_LEVEL", raising=False)
    assert _resolve_easycat_log_level(default=logging.ERROR) == logging.ERROR


def test_require_env_missing_value_gives_actionable_hint(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        require_env("OPENAI_API_KEY")

    message = str(excinfo.value)
    assert "OPENAI_API_KEY is required." in message
    assert "export OPENAI_API_KEY=..." in message
    assert "uv run easycat doctor" in message


# ── Config factory presets ───────────────────────────────────────


def test_mic_preset_uses_local_transport(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    cfg = EasyConfig.mic()
    assert isinstance(cfg.transport, LocalTransportConfig)


def test_browser_preset_uses_webrtc_transport_and_aec(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    cfg = EasyConfig.browser()
    assert isinstance(cfg.transport, WebRTCTransportConfig)
    assert cfg.echo_cancellation is not None
    assert cfg.echo_cancellation.enabled is True


def test_phone_preset_uses_twilio_transport(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    cfg = EasyConfig.phone()
    assert isinstance(cfg.transport, TwilioTransportConfig)


def test_preset_still_honors_explicit_overrides(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    cfg = EasyConfig.mic(stt=OpenAIRealtimeSTTConfig(api_key="override"))
    # Explicit keyword takes precedence over the preset's transport-only
    # default — the preset must not clobber other fields.
    assert isinstance(cfg.stt, OpenAIRealtimeSTTConfig)
    assert cfg.stt.api_key == "override"


def test_canonical_example_keeps_next_step_breadcrumbs() -> None:
    example = (REPO_ROOT / "examples" / "openai_agents_voice.py").read_text(encoding="utf-8")

    assert "uv add 'easycat[quickstart]'" in example
    assert "uv sync --extra quickstart" in example
    assert "# Next, try" in example
    assert 'stt="deepgram/nova-2"' in example
    assert "DEEPGRAM_API_KEY + --extra deepgram" in example
    assert "tools live on YOUR Agent" in example
    assert "EasyConfig.browser(agent=...)" in example
    assert "server + --extra webrtc" in example
    assert 'debug="full"' in example
    assert "docs/teaching/00-hello-audio/" in example


def test_easyconfig_preset_docstrings_explain_next_rungs() -> None:
    mic_doc = EasyConfig.mic.__doc__ or ""
    browser_doc = EasyConfig.browser.__doc__ or ""
    phone_doc = EasyConfig.phone.__doc__ or ""

    assert "Next:" in mic_doc
    assert "stt=" in mic_doc and "tts=" in mic_doc
    assert "browser()" in mic_doc and "phone()" in mic_doc
    assert "DEEPGRAM_API_KEY" in mic_doc and "easycat[deepgram]" in mic_doc

    assert "Next:" in browser_doc
    assert "server process" in browser_doc
    assert "easycat[webrtc]" in browser_doc
    assert "examples/webrtc_server.py" in browser_doc

    assert "Next:" in phone_doc
    assert "server process" in phone_doc
    assert "easycat[telephony]" in phone_doc
    assert "examples/twilio_app.py" in phone_doc


def test_sessionconfig_docstring_steers_to_easyconfig() -> None:
    doc = SessionConfig.__doc__ or ""

    assert "lowest rung of the ladder" in doc
    assert "provider *instances*" in doc
    assert "EasyConfig" in doc
    assert "one rung up" in doc
    assert "create_session" in doc


# ── Debugger auto-launch on debug="full" ─────────────────────────


def test_debug_full_skips_auto_launch_under_pytest(monkeypatch: pytest.MonkeyPatch):
    """Ensure ``debug='full'`` does not spin up the debugger during pytest.

    The auto-launch helper short-circuits when ``PYTEST_CURRENT_TEST``
    is set so we don't crash test runs or fight for the debugger port.
    """
    from easycat.debugger._autolaunch import maybe_launch_debugger_ui

    calls: list[object] = []

    def _fake_serve(session, **kwargs):
        calls.append(session)

    # The skip fires before serve_session is consulted, so this
    # monkeypatch should never be invoked.
    monkeypatch.setattr(
        "easycat.debugger.serve_session",
        _fake_serve,
        raising=False,
    )
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "on")

    maybe_launch_debugger_ui(session=object())
    assert calls == []


def test_debug_full_auto_launches_happy_path(monkeypatch: pytest.MonkeyPatch):
    """``_maybe_launch_debugger_ui`` forwards to ``serve_session`` when aiohttp is available."""
    pytest.importorskip("aiohttp")

    from easycat.debugger._autolaunch import maybe_launch_debugger_ui

    calls: list[dict[str, object]] = []

    def _fake_serve(session, **kwargs):
        calls.append({"session": session, **kwargs})

    monkeypatch.setattr("easycat.debugger.serve_session", _fake_serve, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("EASYCAT_DEBUGGER_DISABLE", raising=False)
    monkeypatch.delenv("EASYCAT_DEBUGGER_PORT", raising=False)
    monkeypatch.setenv("EASYCAT_DEBUGGER_OPEN_BROWSER", "0")

    sentinel = object()
    maybe_launch_debugger_ui(session=sentinel)

    assert len(calls) == 1
    assert calls[0]["session"] is sentinel
    assert calls[0]["port"] == 8765
    assert calls[0]["open_browser"] is False
    assert calls[0]["in_thread"] is True


def test_debug_full_bad_port_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch):
    """A non-integer ``EASYCAT_DEBUGGER_PORT`` must not crash the launch."""
    pytest.importorskip("aiohttp")

    from easycat.debugger._autolaunch import maybe_launch_debugger_ui

    captured: dict[str, object] = {}

    def _fake_serve(session, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("easycat.debugger.serve_session", _fake_serve, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("EASYCAT_DEBUGGER_DISABLE", raising=False)
    monkeypatch.setenv("EASYCAT_DEBUGGER_PORT", "not-a-number")

    maybe_launch_debugger_ui(session=object())

    assert captured["port"] == 8765


def test_debug_full_skips_when_aiohttp_missing(monkeypatch: pytest.MonkeyPatch):
    """Missing aiohttp logs a hint and does not attempt to start the server."""
    import sys

    from easycat.debugger._autolaunch import maybe_launch_debugger_ui

    calls: list[object] = []

    def _fake_serve(session, **kwargs):
        calls.append(session)

    monkeypatch.setattr("easycat.debugger.serve_session", _fake_serve, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("EASYCAT_DEBUGGER_DISABLE", raising=False)
    # Block ``import aiohttp`` even if the debugger extra is installed —
    # ``sys.modules[name] = None`` is the documented way to force a
    # future ``import`` to raise ``ImportError``.
    monkeypatch.setitem(sys.modules, "aiohttp", None)

    maybe_launch_debugger_ui(session=object())

    assert calls == []
