"""Decompose Chapter 6 bundle latency through first accepted audio."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from easycat.debug.testing import load_bundle


def _timestamp_ms(record: dict[str, Any]) -> float | None:
    value = (record.get("data") or {}).get("t_ms")
    return float(value) if isinstance(value, (int, float)) else None


def _finish_turn(turn: dict[str, Any]) -> dict[str, Any]:
    stt_final_ms = turn.pop("_stt_final_ms")
    first_token_ms = turn.pop("_first_token_ms")
    first_audio_ms = turn.pop("_first_audio_ms")
    turn["stt_final_to_first_token_ms"] = (
        first_token_ms - stt_final_ms
        if first_token_ms is not None and stt_final_ms is not None
        else None
    )
    turn["first_token_to_first_audio_ms"] = (
        first_audio_ms - first_token_ms
        if first_audio_ms is not None and first_token_ms is not None
        else None
    )
    turn["stt_final_to_first_audio_ms"] = (
        first_audio_ms - stt_final_ms
        if first_audio_ms is not None and stt_final_ms is not None
        else None
    )
    return turn


def measure_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Measure each turn delimited by a Chapter 6 ``stt.final`` record."""
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for record in records:
        name = record.get("name")
        data = record.get("data") or {}
        if name == "stt.final":
            if current is not None:
                turns.append(_finish_turn(current))
            current = {
                "stt_final_sequence": record.get("sequence"),
                "text": data.get("text"),
                "sentence_tts_ms": [],
                "_stt_final_ms": _timestamp_ms(record),
                "_first_token_ms": None,
                "_first_audio_ms": None,
            }
            continue

        if current is None:
            continue
        if name == "agent.first_token" and current["_first_token_ms"] is None:
            current["_first_token_ms"] = _timestamp_ms(record)
        elif name == "tts.first_audio" and current["_first_audio_ms"] is None:
            current["_first_audio_ms"] = _timestamp_ms(record)
        elif name == "stage.tts.execute":
            elapsed_ms = data.get("elapsed_ms")
            if isinstance(elapsed_ms, (int, float)):
                current["sentence_tts_ms"].append(float(elapsed_ms))

    if current is not None:
        turns.append(_finish_turn(current))
    return turns


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="Chapter 6 .bundle path")
    args = parser.parse_args(argv)

    bundle = load_bundle(args.bundle)
    turns = measure_records(bundle.records())
    print(json.dumps({"bundle": str(args.bundle), "turns": turns}, indent=2, sort_keys=True))
    return 0 if turns else 1


if __name__ == "__main__":
    raise SystemExit(main())
