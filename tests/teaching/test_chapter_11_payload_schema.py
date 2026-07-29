"""Keep Chapter 11 honest about journal and emitter schema ownership."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from easycat.runtime import JournalRecord
from tests.teaching import _script_runner as script_runner

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "11-journal"
PROBE = CHAPTER / "payload_schema_probe.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location("teaching_ch11_payload_schema", PROBE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_payload_schema_probe_preserves_then_rejects_unchecked_type() -> None:
    completed = script_runner.run(
        [sys.executable, str(PROBE)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "envelope": {
            "data_type": "dict",
            "kind": "metric",
            "name": "demo.latency",
            "record_type": "JournalRecord",
            "sequence": 1,
            "session_id": "schema-demo",
        },
        "unchecked_payload": {"python_type": "str", "value": "125.0"},
        "validation": {
            "invalid_error": ("demo.latency.data['t_ms'] must be a finite int or float; got str"),
            "valid_t_ms": 125.0,
        },
    }


@pytest.mark.parametrize(
    ("value", "type_name"),
    ((True, "bool"), (float("inf"), "float"), (float("nan"), "float"), (None, "NoneType")),
)
def test_emitter_validator_rejects_non_metric_values(value: object, type_name: str) -> None:
    chapter = _load_probe()
    record = JournalRecord(
        sequence=1,
        session_id="schema-demo",
        name="demo.latency",
        data={"t_ms": value},
    )

    with pytest.raises(
        ValueError,
        match=rf"must be a finite int or float; got {type_name}",
    ):
        chapter.require_finite_number(record, "t_ms")
