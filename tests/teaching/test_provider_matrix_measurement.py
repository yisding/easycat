from __future__ import annotations

import importlib.util
import json
import shlex
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from typer.testing import CliRunner

from easycat.cli._app import _register_commands, app
from easycat.config._telephony_wiring import create_action_executors
from easycat.debug.export import export_debug_bundle
from easycat.runtime import JournalRecord, TimingInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
CHAPTER = REPO_ROOT / "docs" / "teaching" / "13-swap-providers-and-transports"


def _load_main_module(chapter: Path = CHAPTER) -> ModuleType:
    path = chapter / "main.py"
    module_name = f"teaching_{chapter.name.replace('-', '_')}_main"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_measurement_commands_read_production_bundle_directly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "bundle output" / "ch13-openai-local-123.bundle"
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=object))

    for slug in (
        "13-swap-providers-and-transports",
        "14-bring-your-own-agent",
        "15-operate-in-production",
    ):
        chapter = _load_main_module(REPO_ROOT / "docs" / "teaching" / slug)
        display_path = chapter._display_path(bundle)
        base = ["uv", "run", "easycat", "latency", str(display_path)]
        assert chapter.measurement_commands(bundle) == (
            shlex.join(base),
            shlex.join([*base, "--json"]),
        )


def test_twilio_preset_wires_session_action_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chapter = _load_main_module()
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret")

    telephony = chapter.telephony_config("twilio")
    executors = create_action_executors(telephony)

    assert chapter.telephony_config("local") is None
    assert telephony.twilio_actions.account_sid == "AC-test"
    assert telephony.twilio_actions.auth_token == "secret"
    assert [type(executor).__name__ for executor in executors] == ["TwilioSessionActionExecutor"]


def test_printed_latency_command_accepts_production_journal_shape(tmp_path: Path) -> None:
    _register_commands()
    ms = 1_000_000
    records = [
        JournalRecord(
            sequence=index,
            session_id="ch13",
            turn_id="turn-1",
            name=name,
            timing=TimingInfo(wall_ns=offset_ms * ms),
        )
        for index, (name, offset_ms) in enumerate(
            (
                ("vad_stop_speaking", 0),
                ("stt_final", 50),
                ("agent_request_started", 80),
                ("agent_delta", 200),
                ("tts_frame", 300),
            ),
            start=1,
        )
    ]
    bundle = tmp_path / "ch13.bundle"
    journal = SimpleNamespace(read=lambda: records)
    export_debug_bundle(SimpleNamespace(journal=journal), bundle)

    result = CliRunner().invoke(app, ["latency", str(bundle), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["command"] == "latency"
    assert payload["percentiles"]["vad->tts"]["p50"] == pytest.approx(300.0)


def test_live_single_session_chapters_exit_scope_before_export() -> None:
    for slug in ("13-swap-providers-and-transports", "14-bring-your-own-agent"):
        source = (REPO_ROOT / "docs" / "teaching" / slug / "main.py").read_text(encoding="utf-8")
        scope = source.index("async with session:")
        export = source.index("export_debug_bundle(session, path, overwrite=True)")
        assert scope < export, slug
        assert "await session.start()" not in source, slug
        assert "await session.stop(force=True)" not in source, slug


@pytest.mark.asyncio
async def test_run_stops_before_export_and_prints_measurement_commands(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    chapter = _load_main_module()
    events: list[object] = []

    class FakeSession:
        async def __aenter__(self):
            events.append("start")
            return self

        async def __aexit__(self, _exc_type, _exc, _tb) -> None:
            events.append(("stop", True))

    session = FakeSession()

    async def fake_wait(_session) -> None:
        events.append("wait")

    def fake_export(exported_session, path, *, overwrite: bool) -> None:
        events.append(("export", exported_session, path, overwrite))

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(sys, "argv", ["main.py", "--provider-mix", "openai"])
    monkeypatch.setattr(chapter, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(chapter, "build_agent", lambda: object())
    monkeypatch.setattr(chapter, "provider_mix", lambda _name: {"stt": "x", "tts": "y"})
    monkeypatch.setattr(chapter, "transport_config", lambda _name: object())
    monkeypatch.setattr(chapter, "EasyConfig", lambda **kwargs: kwargs)
    monkeypatch.setattr(chapter, "create_session", lambda _config: session)
    monkeypatch.setattr(chapter, "attach_runtime_feedback", lambda _session: None)
    monkeypatch.setattr(chapter, "wait_for_shutdown_signal", fake_wait)
    monkeypatch.setattr(chapter, "export_debug_bundle", fake_export)
    monkeypatch.setattr(chapter.time, "time", lambda: 123)

    await chapter.main()

    bundle = tmp_path / "ch13-openai-local-123.bundle"
    assert events == [
        "start",
        "wait",
        ("stop", True),
        ("export", session, bundle, True),
    ]
    output = capsys.readouterr().out
    display_path = chapter._display_path(bundle)
    assert f"uv run easycat latency {display_path}" in output
    assert f"uv run easycat latency {display_path} --json" in output


def test_lesson_distinguishes_pipeline_from_delivery_latency() -> None:
    from easycat.transports._webrtc_audio import WEBRTC_SAMPLE_RATE
    from easycat.transports._webrtc_config import WebRTCTransportConfig
    from easycat.transports.local import LocalTransportConfig
    from easycat.transports.twilio_media import MULAW_8K

    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")
    normalized_readme = " ".join(readme.split())

    assert "uv run easycat latency PATH" in readme
    assert "no translator is required" in readme
    assert "does **not** include browser/PSTN delivery" in normalized_readme
    assert "uv run easycat latency PATH --json" in exercises
    assert "client `getStats()` artifacts" in exercises
    assert "you will need a small translator" not in readme
    assert "`evals.py` translator" not in exercises
    assert "Custom local providers on Local" in readme
    assert "future: local models" not in readme
    assert LocalTransportConfig().audio_format.sample_rate == 24_000
    assert WebRTCTransportConfig().audio_format.sample_rate == 16_000
    assert WEBRTC_SAMPLE_RATE == 48_000
    assert (MULAW_8K.sample_rate, MULAW_8K.encoding) == (8_000, "mulaw")
    for claim in ("Local uses 24 kHz PCM", "48 kHz media frames", "μ-law at 8 kHz"):
        assert claim in readme


def test_exercises_name_current_provider_and_session_action_contracts() -> None:
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")

    assert '{"stt": "cartesia", "tts": "cartesia"}' in exercises
    assert "CARTESIA_API_KEY" in exercises
    assert "--extra cartesia" in exercises
    assert "Cartesia is already registered on both sides" in exercises
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    assert "expanded nine-cell matrix" in readme

    for name in (
        "session_action_requested",
        "session_action_started",
        "session_action_completed",
        "session_action_failed",
    ):
        assert f"`{name}`" in exercises
    assert "session_action.dispatched" not in exercises
    assert "session_action.unhandled" not in exercises
    assert "silent no-op" in exercises
