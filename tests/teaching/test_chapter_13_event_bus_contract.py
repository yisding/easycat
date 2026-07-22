"""Keep Chapter 13's EventBus lesson aligned with provider factories."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from tests._markdown_asserts import assert_prose_in

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "13-swap-providers-and-transports"


def _load_probe():
    path = CHAPTER / "event_bus_probe.py"
    spec = importlib.util.spec_from_file_location("teaching_ch13_event_bus_probe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_event_bus_probe_covers_both_provider_catalogs(capsys) -> None:
    probe = _load_probe()
    rows = probe.catalog_rows()

    assert rows == [
        ("stt", "cartesia", True),
        ("stt", "deepgram", True),
        ("stt", "elevenlabs", True),
        ("stt", "openai", False),
        ("stt", "openai-realtime", True),
        ("tts", "cartesia", True),
        ("tts", "deepgram", True),
        ("tts", "elevenlabs", True),
        ("tts", "openai", True),
    ]

    probe.main()
    output = capsys.readouterr().out
    assert "stt      openai           no" in output
    assert "tts      openai           yes" in output


def test_chapter_distinguishes_reconnects_from_http_provider_errors() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")

    assert "`event_bus` dataclass field" in readme
    assert "HTTP OpenAI TTS uses it for provider `Error` events" in readme
    assert_prose_in("cannot emit\n  reconnect lifecycle", readme)
    assert "session bus is not the audio/transcript stream" in readme
    assert_prose_in(
        "distinguish reconnect telemetry from HTTP provider-error telemetry",
        exercises,
    )
