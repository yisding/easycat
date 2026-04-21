"""Tests for the DX helpers added from peripheral-dx-onboarding.md."""

from __future__ import annotations

import logging

import pytest

from easycat import EasyCatConfig
from easycat.config import (
    _autodetect_stt_string,
    _autodetect_tts_string,
    _resolve_easycat_log_level,
)
from easycat.stt.deepgram_provider import DeepgramSTTConfig
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTTConfig
from easycat.transports.local import LocalTransportConfig
from easycat.transports.twilio_media import TwilioTransportConfig
from easycat.transports.webrtc import WebRTCTransportConfig
from easycat.tts.elevenlabs_tts import ElevenLabsTTSConfig
from easycat.tts.openai_tts import OpenAITTSConfig

# ── Env-var autodetect ────────────────────────────────────────────


def test_autodetect_stt_prefers_deepgram(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-key")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("CARTESIA_API_KEY", raising=False)
    assert _autodetect_stt_string() == "deepgram/flux"


def test_autodetect_stt_falls_back_to_elevenlabs(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-key")
    assert _autodetect_stt_string() == "elevenlabs"


def test_autodetect_returns_none_when_nothing_set(monkeypatch: pytest.MonkeyPatch):
    for var in ("DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY", "CARTESIA_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert _autodetect_stt_string() is None
    assert _autodetect_tts_string() is None


def test_config_autowires_deepgram_when_only_key_is_deepgram(
    monkeypatch: pytest.MonkeyPatch,
):
    # Only DEEPGRAM_API_KEY set; the plan promises "simplest working
    # config has zero provider strings — just an agent and an env var".
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("CARTESIA_API_KEY", raising=False)
    cfg = EasyCatConfig()
    assert isinstance(cfg.stt, DeepgramSTTConfig)
    # TTS still defaults to OpenAI because only Deepgram STT was
    # autodetected (Deepgram TTS is not part of the autodetect
    # allowlist — see ``_autodetect_tts_string``).
    assert isinstance(cfg.tts, OpenAITTSConfig)


def test_config_autowires_elevenlabs_tts(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-key")
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.delenv("CARTESIA_API_KEY", raising=False)
    cfg = EasyCatConfig()
    # ElevenLabs drives both STT and TTS autodetection because it
    # offers both services; OpenAI remains the LLM default.
    assert isinstance(cfg.tts, ElevenLabsTTSConfig)


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


# ── Config factory presets ───────────────────────────────────────


def test_mic_preset_uses_local_transport(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    cfg = EasyCatConfig.mic()
    assert isinstance(cfg.transport, LocalTransportConfig)


def test_browser_preset_uses_webrtc_transport_and_aec(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    cfg = EasyCatConfig.browser()
    assert isinstance(cfg.transport, WebRTCTransportConfig)
    assert cfg.echo_cancellation is not None
    assert cfg.echo_cancellation.enabled is True


def test_phone_preset_uses_twilio_transport(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    cfg = EasyCatConfig.phone()
    assert isinstance(cfg.transport, TwilioTransportConfig)


def test_preset_still_honors_explicit_overrides(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    cfg = EasyCatConfig.mic(stt=OpenAIRealtimeSTTConfig(api_key="override"))
    # Explicit keyword takes precedence over the preset's transport-only
    # default — the preset must not clobber other fields.
    assert isinstance(cfg.stt, OpenAIRealtimeSTTConfig)
    assert cfg.stt.api_key == "override"


# ── Debugger auto-launch on debug="full" ─────────────────────────


def test_debug_full_skips_auto_launch_under_pytest(monkeypatch: pytest.MonkeyPatch):
    """Ensure ``debug='full'`` does not spin up the debugger during pytest.

    The auto-launch helper short-circuits when ``PYTEST_CURRENT_TEST``
    is set so we don't crash test runs or fight for the debugger port.
    """
    from easycat.config import _maybe_launch_debugger_ui

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

    _maybe_launch_debugger_ui(session=object())
    assert calls == []


def test_debug_full_auto_launches_happy_path(monkeypatch: pytest.MonkeyPatch):
    """``_maybe_launch_debugger_ui`` forwards to ``serve_session`` when aiohttp is available."""
    pytest.importorskip("aiohttp")

    from easycat.config import _maybe_launch_debugger_ui

    calls: list[dict[str, object]] = []

    def _fake_serve(session, **kwargs):
        calls.append({"session": session, **kwargs})

    monkeypatch.setattr("easycat.debugger.serve_session", _fake_serve, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("EASYCAT_DEBUGGER_DISABLE", raising=False)
    monkeypatch.delenv("EASYCAT_DEBUGGER_PORT", raising=False)
    monkeypatch.setenv("EASYCAT_DEBUGGER_OPEN_BROWSER", "0")

    sentinel = object()
    _maybe_launch_debugger_ui(session=sentinel)

    assert len(calls) == 1
    assert calls[0]["session"] is sentinel
    assert calls[0]["port"] == 8765
    assert calls[0]["open_browser"] is False
    assert calls[0]["in_thread"] is True


def test_debug_full_bad_port_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch):
    """A non-integer ``EASYCAT_DEBUGGER_PORT`` must not crash the launch."""
    pytest.importorskip("aiohttp")

    from easycat.config import _maybe_launch_debugger_ui

    captured: dict[str, object] = {}

    def _fake_serve(session, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("easycat.debugger.serve_session", _fake_serve, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("EASYCAT_DEBUGGER_DISABLE", raising=False)
    monkeypatch.setenv("EASYCAT_DEBUGGER_PORT", "not-a-number")

    _maybe_launch_debugger_ui(session=object())

    assert captured["port"] == 8765


def test_debug_full_skips_when_aiohttp_missing(monkeypatch: pytest.MonkeyPatch):
    """Missing aiohttp logs a hint and does not attempt to start the server."""
    import sys

    from easycat.config import _maybe_launch_debugger_ui

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

    _maybe_launch_debugger_ui(session=object())

    assert calls == []
