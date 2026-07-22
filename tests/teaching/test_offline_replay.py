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
