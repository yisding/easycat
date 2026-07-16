"""Keep Chapter 14's pronunciation exercise observable and honest."""

from __future__ import annotations

import importlib.util
import json
import shlex
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from typer.testing import CliRunner

from easycat.cli._app import _register_commands, app
from easycat.debug.export import export_debug_bundle
from easycat.llm_output_processing import apply_output_processors
from easycat.runtime import JournalRecord
from easycat.tts.base import TTSBase
from easycat.tts.cartesia_tts import CartesiaTTS
from easycat.tts.deepgram_tts import DeepgramTTS
from easycat.tts.elevenlabs_tts import ElevenLabsTTS
from easycat.tts.input import TTSInput, strip_ssml_tags
from easycat.tts.openai_tts import OpenAITTS

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "14-bring-your-own-agent"


def _load_main_module() -> ModuleType:
    path = CHAPTER / "main.py"
    spec = importlib.util.spec_from_file_location("teaching_ch14_pronunciation", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_chapter_stack_exposes_the_real_ssml_downgrade() -> None:
    chapter = _load_main_module()
    processors = chapter.build_output_processors()

    processed = apply_output_processors(
        TTSInput("Call me at 555-867-5309."),
        processors,
        is_final=True,
        is_streaming=False,
    )
    prepared = TTSInput(strip_ssml_tags(processed.text), format="plain")

    assert [type(processor).__name__ for processor in processors] == [
        "MarkdownStripProcessor",
        "PhoneticReplacementProcessor",
        "PauseProcessor",
    ]
    assert processed.format == "ssml"
    assert '<break time="120ms"/>' in processed.text
    assert "5 5 5 8 6 7 5 3 0 9" in prepared.text
    assert "<break" not in prepared.text


def test_bundled_tts_providers_use_the_plain_text_policy() -> None:
    for provider_type in (OpenAITTS, CartesiaTTS, DeepgramTTS, ElevenLabsTTS):
        assert provider_type.input_policy is TTSBase.input_policy
        assert provider_type.supports_ssml is TTSBase.supports_ssml


def test_printed_command_finds_the_provider_ready_record(tmp_path: Path) -> None:
    chapter = _load_main_module()
    bundle = tmp_path / "path with spaces" / "ch14.bundle"
    bundle.parent.mkdir()
    record = JournalRecord(
        sequence=1,
        session_id="ch14",
        turn_id="turn-1",
        name="tts_payload_prepared",
        data={
            "changed": True,
            "original_format": "plain",
            "prepared_format": "plain",
            "processors": [
                "MarkdownStripProcessor",
                "PhoneticReplacementProcessor",
                "PauseProcessor",
            ],
            "ssml_downgraded": True,
        },
    )
    export_debug_bundle(SimpleNamespace(journal=SimpleNamespace(read=lambda: [record])), bundle)
    _register_commands()

    assert shlex.split(chapter.pronunciation_command(bundle)) == [
        "uv",
        "run",
        "easycat",
        "journal",
        "grep",
        str(chapter._display_path(bundle)),
        "--query",
        "tts_payload_prepared",
        "--json",
    ]
    result = CliRunner().invoke(
        app,
        ["journal", "grep", str(bundle), "--query", "tts_payload_prepared", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["total"] == 1
    assert payload["matches"][0]["name"] == "tts_payload_prepared"
    assert payload["matches"][0]["data"]["ssml_downgraded"] is True


def test_exercise_distinguishes_the_real_scheduler_record() -> None:
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")
    normalized = " ".join(exercises.split())

    assert "not a family of `output_processor.*` records" in normalized
    assert "`tts_payload_prepared`" in normalized
    assert "exact 120 ms timing guarantee is gone" in normalized
