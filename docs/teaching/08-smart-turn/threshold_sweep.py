"""Re-score Chapter 8 smart-turn records at two decision thresholds."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from easycat.debug.testing import load_bundle


def _threshold(value: str) -> float:
    parsed = float(value)
    if not 0 <= parsed <= 1:
        raise argparse.ArgumentTypeError("threshold must be between 0 and 1")
    return parsed


def load_labels(path: Path | None) -> dict[int, bool] | None:
    """Load ``{sequence: user_was_done}`` labels when supplied."""
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or any(
        not isinstance(value, bool) for value in payload.values()
    ):
        raise SystemExit("labels must be a JSON object mapping record sequence to true/false")
    try:
        return {int(sequence): value for sequence, value in payload.items()}
    except (TypeError, ValueError) as exc:
        raise SystemExit("label keys must be integer record sequences") from exc


def _confusion(rows: list[dict[str, Any]], decision_field: str) -> dict[str, int]:
    counts = {"true_positive": 0, "false_positive": 0, "true_negative": 0, "false_negative": 0}
    for row in rows:
        label = row["user_was_done"]
        if label is None:
            continue
        accepted = row[decision_field]
        if accepted and label:
            counts["true_positive"] += 1
        elif accepted:
            counts["false_positive"] += 1
        elif label:
            counts["false_negative"] += 1
        else:
            counts["true_negative"] += 1
    return counts


def sweep_records(
    records: Iterable[dict[str, Any]],
    *,
    baseline: float,
    candidate: float,
    labels: dict[int, bool] | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for record in records:
        if record.get("name") != "smart_turn.classify":
            continue
        data = record.get("data") or {}
        probability = data.get("probability")
        sequence = record.get("sequence")
        if not isinstance(probability, (int, float)) or not isinstance(sequence, int):
            continue
        baseline_accepts = probability > baseline
        candidate_accepts = probability > candidate
        rows.append(
            {
                "sequence": sequence,
                "probability": float(probability),
                "recorded_confirmed": data.get("confirmed"),
                "baseline_accepts": baseline_accepts,
                "candidate_accepts": candidate_accepts,
                "newly_accepted": candidate_accepts and not baseline_accepts,
                "user_was_done": labels.get(sequence) if labels is not None else None,
            }
        )

    if labels is not None:
        expected_sequences = {row["sequence"] for row in rows}
        provided_sequences = set(labels)
        missing = sorted(expected_sequences - provided_sequences)
        unknown = sorted(provided_sequences - expected_sequences)
        if missing or unknown:
            details = []
            if missing:
                details.append(f"missing sequences {missing}")
            if unknown:
                details.append(f"unknown sequences {unknown}")
            raise ValueError("labels must exactly cover classifications: " + "; ".join(details))

    labeled_count = len(rows) if labels is not None else 0
    return {
        "baseline_threshold": baseline,
        "candidate_threshold": candidate,
        "classification_count": len(rows),
        "newly_accepted_count": sum(row["newly_accepted"] for row in rows),
        "labeled_count": labeled_count,
        "classifications": rows,
        "metrics": (
            {
                "baseline": _confusion(rows, "baseline_accepts"),
                "candidate": _confusion(rows, "candidate_accepts"),
            }
            if labels is not None
            else None
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--baseline", type=_threshold, default=0.5)
    parser.add_argument("--candidate", type=_threshold, default=0.3)
    parser.add_argument(
        "--labels",
        type=Path,
        help="JSON object mapping smart_turn.classify sequence to true when the user was done",
    )
    args = parser.parse_args(argv)

    bundle = load_bundle(args.bundle)
    try:
        report = sweep_records(
            bundle.records(),
            baseline=args.baseline,
            candidate=args.candidate,
            labels=load_labels(args.labels),
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["classification_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
