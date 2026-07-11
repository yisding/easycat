#!/usr/bin/env python3
"""Benchmark EasyCat smart-turn inference, optionally against Pipecat.

The comparison is unusually controlled: both frameworks bundle the same
``smart-turn-v3.2-cpu.onnx`` model.  Each worker runs in a fresh process so
one framework's NumPy/ONNX thread pools cannot warm or contend with the
other's.

Example (the Python environment must contain NumPy + ONNX Runtime)::

    uv run --extra smart-turn python perf/bench_smart_turn.py

To compare a Pipecat installation in an isolated environment::

    uv run --extra smart-turn python perf/bench_smart_turn.py \
      --pipecat-python /tmp/pipecat-venv/bin/python --require-faster
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_RESULT_PREFIX = "EASYCAT_SMART_TURN_RESULT="
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _percentile(samples: list[float], percentile: float) -> float:
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return ordered[index]


def summarize_samples(samples_ms: list[float]) -> dict[str, float | int]:
    """Return stable latency statistics for one framework worker."""
    if not samples_ms:
        raise ValueError("at least one latency sample is required")
    return {
        "runs": len(samples_ms),
        "p50_ms": round(statistics.median(samples_ms), 3),
        "p90_ms": round(_percentile(samples_ms, 0.90), 3),
        "p99_ms": round(_percentile(samples_ms, 0.99), 3),
        "mean_ms": round(statistics.mean(samples_ms), 3),
        "min_ms": round(min(samples_ms), 3),
        "max_ms": round(max(samples_ms), 3),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _measure(call: Any, *, warmup: int, runs: int) -> list[float]:
    for _ in range(warmup):
        call()
    samples: list[float] = []
    for _ in range(runs):
        started = time.perf_counter_ns()
        call()
        samples.append((time.perf_counter_ns() - started) / 1_000_000.0)
    return samples


def _easycat_worker(*, warmup: int, runs: int) -> dict[str, Any]:
    import numpy as np

    from easycat.audio_format import PCM16_MONO_16K, AudioChunk
    from easycat.smart_turn import _BUNDLED_MODEL, SmartTurnONNX

    provider = SmartTurnONNX(_BUNDLED_MODEL)
    provider._ensure_loaded()
    audio = AudioChunk(
        data=np.zeros(8 * 16000, dtype=np.int16).tobytes(),
        format=PCM16_MONO_16K,
    )
    samples = _measure(lambda: provider._detect_sync([audio]), warmup=warmup, runs=runs)
    return {
        "framework": "easycat",
        "model_sha256": _sha256(Path(_BUNDLED_MODEL)),
        "intra_op_threads": provider._session.get_session_options().intra_op_num_threads,
        **summarize_samples(samples),
    }


def _pipecat_worker(*, warmup: int, runs: int) -> dict[str, Any]:
    import importlib.resources

    import numpy as np
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

    provider = LocalSmartTurnAnalyzerV3(sample_rate=16000)
    audio = np.zeros(8 * 16000, dtype=np.float32)
    samples = _measure(lambda: provider._predict_endpoint(audio), warmup=warmup, runs=runs)
    model = importlib.resources.files("pipecat.audio.turn.smart_turn.data").joinpath(
        "smart-turn-v3.2-cpu.onnx"
    )
    with importlib.resources.as_file(model) as model_path:
        model_sha = _sha256(model_path)
    return {
        "framework": "pipecat",
        "model_sha256": model_sha,
        "intra_op_threads": provider._session.get_session_options().intra_op_num_threads,
        **summarize_samples(samples),
    }


def compare_results(easycat: dict[str, Any], pipecat: dict[str, Any]) -> dict[str, Any]:
    """Compare same-model results and quantify EasyCat's latency advantage."""
    models_identical = easycat["model_sha256"] == pipecat["model_sha256"]

    def improvement(metric: str) -> float:
        competitor = float(pipecat[metric])
        return round((competitor - float(easycat[metric])) / competitor * 100.0, 3)

    p50_improvement = improvement("p50_ms")
    p90_improvement = improvement("p90_ms")
    return {
        "models_identical": models_identical,
        "easycat_faster": models_identical and p50_improvement > 0 and p90_improvement > 0,
        "easycat_p50_improvement_percent": p50_improvement,
        "easycat_p90_improvement_percent": p90_improvement,
    }


def _run_worker(
    framework: str,
    *,
    python: str,
    warmup: int,
    runs: int,
) -> dict[str, Any]:
    env = dict(os.environ)
    if framework == "easycat":
        source = str(_REPO_ROOT / "src")
        env["PYTHONPATH"] = (
            source if not env.get("PYTHONPATH") else f"{source}{os.pathsep}{env['PYTHONPATH']}"
        )
    command = [
        python,
        str(Path(__file__).resolve()),
        "--worker",
        framework,
        "--warmup",
        str(warmup),
        "--runs",
        str(runs),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, env=env, check=True)
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith(_RESULT_PREFIX):
            return json.loads(line.removeprefix(_RESULT_PREFIX))
    raise RuntimeError(f"{framework} worker did not emit a benchmark result")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable, help="Python used for EasyCat")
    parser.add_argument("--pipecat-python", help="Python containing a Pipecat installation")
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--runs", type=int, default=50)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-faster", action="store_true")
    parser.add_argument("--worker", choices=("easycat", "pipecat"), help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.runs < 1 or args.warmup < 0:
        raise SystemExit("--runs must be positive and --warmup must be non-negative")
    if args.worker:
        worker = _easycat_worker if args.worker == "easycat" else _pipecat_worker
        print(_RESULT_PREFIX + json.dumps(worker(warmup=args.warmup, runs=args.runs)))
        return 0

    easycat = _run_worker("easycat", python=args.python, warmup=args.warmup, runs=args.runs)
    artifact: dict[str, Any] = {
        "kind": "smart_turn_framework_benchmark",
        "schema_version": 1,
        "meta": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "warmup": args.warmup,
            "runs": args.runs,
        },
        "easycat": easycat,
    }
    if args.pipecat_python:
        pipecat = _run_worker(
            "pipecat", python=args.pipecat_python, warmup=args.warmup, runs=args.runs
        )
        artifact["pipecat"] = pipecat
        artifact["comparison"] = compare_results(easycat, pipecat)

    rendered = json.dumps(artifact, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    if args.require_faster and not artifact.get("comparison", {}).get("easycat_faster", False):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
