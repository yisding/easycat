"""Expose which Chapter 12 fixture controls the small-sample P95.

Run with::

    uv run python docs/teaching/12-evals-and-latency/p95_sensitivity_probe.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def load_evals():
    path = Path(__file__).with_name("evals.py")
    spec = importlib.util.spec_from_file_location("teaching_ch12_p95_sensitivity", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def probe() -> dict[str, object]:
    evals = load_evals()
    bundles_dir = Path(__file__).parent / "bundles"
    samples = {
        path.name: evals._bundle_stats(path)["total_gap_ms"]
        for path in sorted(bundles_dir.glob("*.bundle"))
    }
    return evals.p95_sensitivity(samples)


if __name__ == "__main__":
    print(json.dumps(probe(), indent=2, sort_keys=True))
