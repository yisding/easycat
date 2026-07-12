"""Keep the first manual STT chapters aligned with provider ownership."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEACHING = ROOT / "docs" / "teaching"
CHAPTER_4 = TEACHING / "04-vad-preroll"


def test_chapter_4_closes_stt_on_normal_and_cancelled_turns() -> None:
    result = subprocess.run(
        [sys.executable, str(CHAPTER_4 / "stt_cleanup_probe.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "normal_turn": {"started": 1, "ended": 1, "closed": 1},
        "cancelled_turn": {"started": 1, "ended": 1, "closed": 1},
    }


def test_chapters_2_through_5_close_manually_created_stt() -> None:
    chapter_2 = TEACHING / "02-transcribe" / "streaming.py"
    source = chapter_2.read_text(encoding="utf-8")
    assert "from easycat.runtime.capabilities import close_if_supported" in source
    assert "resources.push_async_callback(close_if_supported, stt)" in source

    direct_cleanup_paths = (
        TEACHING / "03-parrot-naive" / "main.py",
        TEACHING / "04-vad-preroll" / "main.py",
        TEACHING / "05-blocking-agent" / "main.py",
    )

    for path in direct_cleanup_paths:
        source = path.read_text(encoding="utf-8")
        assert "from easycat.runtime.capabilities import close_if_supported" in source
        assert "await close_if_supported(" in source


def test_chapter_4_names_cleanup_as_distinct_from_stream_end() -> None:
    readme = (CHAPTER_4 / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER_4 / "EXERCISES.md").read_text(encoding="utf-8")

    assert "A VAD turn does not own the provider process" in readme
    assert "normal and cancelled paths" in readme
    assert "ends and closes exactly once" in exercises
