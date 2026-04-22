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
- Per-bundle turn.gap ms, sorted.
- P50 and P95 across the set.
- WER aggregated across bundles with a reference transcript.
- Barge-in F1 over the {had_real_barge_in, observed_interruption} matrix.
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from pathlib import Path

from easycat import load_bundle


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


def _bundle_stats(path: Path) -> dict:
    bundle = load_bundle(path)
    hyp_text = ""
    total_gap_ms: float | None = None
    saw_interruption = False
    for r in bundle.records():
        if r["name"] == "stt.final":
            hyp_text = r["data"].get("text", "") or hyp_text
        elif r["name"] == "turn.gap":
            total_gap_ms = r["data"].get("total_gap_ms")
        elif r["name"] == "interruption.start":
            saw_interruption = True
    return {
        "hypothesis": hyp_text,
        "total_gap_ms": total_gap_ms,
        "observed_interruption": saw_interruption,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("bundles_dir", type=Path)
    ap.add_argument("ground_truth_csv", type=Path)
    args = ap.parse_args()

    if not args.bundles_dir.is_dir():
        sys.exit(f"{args.bundles_dir} is not a directory.")
    if not args.ground_truth_csv.exists():
        sys.exit(f"{args.ground_truth_csv} does not exist.")

    rows = {r["bundle"]: r for r in csv.DictReader(args.ground_truth_csv.open())}
    bundles = sorted(args.bundles_dir.glob("*.bundle"))
    if not bundles:
        sys.exit("No bundles found.")

    # Latency per-bundle.
    print("=== Per-bundle latency (turn.gap ms) ===")
    lat_ms = []
    for b in bundles:
        s = _bundle_stats(b)
        val = s["total_gap_ms"]
        if val is not None:
            lat_ms.append(val)
            print(f"  {b.name:38}  {val:>6.0f} ms")
    if lat_ms:
        lat_ms.sort()
        p50 = statistics.median(lat_ms)
        p95 = lat_ms[max(0, int(0.95 * len(lat_ms)) - 1)] if len(lat_ms) > 1 else lat_ms[0]
        print(f"  {'P50':38}  {p50:>6.0f} ms")
        print(f"  {'P95':38}  {p95:>6.0f} ms")
        print(f"  {'P95 / P50 ratio':38}  {p95 / p50:>6.2f}")

    # WER aggregated.
    print("\n=== WER ===")
    total_edits = 0
    total_ref_words = 0
    for b in bundles:
        gt = rows.get(b.name)
        if gt is None:
            continue
        s = _bundle_stats(b)
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
        gt = rows.get(b.name)
        if gt is None:
            continue
        s = _bundle_stats(b)
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
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    print(f"  TP={tp}  FP={fp}  FN={fn}  TN={tn}")
    print(f"  precision = {precision:.2f}   recall = {recall:.2f}   F1 = {f1:.2f}")


if __name__ == "__main__":
    main()
