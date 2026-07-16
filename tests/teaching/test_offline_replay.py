from __future__ import annotations

import ast
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEACHING = ROOT / "docs" / "teaching"
CHAPTER = TEACHING / "10-cleaning-signal"
MIC_PATH = "docs/teaching/10-cleaning-signal/recordings/speakerphone_loop.mic.wav"
REF_PATH = "docs/teaching/10-cleaning-signal/recordings/speakerphone_loop.ref.wav"


def test_chapter_10_offline_replay_paths_resolve_from_the_repository_root() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    replay_source = (CHAPTER / "replay.py").read_text(encoding="utf-8")
    replay_doc = ast.get_docstring(ast.parse(replay_source)) or ""

    for relative_path in (MIC_PATH, REF_PATH):
        path = ROOT / relative_path
        assert relative_path in readme
        assert relative_path in replay_doc
        assert path.is_file()
        with wave.open(str(path), "rb") as audio:
            assert audio.getnframes() > 0
            assert audio.getsampwidth() == 2

    assert "--mic recordings/" not in readme
    assert "--ref recordings/" not in readme
    assert "--mic recordings/" not in replay_doc
    assert "--ref recordings/" not in replay_doc


def test_chapter_10_offline_replay_is_a_no_hardware_starting_point() -> None:
    overview = (TEACHING / "README.md").read_text(encoding="utf-8")
    starting_points = overview.split("## Choose a starting point", 1)[1].split("## The ladder", 1)[
        0
    ]
    prerequisites = (
        (CHAPTER / "README.md")
        .read_text(encoding="utf-8")
        .split("## Prerequisites", 1)[1]
        .split("## Diff from", 1)[0]
    )
    normalized_starting_points = " ".join(starting_points.split())
    normalized_prerequisites = " ".join(prerequisites.split())

    assert "No mic or API keys" in normalized_starting_points
    assert "[`10-cleaning-signal`](./10-cleaning-signal/) offline replay" in starting_points
    assert "checked-in WAV pairs" in normalized_starting_points
    assert "For offline replay only" in normalized_prerequisites
    assert "need no microphone or API keys" in normalized_prerequisites
