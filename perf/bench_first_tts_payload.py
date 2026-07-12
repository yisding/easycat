#!/usr/bin/env python3
"""Benchmark first TTS payload dispatch for streamed model text.

This benchmark isolates EasyCat's text-aggregation contribution to time to
first audio.  It replays word-sized model deltas at a fixed cadence and
compares the former sentence-only fallback with the current bounded first
payload policy.  Provider/network latency is deliberately excluded.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from easycat.session.text import (
    _split_first_phrase,
    split_at_sentence_boundaries,
    split_first_clause,
)

_DEFAULT_TEXT = (
    "I can help investigate the account activity and identify the most likely cause "
    "before we decide what action to take next"
)


def _word_deltas(text: str) -> list[str]:
    return re.findall(r"\S+\s*", text)


def _bounded_first_payload(text: str) -> tuple[str, str]:
    ready, remaining = split_first_clause(text)
    if ready:
        return ready, remaining
    ready, remaining = _split_first_phrase(text)
    if ready:
        return ready, remaining
    return split_at_sentence_boundaries(text)


def _first_dispatch(
    deltas: list[str],
    splitter: Callable[[str], tuple[str, str]],
    *,
    delta_ms: float,
) -> dict[str, Any]:
    pending = ""
    for index, delta in enumerate(deltas):
        pending += delta
        ready, _ = splitter(pending)
        if ready:
            return {
                "delta_index": index,
                "latency_ms": index * delta_ms,
                "payload_chars": len(ready),
                "payload_text": ready,
            }

    # A clean model-stream completion flushes the pending text even when no
    # sentence boundary was observed.
    return {
        "delta_index": len(deltas) - 1,
        "latency_ms": max(0, len(deltas) - 1) * delta_ms,
        "payload_chars": len(pending),
        "payload_text": pending,
    }


def compare(text: str, *, delta_ms: float) -> dict[str, Any]:
    """Return sentence-only and bounded-first-payload dispatch measurements."""
    deltas = _word_deltas(text)
    if not deltas:
        raise ValueError("benchmark text must contain at least one non-whitespace character")
    sentence = _first_dispatch(deltas, split_at_sentence_boundaries, delta_ms=delta_ms)
    bounded = _first_dispatch(deltas, _bounded_first_payload, delta_ms=delta_ms)
    saved_ms = float(sentence["latency_ms"]) - float(bounded["latency_ms"])
    baseline_ms = float(sentence["latency_ms"])
    return {
        "schema_version": 1,
        "delta_ms": delta_ms,
        "delta_count": len(deltas),
        "text_chars": len(text),
        "sentence_only": sentence,
        "bounded_first_payload": bounded,
        "saved_ms": saved_ms,
        "reduction_percent": (saved_ms / baseline_ms * 100.0) if baseline_ms > 0 else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", default=_DEFAULT_TEXT)
    parser.add_argument("--delta-ms", type=float, default=40.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.delta_ms < 0:
        parser.error("--delta-ms must be non-negative")

    payload = compare(args.text, delta_ms=args.delta_ms)
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
