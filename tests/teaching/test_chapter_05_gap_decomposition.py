from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tests.teaching import _script_runner as script_runner

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "docs" / "teaching" / "05-blocking-agent" / "gap_decomposition_probe.py"
_MISSING = object()


def load_probe():
    previous_main = sys.modules.pop("main", _MISSING)
    sys.path.insert(0, str(PROBE.parent))
    try:
        spec = importlib.util.spec_from_file_location("test_chapter_05_gap_probe", PROBE)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(PROBE.parent))
        if previous_main is _MISSING:
            sys.modules.pop("main", None)
        else:
            sys.modules["main"] = previous_main
    return module


def test_gap_decomposition_probe_accounts_for_first_audio_latency() -> None:
    completed = script_runner.run(
        [sys.executable, str(PROBE)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert payload == {
        "agent_ms": 1200.0,
        "components_match_total": True,
        "first_audio_precedes_enqueue_end": True,
        "stt_to_agent_ms": 0.0,
        "total_gap_ms": 1650.0,
        "tts_enqueue_ms": 800.0,
        "tts_to_first_audio_ms": 450.0,
    }


def test_gap_probe_import_does_not_require_or_leak_openai() -> None:
    previous_openai = sys.modules.pop("openai", _MISSING)
    try:
        load_probe()
        assert "openai" not in sys.modules
    finally:
        if previous_openai is not _MISSING:
            sys.modules["openai"] = previous_openai


async def test_gap_probe_restores_chapter_globals_on_success_and_failure(monkeypatch) -> None:
    probe = load_probe()
    chapter = probe.chapter
    originals = (chapter.time.monotonic, chapter.blocking_agent, chapter.speak)

    await probe.probe()
    assert (chapter.time.monotonic, chapter.blocking_agent, chapter.speak) == originals

    async def fail_run_turn(*_args, **_kwargs) -> None:
        raise RuntimeError("scripted failure")

    monkeypatch.setattr(chapter, "run_turn", fail_run_turn)
    with pytest.raises(RuntimeError, match="scripted failure"):
        await probe.probe()
    assert (chapter.time.monotonic, chapter.blocking_agent, chapter.speak) == originals
