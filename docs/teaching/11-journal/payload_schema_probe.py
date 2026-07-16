"""Show where journal typing ends and emitter-owned validation begins.

Run from the repository root:

    uv run python docs/teaching/11-journal/payload_schema_probe.py

No providers, devices, or API keys are required.
"""

from __future__ import annotations

import json
import math

from easycat.runtime import InMemoryRingBuffer, JournalRecord, JournalRecordKind


def require_finite_number(record: JournalRecord, field: str) -> float:
    """Validate one emitter-defined numeric payload field."""
    value = record.data.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(
            f"{record.name}.data[{field!r}] must be a finite int or float; "
            f"got {type(value).__name__}"
        )
    return float(value)


def probe() -> dict[str, object]:
    journal = InMemoryRingBuffer(capacity=10)
    for value in (125.0, "125.0"):
        journal.append(
            kind=JournalRecordKind.METRIC,
            name="demo.latency",
            session_id="schema-demo",
            data={"t_ms": value},
        )

    valid, invalid = journal.read()
    invalid_error = ""
    try:
        require_finite_number(invalid, "t_ms")
    except ValueError as exc:
        invalid_error = str(exc)

    return {
        "envelope": {
            "data_type": type(valid.data).__name__,
            "kind": valid.kind.value,
            "name": valid.name,
            "record_type": type(valid).__name__,
            "sequence": valid.sequence,
            "session_id": valid.session_id,
        },
        "unchecked_payload": {
            "python_type": type(invalid.data["t_ms"]).__name__,
            "value": invalid.data["t_ms"],
        },
        "validation": {
            "invalid_error": invalid_error,
            "valid_t_ms": require_finite_number(valid, "t_ms"),
        },
    }


if __name__ == "__main__":
    print(json.dumps(probe(), indent=2, sort_keys=True))
