"""Show why one fixed silence timeout cannot optimize both turn splits and latency.

Run with::

    uv run python docs/teaching/03-parrot-naive/timeout_policy_probe.py
"""

from __future__ import annotations

import json

from inspect_timeout import analyze_records


def _scenario(*, timeout_ms: float, next_word_at_ms: float | None) -> dict[str, object]:
    trigger_at_ms = 100.0
    fire_at_ms = trigger_at_ms + timeout_ms + 5.0
    records: list[dict[str, object]] = [
        {
            "sequence": 1,
            "name": "stt.partial",
            "data": {"text": "the capital is", "offset_ms": trigger_at_ms},
        },
        {
            "sequence": 2,
            "name": "parrot.fire",
            "data": {
                "committed_text": "the capital is",
                "silence_timeout_s": timeout_ms / 1000,
                "offset_ms": fire_at_ms,
            },
        },
    ]
    if next_word_at_ms is not None:
        records.extend(
            (
                {
                    "sequence": 3,
                    "name": "stt.received",
                    "data": {
                        "event_id": 2,
                        "text": "the capital is Paris",
                        "offset_ms": next_word_at_ms,
                    },
                },
                {
                    "sequence": 4,
                    "name": "stt.partial",
                    "data": {
                        "event_id": 2,
                        "text": "the capital is Paris",
                        "offset_ms": next_word_at_ms,
                        "received_offset_ms": next_word_at_ms,
                    },
                },
            )
        )

    analysis = analyze_records(records)[0]
    return {
        "configured_timeout_ms": analysis["configured_timeout_ms"],
        "observed_silence_ms": analysis["observed_silence_ms"],
        "next_word_after_fire_ms": analysis["post_fire_ingress_gap_ms"],
    }


def probe() -> dict[str, object]:
    short = _scenario(timeout_ms=500.0, next_word_at_ms=650.0)
    long = _scenario(timeout_ms=2000.0, next_word_at_ms=None)
    return {
        "short_timeout": {**short, "outcome": "splits_before_next_word"},
        "long_timeout": {**long, "outcome": "adds_commit_latency"},
        "one_timeout_cannot_optimize_both": True,
    }


if __name__ == "__main__":
    print(json.dumps(probe(), indent=2, sort_keys=True))
