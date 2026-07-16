"""Keep Chapter 1 aligned with the maintained Transport protocol."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "01-echo"


def _load_main():
    path = CHAPTER / "main.py"
    spec = importlib.util.spec_from_file_location("teaching_ch01_echo", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_echo_counts_transport_acceptance() -> None:
    chapter = _load_main()

    class FakeTransport:
        def __init__(self) -> None:
            self.acceptance = iter((True, False, True))

        async def receive_audio(self):
            for frame in ("first", "rejected", "last"):
                yield frame

        async def send_audio(self, _frame) -> bool:
            return next(self.acceptance)

    assert await chapter.echo(FakeTransport()) == (2, 1)


def test_transport_contract_probe_is_device_free_and_executable() -> None:
    result = subprocess.run(
        [sys.executable, str(CHAPTER / "transport_contract_probe.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "accepted": 2,
        "rejected": 1,
        "full_transport": True,
        "legacy_transport_like": True,
        "legacy_full_transport": False,
    }


def test_chapter_names_acceptance_and_full_versioned_contract() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")

    assert "send_audio(self, chunk: AudioChunk) -> bool" in readme
    assert "class Transport(VersionedProvider, Protocol)" in readme
    assert "Acceptance is not playback" in readme
    assert "`True` means accepted, not heard" in exercises
