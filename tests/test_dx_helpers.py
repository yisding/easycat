"""Tests for the DX helpers added from peripheral-dx-onboarding.md."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from dataclasses import fields
from pathlib import Path

import pytest

import easycat
from easycat import EasyConfig, SessionConfig, require_env, run
from easycat.config import _resolve_easycat_log_level
from easycat.config.easy import _EASYCAT_LOG_LEVELS
from easycat.helpers import _feedback_enabled
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


def test_run_docstring_tracks_log_level_env_vocabulary() -> None:
    doc = run.__doc__ or ""
    missing = sorted(level for level in _EASYCAT_LOG_LEVELS if level not in doc)

    assert not missing, "run() docstring missing EASYCAT_LOG_LEVEL values: " + ", ".join(missing)


def test_run_docstring_tracks_feedback_modes() -> None:
    doc = run.__doc__ or ""

    assert 'feedback="auto"' in doc
    assert 'feedback="on"' in doc
    assert 'feedback="off"' in doc


def test_run_feedback_auto_uses_tty_outside_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    assert _feedback_enabled("auto", stderr_isatty=True) is True
    assert _feedback_enabled("auto", stderr_isatty=False) is False

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_dx_helpers.py::test")
    assert _feedback_enabled("auto", stderr_isatty=True) is False


def test_run_feedback_on_off_are_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_dx_helpers.py::test")

    assert _feedback_enabled("on", stderr_isatty=False) is True
    assert _feedback_enabled("off", stderr_isatty=True) is False


def test_run_feedback_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="Unknown feedback mode.*auto.*on.*off"):
        _feedback_enabled("loud")  # type: ignore[arg-type]


def test_require_env_missing_value_gives_actionable_hint(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        require_env("OPENAI_API_KEY")

    message = str(excinfo.value)
    assert "OPENAI_API_KEY is required." in message
    assert "export OPENAI_API_KEY=..." in message
    assert "uv run easycat doctor" in message
    assert "uv run --env-file .env ..." in message
    assert "uv run easycat doctor --env-file .env" in message


async def test_create_shutdown_event_wires_signal_handlers(monkeypatch: pytest.MonkeyPatch):
    from easycat.helpers import create_shutdown_event

    captured: dict[str, object] = {}

    def install_shutdown(loop: asyncio.AbstractEventLoop, event: asyncio.Event) -> bool:
        captured["loop"] = loop
        captured["event"] = event
        return True

    monkeypatch.setattr("easycat._signals.install_shutdown_signal_handlers", install_shutdown)

    event = create_shutdown_event()

    assert isinstance(event, asyncio.Event)
    assert captured["loop"] is asyncio.get_running_loop()
    assert captured["event"] is event


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
    assert cfg.transport.host == "127.0.0.1"
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
    assert "uv sync --extra quickstart --group dev" in example
    assert "# Next, try" in example
    assert 'stt="deepgram/nova-2"' in example
    assert "DEEPGRAM_API_KEY + --extra deepgram" in example
    assert "tools live on YOUR Agent" in example
    assert "EasyConfig.browser(agent=...)" in example
    assert "server + --extra webrtc" in example
    assert 'debug="full"' in example
    assert "`easycat inspect`" not in example
    assert "uv run easycat inspect .easycat/journals/<session_id>.sqlite" in example
    assert "uv run easycat docs --audience learners" in example
    assert "docs/teaching/00-hello-audio/" in example


def test_package_docstring_leads_with_canonical_quickstart() -> None:
    doc = easycat.__doc__ or ""

    assert doc.startswith("EasyCat — a voice bot in three lines.")
    assert "uv add 'easycat[quickstart]'" in doc
    assert "from agents import Agent" in doc
    assert "from easycat import EasyConfig, run" in doc
    assert "run(EasyConfig.mic(agent=Agent(" in doc
    assert "uv run easycat doctor" in doc
    assert "uv run easycat doctor --env-file .env" in doc
    assert "uv run --env-file .env ..." in doc
    assert "Session.from_providers" in doc
    assert "hand-build provider instances" in doc
    before_raw_providers = doc.split("Start here", 1)[1].split("Session.from_providers", 1)[0]
    assert "create_session" not in before_raw_providers


def test_easyconfig_preset_docstrings_explain_next_rungs() -> None:
    config_doc = " ".join((EasyConfig.__doc__ or "").split())
    mic_doc = " ".join((EasyConfig.mic.__doc__ or "").split())
    browser_doc = " ".join((EasyConfig.browser.__doc__ or "").split())
    phone_doc = " ".join((EasyConfig.phone.__doc__ or "").split())

    assert "provider shortcut" in config_doc
    assert "provider instances" in config_doc
    assert "VADConfig" in config_doc
    assert "NoiseReducerConfig" in config_doc
    assert "EchoCancellationConfig" in config_doc
    assert "smart_turn" in config_doc
    assert "smart_turn_sensitivity" in config_doc

    assert "Next:" in mic_doc
    assert "stt=" in mic_doc and "tts=" in mic_doc
    assert "shortcut string" in mic_doc
    assert "config dataclass" in mic_doc
    assert "provider instance" in mic_doc
    assert "vad=" in mic_doc
    assert "browser()" in mic_doc and "phone()" in mic_doc
    assert "DEEPGRAM_API_KEY" in mic_doc and "easycat[deepgram]" in mic_doc

    assert "Next:" in browser_doc
    assert "server process" in browser_doc
    assert "easycat[webrtc]" in browser_doc
    assert "examples/webrtc_server.py" in browser_doc
    assert "provider instances" in browser_doc
    assert "vad=" in browser_doc

    assert "Next:" in phone_doc
    assert "server process" in phone_doc
    assert "easycat[telephony]" in phone_doc
    assert "examples/twilio_app.py" in phone_doc
    assert "provider instances" in phone_doc
    assert "vad=" in phone_doc


def test_sessionconfig_docstring_steers_to_easyconfig() -> None:
    doc = SessionConfig.__doc__ or ""

    assert "lowest rung of the ladder" in doc
    assert "provider *instances*" in doc
    assert "EasyConfig" in doc
    assert "Session.from_providers" in doc
    assert "one rung up" in doc
    assert "create_session" in doc


def test_dx_onboarding_plan_tracks_current_easyconfig_surface() -> None:
    plan = (REPO_ROOT / "plan" / "peripherals" / "peripheral-dx-onboarding.md").read_text(
        encoding="utf-8"
    )
    plan_index = (REPO_ROOT / "plan" / "peripherals" / "README.md").read_text(encoding="utf-8")
    field_names = {field.name for field in fields(EasyConfig)}
    field_count = len(field_names)
    runtime_knobs = plan.split(
        "Runtime enforcement for advanced observability knobs remains:",
        1,
    )[1].split(
        "The high-leverage DX wins",
        1,
    )[0]
    remaining_summary = plan.split("The high-leverage DX wins are shipped;", 1)[1].split(
        ">",
        1,
    )[0]
    normalized_remaining_summary = " ".join(remaining_summary.split())

    assert "session_policy" in field_names
    assert "audio_processing" in field_names
    assert "observability" in field_names
    assert {
        "greeting",
        "dnc_list",
        "opt_out_detection",
        "opt_out_phrases",
        "caller_id_exposure",
    }.isdisjoint(field_names)
    assert {
        "vad",
        "noise_reduction",
        "echo_cancellation",
        "enable_noise_reduction",
        "enable_echo_cancellation",
        "smart_turn",
        "smart_turn_sensitivity",
    }.isdisjoint(field_names)
    assert {
        "debug",
        "journal_backend",
        "journal_retention",
    }.isdisjoint(field_names)
    assert "EasyCatConfig" not in plan
    assert "EasyConfig(record_to=...)" in plan
    assert "`record_to=`" not in runtime_knobs
    assert f"currently {field_count} top-level `EasyConfig` fields" in plan
    assert "target ≤22" in plan
    assert field_count <= 22
    assert f"`EasyConfig` remains at {field_count} top-level fields" in plan_index
    assert "≤22 flattening target" in plan_index
    assert "field flattening" not in remaining_summary
    assert "ecosystem-gated offline preset wiring" in remaining_summary
    assert "cross-pipeline" in remaining_summary
    assert "full structlog adoption" in remaining_summary
    assert "non-canonical example shrinkage" in normalized_remaining_summary
    assert (
        "runtime enforcement for first-token/audio `latency_budget=` alerts"
        in normalized_remaining_summary
    )
    assert "first-token/audio `latency_budget=` alerts" in normalized_remaining_summary
    assert "`warmup=`" not in remaining_summary
    assert "`max_session_cost_usd=`" not in remaining_summary


def test_dx_onboarding_status_uses_stable_source_symbols() -> None:
    """Keep current DX status notes anchored to symbols, not stale line numbers."""
    from easycat.helpers import run as helpers_run
    from easycat.runtime.records import ErrorInfo
    from easycat.session._session import Session

    plan = (REPO_ROOT / "plan" / "peripherals" / "peripheral-dx-onboarding.md").read_text(
        encoding="utf-8"
    )
    status = plan.split("## Status", 1)[1].split("Still remaining:", 1)[0]
    normalized_status = " ".join(status.split())
    line_refs = re.findall(r"`?[\w./-]+\.py:\d+(?:-\d+)?`?", status)

    assert not line_refs, "DX onboarding status uses brittle source line refs: " + ", ".join(
        line_refs
    )
    assert "examples/pydantic_ai_workflow_voice.py" in status
    assert "same slim workflow object" in status
    assert "without structured-output or usage-history plumbing" in normalized_status
    assert "examples/debug_bundle.py" in status
    assert "through `run(EasyConfig.mic(...))`" in status
    assert "instead of manually starting a session" in normalized_status
    assert "examples/journal_demo.py" in status
    assert "`scripted_turn_providers(...)`" in status
    assert "visible-code budget is now ≤40 instead of ≤90" in normalized_status
    assert "Advanced observability knobs are now config-addressable" in status
    assert "`ObservabilityConfig` carries" in status
    assert "`latency_budget_exceeded`" in status
    assert "`easycat.runtime.cost_budget_status(...)`" in status
    assert '`latency_budget=LatencyBudget(stage="total_ms", max_ms=...)`' in status
    assert "turn-level `latency_budget_exceeded` metric records" in status
    assert "`cost_budget_warning`" in status
    assert "`cost_budget_exceeded`" in status
    assert "`cost_budget_stop_requested`" in status
    assert "`stop(force=True)`" in status
    assert "`warmup_completed`" in status
    assert "`warmup_failed`" in status
    assert "structured" in status
    assert "examples/ws_supervisor_server.py" in status
    assert "serve_supervisor_websocket" in status
    assert "visible-code budget is now ≤140 instead of ≤265" in status
    assert "examples/agent_event_subscription.py" in status
    assert "examples/journal_ui.py" in status
    assert "letting `EasyConfig.mic(...)` own the default OpenAI key validation" in (
        normalized_status
    )
    assert "their guarded visible-code budgets are now ≤48 and ≤23" in normalized_status
    assert "`EasyConfig.browser(...)`" in status
    assert "duplicate OpenAI key preflight" in normalized_status
    assert "`webrtc_transport_config_from_env(...)`" in status
    assert "visible-code budgets are now ≤39, ≤29, and ≤59" in normalized_status
    assert "examples/reconnecting_ws_client.py" in status
    assert "`create_shutdown_event()`" in status
    assert "`connect_until_stopped(...)`" in status
    assert "visible-code budget is now ≤75 instead of ≤94" in status
    assert "examples/vad_backends.py" in status
    assert "`VADConfig(backend=...)`" in status
    assert "`run(EasyConfig.mic(vad=...))`" in status
    assert "visible-code budget is now ≤28 instead of ≤30" in normalized_status
    assert "examples/twilio_app.py" in status
    assert "reusable webhook request helpers" in normalized_status
    assert "`twilio_app_settings_from_env(...)`" in status
    assert "visible-code budget is now ≤150 instead of ≤205" in normalized_status
    assert "examples/push_to_talk.py" in status
    assert "`EasyConfig.mic(turn_taking=...)`" in status
    assert "`create_session(config)`" in status
    assert "`run_stdin_push_to_talk_session(session)`" in status
    assert "duplicate key preflight" in normalized_status
    assert "explicit local transport setup" in normalized_status
    assert "runtime feedback attachment" in normalized_status
    assert "`asyncio.run(...)`" in status
    assert "visible-code budget is now ≤27 instead of ≤90" in normalized_status
    symbol_refs = {
        "src/easycat/helpers.py::run": helpers_run,
        "src/easycat/runtime/records.py::ErrorInfo.from_exception": ErrorInfo.from_exception,
        "src/easycat/session/_session.py::export_debug_bundle": Session.export_debug_bundle,
        "src/easycat/session/_session.py::__aenter__": Session.__aenter__,
        "src/easycat/session/_session.py::__aexit__": Session.__aexit__,
    }
    for symbol_ref, symbol in symbol_refs.items():
        assert symbol_ref in status
        assert callable(symbol)


def test_dx_onramp_plan_uses_stable_current_symbols() -> None:
    """Keep the DX onramp plan from drifting back to brittle source line refs."""
    import easycat
    from easycat.config._factory import _validate_agent_shape, create_session, create_text_session
    from easycat.config.easy import EasyConfig, _AgentSessionConfig
    from easycat.errors import EasyCatError, register
    from easycat.helpers import _wired_summary, run

    plan = (REPO_ROOT / "plan" / "dx" / "onramp-zen-dx-plan.md").read_text(encoding="utf-8")
    line_refs = re.findall(r"`?[\w./-]+\.(?:py|md):\d+(?:-\d+)?`?", plan)

    assert not line_refs, "DX onramp plan uses brittle file-line refs: " + ", ".join(line_refs)
    assert "src/easycat/config.py" not in plan
    assert "`config.py`" not in plan
    assert f"{len(easycat.__all__)} flat alphabetical names" in plan
    assert f"curated {len(easycat.__all__)}" in plan
    assert f"tested {len(easycat.__all__)}-name `__all__` contract" in plan
    assert "84 flat alphabetical names" not in plan
    assert "curated 84" not in plan
    assert "tested 84-name `__all__` contract" not in plan

    symbol_refs = {
        "src/easycat/config/easy.py::EasyConfig.__post_init__": EasyConfig.__post_init__,
        "src/easycat/config/easy.py::EasyConfig._validate": EasyConfig._validate,
        "src/easycat/config/easy.py::_AgentSessionConfig": _AgentSessionConfig,
        "src/easycat/config/_factory.py::create_session": create_session,
        "src/easycat/config/_factory.py::create_text_session": create_text_session,
        "src/easycat/config/_factory.py::_validate_agent_shape": _validate_agent_shape,
        "src/easycat/errors.py::EasyCatError.__init__": EasyCatError.__init__,
        "src/easycat/errors.py::register": register,
        "src/easycat/helpers.py::run": run,
        "src/easycat/helpers.py::_wired_summary": _wired_summary,
    }
    for symbol_ref, symbol in symbol_refs.items():
        assert symbol_ref in plan
        assert callable(symbol)

    landed_statuses = {
        "5.1": "landed; guarded",
        "5.2": "landed; guarded",
        "5.3": "landed; guarded",
        "5.4": "folded into 5.3",
        "5.5": "landed; guarded",
        "5.6": "landed; guarded",
        "5.7": "landed; guarded",
        "5.8": "landed; guarded",
        "5.9": "landed; guarded",
        "5.10": "landed",
        "5.11": "landed; guarded",
        "5.12": "landed Part A; Part B dropped",
        "5.13": "landed; guarded",
    }
    for number, status in landed_statuses.items():
        pattern = rf"^### {re.escape(number)} .* \*\({re.escape(status)}\)\*$"
        assert re.search(pattern, plan, re.MULTILINE), f"section {number} status drifted"


def test_dx_onramp_plan_marks_canonical_hello_world_landed_with_current_evidence() -> None:
    plan = (REPO_ROOT / "plan" / "dx" / "onramp-zen-dx-plan.md").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    example = (REPO_ROOT / "examples" / "openai_agents_voice.py").read_text(encoding="utf-8")
    scaffold = (
        REPO_ROOT
        / "src"
        / "easycat"
        / "cli"
        / "scaffold"
        / "templates"
        / "openai-agents"
        / "agent.py"
    ).read_text(encoding="utf-8")
    package_doc = easycat.__doc__ or ""
    quickstart = readme.split("### Quickstart (EasyConfig)", 1)[1].split(
        "## Install",
        1,
    )[0]
    advanced = readme.split("### Advanced: own the lifecycle", 1)[1].split(
        "## Telephony",
        1,
    )[0]
    normalized_quickstart = " ".join(quickstart.split())

    assert "### 5.1" in plan
    assert "*(landed; guarded)*" in plan.split("### 5.1", 1)[1].split("### 5.2", 1)[0]
    assert "test_dx_onramp_plan_marks_canonical_hello_world_landed_with_current_evidence" in (plan)

    assert "run(EasyConfig.mic(agent=Agent(" in package_doc
    assert "run(\n    EasyConfig.mic(" in quickstart
    assert "create_session" not in quickstart
    assert "your-api-key" not in quickstart
    assert "one canonical shape" in normalized_quickstart
    assert "examples/openai_agents_voice.py" in quickstart
    assert "easycat init my-agent" in quickstart

    assert "run(\n    EasyConfig.mic(" in example
    assert "create_session" not in example
    assert "run(EasyConfig.mic(agent=agent, **__EASYCAT_CONFIG_EXTRA__))" in scaffold

    # The advanced section is now a one-line door into the graduation guide,
    # which carries the full create_session/run_session lifecycle example.
    guide = (REPO_ROOT / "docs" / "from-easyconfig-to-session.md").read_text(encoding="utf-8")
    assert "docs/from-easyconfig-to-session.md" in advanced
    assert "create_session(...)" in guide
    assert "from easycat import EasyConfig, STTFinal, create_session" in guide
    assert "from easycat.helpers import run_session" in guide
    assert "session = create_session(EasyConfig.mic(agent=agent))" in guide
    assert "run_session(session)" in guide
    assert "async with session:" in guide


def test_dx_onramp_plan_marks_lifecycle_idiom_landed_with_current_evidence() -> None:
    from easycat.session._session import Session

    plan = (REPO_ROOT / "plan" / "dx" / "onramp-zen-dx-plan.md").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    claude = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    chapter_15 = (
        REPO_ROOT / "docs" / "teaching" / "15-operate-in-production" / "README.md"
    ).read_text(encoding="utf-8")
    lifecycle = readme.split("## Session lifecycle", 1)[1].split(
        "## Pre-TTS output processors",
        1,
    )[0]
    section = plan.split("### 5.11", 1)[1].split("### 5.12", 1)[0]
    stop_signature = inspect.signature(Session.stop)

    assert "*(landed; guarded)*" in section
    assert "test_dx_onramp_plan_marks_lifecycle_idiom_landed_with_current_evidence" in section

    assert inspect.iscoroutinefunction(Session.__aenter__)
    assert inspect.iscoroutinefunction(Session.__aexit__)
    assert inspect.iscoroutinefunction(Session.wait_closed)
    assert inspect.iscoroutinefunction(Session.stop)
    assert inspect.iscoroutinefunction(Session.shutdown)
    assert stop_signature.parameters["force"].kind is inspect.Parameter.KEYWORD_ONLY
    assert stop_signature.parameters["force"].default is False
    assert not hasattr(Session, "close")
    assert not hasattr(Session, "destroy")
    assert callable(Session._close)
    assert callable(Session._destroy)

    run_doc = run.__doc__ or ""
    assert "async with session:" in run_doc
    assert "stop(force=True)" in run_doc
    assert "await session.shutdown()" not in run_doc

    assert "`async with session:` is the one public teardown idiom" in lifecycle
    assert "`await session.stop()` is the single public teardown verb" in lifecycle
    assert "`await session.wait_closed()`" in lifecycle
    assert "await session.shutdown()" not in lifecycle
    for stale in ("session.close()", "session.destroy()"):
        assert stale not in readme
        assert stale not in chapter_15

    for guide in (agents, claude):
        assert "`await session.stop()` is the single public teardown verb" in guide
        assert "`async with session:` is the preferred idiom" in guide
        assert "session.shutdown()" in guide
        assert "thin alias for `stop(force=True)`" in guide
        assert "Session._destroy()" in guide and "Session._close()" in guide
        assert "not public entry points" in guide

    assert "Compatibility alias for `stop(force=True)`" in chapter_15
    assert "new docs should usually show `stop(...)` or `async with session:`" in chapter_15


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
