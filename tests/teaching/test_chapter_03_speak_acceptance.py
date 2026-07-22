"""Keep Chapter 3 aligned with transport acceptance semantics."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from easycat.runtime import InMemoryRingBuffer

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "03-parrot-naive"


def _load_main():
    path = CHAPTER / "main.py"
    spec = importlib.util.spec_from_file_location("teaching_ch03_delivery", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_speak_acceptance_probe_is_provider_free_and_executable() -> None:
    result = subprocess.run(
        [sys.executable, str(CHAPTER / "speak_acceptance_probe.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "produced_chunks": 3,
        "accepted_chunks": 2,
        "rejected_chunks": 1,
    }


def test_parrot_delivery_record_preserves_rejections(capsys) -> None:
    chapter = _load_main()
    journal = InMemoryRingBuffer(capacity=10)

    chapter.record_delivery(
        journal,
        text="hello",
        accepted_chunks=2,
        rejected_chunks=1,
        offset_ms=750.0,
    )

    record = journal.read()[0]
    assert record.name == "parrot.delivery"
    assert record.data == {
        "stage": "parrot",
        "committed_text": "hello",
        "accepted_chunks": 2,
        "rejected_chunks": 1,
        "offset_ms": 750.0,
    }
    assert "transport rejected 1/3 audio chunks" in capsys.readouterr().out
