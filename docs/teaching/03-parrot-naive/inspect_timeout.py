"""Explain each naive silence-timeout fire in a Chapter 3 bundle."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from easycat.debug.testing import load_bundle

STT_RECORD_NAMES = frozenset({"stt.partial", "stt.final"})


def _offset_ms(record: dict[str, Any]) -> float | None:
    value = (record.get("data") or {}).get("offset_ms")
    return float(value) if isinstance(value, (int, float)) else None


def _summary(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    data = record.get("data") or {}
    return {
        "sequence": record.get("sequence"),
        "name": record.get("name"),
        "offset_ms": _offset_ms(record),
        "text": data.get("text") or data.get("committed_text"),
    }


def analyze_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the trigger and timing evidence for every ``parrot.fire``."""
    ordered = list(records)
    analyses: list[dict[str, Any]] = []

    for index, fire in enumerate(ordered):
        if fire.get("name") != "parrot.fire":
            continue

        trigger = next(
            (
                record
                for record in reversed(ordered[:index])
                if record.get("name") in STT_RECORD_NAMES
            ),
            None,
        )
        next_partial = next(
            (record for record in ordered[index + 1 :] if record.get("name") == "stt.partial"),
            None,
        )
        fire_offset = _offset_ms(fire)
        trigger_offset = _offset_ms(trigger) if trigger is not None else None
        next_offset = _offset_ms(next_partial) if next_partial is not None else None
        timeout_s = (fire.get("data") or {}).get("silence_timeout_s")
        timeout_ms = float(timeout_s) * 1000 if isinstance(timeout_s, (int, float)) else None
        observed_silence_ms = (
            fire_offset - trigger_offset
            if fire_offset is not None and trigger_offset is not None
            else None
        )

        analyses.append(
            {
                "fire": _summary(fire),
                "trigger_record": _summary(trigger),
                "next_partial": _summary(next_partial),
                "configured_timeout_ms": timeout_ms,
                "observed_silence_ms": observed_silence_ms,
                "scheduler_overshoot_ms": (
                    observed_silence_ms - timeout_ms
                    if observed_silence_ms is not None and timeout_ms is not None
                    else None
                ),
                "post_fire_consumer_gap_ms": (
                    next_offset - fire_offset
                    if next_offset is not None and fire_offset is not None
                    else None
                ),
            }
        )

    return analyses


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="Chapter 3 .bundle path")
    args = parser.parse_args(argv)

    bundle = load_bundle(args.bundle)
    analyses = analyze_records(bundle.records())
    print(json.dumps({"bundle": str(args.bundle), "fires": analyses}, indent=2, sort_keys=True))
    return 0 if analyses else 1


if __name__ == "__main__":
    raise SystemExit(main())
