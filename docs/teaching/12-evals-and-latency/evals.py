"""Chapter 12 — aggregate WER, barge-in F1, and latency percentiles.

    uv run python docs/teaching/12-evals-and-latency/evals.py \\
        docs/teaching/12-evals-and-latency/bundles/ \\
        docs/teaching/12-evals-and-latency/ground_truth.csv

Inputs:
- A directory of ``*.bundle`` fixtures.
- A ground-truth CSV mapping each bundle name to:
    reference_transcript  — the words the user actually said
    had_real_barge_in     — "1" if the interruption was intentional

Outputs (stdout):
- Coverage counts after validating a one-bundle/one-turn manifest.
- Per-bundle first-audio ``turn.gap`` ms, sorted.
- P50 and P95 across the set.
- WER aggregated across bundles with a reference transcript.
- Barge-in F1 over the {had_real_barge_in, observed_interruption} matrix.

The command fails closed when bundle/label coverage is incomplete or a
fixture contains zero/multiple measured turns. Silent exclusions make
point estimates look healthier, so this teaching evaluator forbids them.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from easycat.debug.testing import load_bundle
from easycat.validation.latency import LatencyPercentileStats

REQUIRED_COLUMNS = {"bundle", "reference_transcript", "had_real_barge_in"}


def _wer_words(ref: str, hyp: str) -> tuple[int, int]:
    """Return (total_edits, reference_words).

    Standard Levenshtein distance over word tokens. No normalization
    (deliberately — the reader should see that punctuation and case
    contribute to WER until they add their own canonicaliser).
    """
    r = ref.split()
    h = hyp.split()
    n, m = len(r), len(h)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if r[i - 1] == h[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = 1 + min(
                    dp[i - 1][j - 1],  # substitute
                    dp[i - 1][j],  # delete
                    dp[i][j - 1],  # insert
                )
    return dp[n][m], n


def _stats_from_records(records, *, bundle_name: str) -> dict:
    """Validate and measure one single-turn teaching fixture."""
    hypotheses = []
    gaps = []
    saw_interruption = False
    for record in records:
        if record["name"] == "stt.final":
            hypotheses.append(record["data"].get("text", ""))
        elif record["name"] == "turn.gap":
            gaps.append(record["data"].get("total_gap_ms"))
        elif record["name"] == "interruption.start":
            saw_interruption = True

    if len(hypotheses) != 1:
        raise ValueError(
            f"{bundle_name}: expected exactly one stt.final, found {len(hypotheses)}; "
            "split multi-turn runs into one labeled fixture per turn"
        )
    if len(gaps) != 1:
        raise ValueError(
            f"{bundle_name}: expected exactly one turn.gap, found {len(gaps)}; "
            "missing first-audio turns must not disappear from latency coverage"
        )
    gap = gaps[0]
    hypothesis = hypotheses[0]
    if not isinstance(hypothesis, str):
        raise ValueError(f"{bundle_name}: stt.final.text must be a string")
    if isinstance(gap, bool) or not isinstance(gap, (int, float)) or gap < 0:
        raise ValueError(f"{bundle_name}: turn.gap.total_gap_ms must be a non-negative number")

    return {
        "hypothesis": hypothesis,
        "total_gap_ms": float(gap),
        "observed_interruption": saw_interruption,
    }


def _bundle_stats(path: Path) -> dict:
    bundle = load_bundle(path)
    return _stats_from_records(bundle.records(), bundle_name=path.name)


def _load_ground_truth(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing_columns = REQUIRED_COLUMNS - set(reader.fieldnames or ())
        if missing_columns:
            raise ValueError(f"ground truth is missing columns: {sorted(missing_columns)}")

        rows: dict[str, dict[str, str]] = {}
        for line_number, row in enumerate(reader, start=2):
            name = (row.get("bundle") or "").strip()
            if not name:
                raise ValueError(f"ground truth line {line_number}: bundle is empty")
            if name in rows:
                raise ValueError(f"ground truth line {line_number}: duplicate bundle {name!r}")
            label = (row.get("had_real_barge_in") or "").strip()
            if label not in {"0", "1"}:
                raise ValueError(
                    f"ground truth line {line_number}: had_real_barge_in must be 0 or 1"
                )
            if not (row.get("reference_transcript") or "").strip():
                raise ValueError(f"ground truth line {line_number}: reference_transcript is empty")
            row["bundle"] = name
            row["had_real_barge_in"] = label
            rows[name] = row
    return rows


def _validate_coverage(bundles: list[Path], rows: dict[str, dict[str, str]]) -> None:
    bundle_names = {path.name for path in bundles}
    row_names = set(rows)
    missing_labels = sorted(bundle_names - row_names)
    stale_labels = sorted(row_names - bundle_names)
    if missing_labels or stale_labels:
        details = []
        if missing_labels:
            details.append(f"missing labels for {missing_labels}")
        if stale_labels:
            details.append(f"labels without bundles {stale_labels}")
        raise ValueError("coverage mismatch: " + "; ".join(details))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("bundles_dir", type=Path)
    ap.add_argument("ground_truth_csv", type=Path)
    args = ap.parse_args()

    if not args.bundles_dir.is_dir():
        sys.exit(f"{args.bundles_dir} is not a directory.")
    if not args.ground_truth_csv.exists():
        sys.exit(f"{args.ground_truth_csv} does not exist.")

    bundles = sorted(args.bundles_dir.glob("*.bundle"))
    if not bundles:
        sys.exit("No bundles found.")
    try:
        rows = _load_ground_truth(args.ground_truth_csv)
        _validate_coverage(bundles, rows)
        stats = {bundle.name: _bundle_stats(bundle) for bundle in bundles}
    except ValueError as exc:
        sys.exit(f"Invalid eval set: {exc}")

    print("=== Coverage ===")
    print(
        f"  bundles={len(bundles)}  labels={len(rows)}  "
        f"latency={len(stats)}  WER={len(stats)}  barge-in={len(stats)}"
    )

    # Latency per-bundle.
    print("\n=== Per-bundle first-audio latency (turn.gap ms) ===")
    lat_ms = []
    for b in bundles:
        s = stats[b.name]
        val = s["total_gap_ms"]
        lat_ms.append(val)
        print(f"  {b.name:38}  {val:>6.0f} ms")
    lat_ms.sort()
    latency = LatencyPercentileStats.from_values(lat_ms)
    assert latency.p50 is not None and latency.p95 is not None
    p50 = latency.p50
    p95 = latency.p95
    ratio = p95 / p50 if p50 else float("inf")
    print(f"  {'P50':38}  {p50:>6.0f} ms")
    print(f"  {'P95':38}  {p95:>6.0f} ms")
    print(f"  {'P95 / P50 ratio':38}  {ratio:>6.2f}")

    # WER aggregated.
    print("\n=== WER ===")
    total_edits = 0
    total_ref_words = 0
    for b in bundles:
        gt = rows[b.name]
        s = stats[b.name]
        edits, n_ref = _wer_words(gt["reference_transcript"], s["hypothesis"])
        total_edits += edits
        total_ref_words += n_ref
        per = (edits / n_ref) if n_ref else 0.0
        print(f"  {b.name:38}  edits={edits:>2}  ref_words={n_ref:>3}  WER={per * 100:>5.1f}%")
    if total_ref_words:
        agg = total_edits / total_ref_words
        print(f"  {'aggregate':38}  WER={agg * 100:>5.1f}%")

    # Barge-in F1.
    print("\n=== Barge-in F1 ===")
    tp = fp = fn = tn = 0
    for b in bundles:
        gt = rows[b.name]
        s = stats[b.name]
        real = gt["had_real_barge_in"] == "1"
        observed = s["observed_interruption"]
        if real and observed:
            tp += 1
        elif not real and observed:
            fp += 1
        elif real and not observed:
            fn += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    f1 = None
    if precision is not None and recall is not None:
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    def display(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.2f}"

    print(f"  TP={tp}  FP={fp}  FN={fn}  TN={tn}")
    print(f"  precision = {display(precision)}   recall = {display(recall)}   F1 = {display(f1)}")


if __name__ == "__main__":
    main()
