"""Exercise eval-set coverage failures without loading provider data.

Run with::

    uv run python docs/teaching/12-evals-and-latency/coverage_probe.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def load_evals():
    path = Path(__file__).with_name("evals.py")
    spec = importlib.util.spec_from_file_location("teaching_ch12_coverage_evals", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def error_from(call) -> str | None:
    try:
        call()
    except ValueError as exc:
        return str(exc)
    return None


def main() -> None:
    evals = load_evals()
    bundles = [Path("turn_a.bundle"), Path("turn_b.bundle")]
    exact_rows = {
        "turn_a.bundle": {},
        "turn_b.bundle": {},
    }
    one_turn = [
        {"name": "stt.final", "data": {"text": "hello"}},
        {"name": "turn.gap", "data": {"total_gap_ms": 800}},
    ]
    missing_gap = one_turn[:1]
    multi_turn = [one_turn[0], one_turn[0], one_turn[1]]

    payload = {
        "exact_manifest": error_from(lambda: evals._validate_coverage(bundles, exact_rows)),
        "missing_label": error_from(
            lambda: evals._validate_coverage(bundles, {"turn_a.bundle": {}})
        ),
        "missing_turn_gap": error_from(
            lambda: evals._stats_from_records(missing_gap, bundle_name="turn_a.bundle")
        ),
        "multi_turn_bundle": error_from(
            lambda: evals._stats_from_records(multi_turn, bundle_name="turn_a.bundle")
        ),
        "one_turn_stats": evals._stats_from_records(one_turn, bundle_name="turn_a.bundle"),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
