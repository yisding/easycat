#!/usr/bin/env python3
"""Compare framework scheduling from accepted transcript to first audio.

EasyCat runs in the current project environment. LiveKit Agents and Pipecat
run in isolated, version-pinned ``uv`` environments. Persistent workers let
the orchestrator randomize framework order for every warmup and measured
round without including process or environment startup in the metric.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import queue
import random
import shutil
import subprocess
import sys
import threading
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

Framework = Literal["easycat", "livekit", "pipecat"]
FRAMEWORKS: tuple[Framework, ...] = ("easycat", "livekit", "pipecat")
ENVIRONMENT_ROOT = Path(__file__).with_name("framework_environments")
LOCK_EXCLUDE_NEWER = "2026-07-11T22:00:00Z"
PINS = {
    framework: tuple(
        tomllib.loads((ENVIRONMENT_ROOT / framework / "pyproject.toml").read_text())["project"][
            "dependencies"
        ]
    )
    for framework in ("livekit", "pipecat")
}
RESPONSE_TEXT = "Hello there."
EXPECTED_TTS_TEXT = {
    "easycat": "Hello there.",
    "livekit": "Hello there",
    "pipecat": "Hello there.",
}


@dataclass(frozen=True)
class WorkerSpec:
    framework: Framework
    command: tuple[str, ...]


def worker_specs(
    frameworks: Sequence[Framework] = FRAMEWORKS,
    *,
    worker_path: Path | None = None,
) -> list[WorkerSpec]:
    """Build explicit commands, pinning competitor environments."""
    worker = worker_path or Path(__file__).with_name("framework_latency_worker.py")
    uv = shutil.which("uv")
    specs: list[WorkerSpec] = []
    for framework in frameworks:
        if framework == "easycat":
            command = (sys.executable, str(worker), "--framework", framework)
        else:
            if uv is None:
                raise RuntimeError("uv is required for isolated competitor environments")
            project = ENVIRONMENT_ROOT / framework
            command = (
                uv,
                "run",
                "--quiet",
                "--no-progress",
                "--no-config",
                "--exclude-newer",
                LOCK_EXCLUDE_NEWER,
                "--isolated",
                "--project",
                str(project),
                "--locked",
                "--python",
                sys.executable,
                "python",
                str(worker),
                "--framework",
                framework,
            )
        specs.append(WorkerSpec(framework=framework, command=command))
    return specs


def _lock_metadata() -> dict[str, dict[str, str]]:
    metadata: dict[str, dict[str, str]] = {}
    for framework in ("livekit", "pipecat"):
        lock_path = ENVIRONMENT_ROOT / framework / "uv.lock"
        digest = hashlib.sha256(lock_path.read_bytes()).hexdigest()
        metadata[framework] = {
            "path": str(lock_path.relative_to(Path(__file__).parents[1])),
            "sha256": digest,
        }
    return metadata


class Worker:
    def __init__(self, spec: WorkerSpec, *, timeout_s: float) -> None:
        self.framework = spec.framework
        self.timeout_s = timeout_s
        self._stderr: list[str] = []
        self._messages: queue.Queue[str] = queue.Queue()
        self._process = subprocess.Popen(  # noqa: S603 - argv is internally constructed
            spec.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env={**os.environ, "PYTHONUNBUFFERED": "1", "LOGURU_LEVEL": "ERROR"},
        )
        assert self._process.stdout is not None
        assert self._process.stderr is not None
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        try:
            ready = self._receive()
            if ready.get("kind") != "ready" or ready.get("framework") != self.framework:
                raise RuntimeError(f"invalid {self.framework} worker handshake: {ready}")
        except BaseException:
            self.close()
            raise
        self.version = str(ready["version"])
        self.python = str(ready["python"])

    def _read_stdout(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            self._messages.put(line)

    def _read_stderr(self) -> None:
        assert self._process.stderr is not None
        for line in self._process.stderr:
            self._stderr.append(line.rstrip())
            del self._stderr[:-50]

    def _receive(self) -> dict[str, Any]:
        try:
            line = self._messages.get(timeout=self.timeout_s)
        except queue.Empty as exc:
            detail = "\n".join(self._stderr[-10:])
            raise TimeoutError(
                f"{self.framework} worker timed out after {self.timeout_s}s\n{detail}"
            ) from exc
        payload = json.loads(line)
        if payload.get("kind") == "error":
            raise RuntimeError(
                f"{self.framework} worker failed: {payload.get('error')}\n"
                f"{payload.get('traceback', '')}"
            )
        return payload

    def sample(self, *, llm_delay_ms: float, tts_delay_ms: float) -> dict[str, Any]:
        assert self._process.stdin is not None
        request = {
            "command": "sample",
            "llm_delay_ms": llm_delay_ms,
            "tts_delay_ms": tts_delay_ms,
        }
        self._process.stdin.write(json.dumps(request) + "\n")
        self._process.stdin.flush()
        payload = self._receive()
        if payload.get("kind") != "sample":
            raise RuntimeError(f"invalid {self.framework} sample: {payload}")
        _validate_sample(payload)
        return payload

    def close(self) -> None:
        if self._process.poll() is not None:
            return
        assert self._process.stdin is not None
        try:
            self._process.stdin.write('{"command":"shutdown"}\n')
            self._process.stdin.flush()
            self._process.wait(timeout=5.0)
        except (BrokenPipeError, subprocess.TimeoutExpired):
            self._process.terminate()
            self._process.wait(timeout=5.0)


def _validate_sample(sample: dict[str, Any]) -> None:
    latency = sample.get("latency_ms")
    if not isinstance(latency, int | float) or not math.isfinite(latency) or latency < 0:
        raise ValueError(f"invalid latency sample: {latency!r}")
    provider_elapsed = sample.get("provider_elapsed_ms")
    if (
        not isinstance(provider_elapsed, int | float)
        or not math.isfinite(provider_elapsed)
        or provider_elapsed < 0
        or provider_elapsed > latency
    ):
        raise ValueError(f"invalid provider elapsed time: {provider_elapsed!r}")
    framework = sample.get("framework")
    expected_text = EXPECTED_TTS_TEXT.get(framework)
    if expected_text is None or sample.get("text") != expected_text:
        raise ValueError(f"incorrect response text: {sample.get('text')!r}")
    audio_bytes = sample.get("audio_bytes")
    if not isinstance(audio_bytes, int) or isinstance(audio_bytes, bool) or audio_bytes <= 0:
        raise ValueError(f"invalid first response audio: {audio_bytes!r}")
    if framework == "easycat" and sample.get("agent_request_started_in_timed_path") is not True:
        raise ValueError("EasyCat sample bypassed the voice-turn agent request transition")


def percentile(samples: Sequence[float], quantile: float) -> float:
    """Return a linearly interpolated percentile over non-empty samples."""
    if not samples:
        raise ValueError("samples must not be empty")
    if not 0.0 <= quantile <= 1.0:
        raise ValueError("quantile must be between zero and one")
    ordered = sorted(samples)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] * (1.0 - weight) + ordered[upper] * weight)


def _revision() -> dict[str, Any]:
    try:
        sha = subprocess.run(  # noqa: S603, S607 - fixed local git query
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(  # noqa: S603, S607 - fixed local git query
                ["git", "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "dirty": None}
    return {"commit": sha, "dirty": dirty}


def run_benchmark(  # noqa: C901, PLR0912 - orchestration keeps cleanup and ordering together
    *,
    iterations: int = 30,
    warmups: int = 5,
    llm_delay_ms: float = 20.0,
    tts_delay_ms: float = 20.0,
    seed: int = 7,
    frameworks: Sequence[Framework] = FRAMEWORKS,
    worker_timeout_s: float = 120.0,
) -> dict[str, Any]:
    """Run randomized warmup and measured rounds against persistent workers."""
    if iterations < 1:
        raise ValueError("iterations must be positive")
    if warmups < 0:
        raise ValueError("warmups must be non-negative")
    if llm_delay_ms < 0 or tts_delay_ms < 0:
        raise ValueError("delays must be non-negative")
    if not frameworks or len(set(frameworks)) != len(frameworks):
        raise ValueError("frameworks must be unique and non-empty")

    workers: dict[Framework, Worker] = {}
    latency_samples: dict[Framework, list[float]] = {framework: [] for framework in frameworks}
    overhead_samples: dict[Framework, list[float]] = {framework: [] for framework in frameworks}
    rng = random.Random(seed)
    try:
        for spec in worker_specs(frameworks):
            workers[spec.framework] = Worker(spec, timeout_s=worker_timeout_s)
        python_versions = {worker.python for worker in workers.values()}
        if len(python_versions) != 1:
            raise RuntimeError(f"workers used different Python versions: {python_versions}")
        for measured, rounds in ((False, warmups), (True, iterations)):
            for _ in range(rounds):
                order = list(frameworks)
                rng.shuffle(order)
                for framework in order:
                    sample = workers[framework].sample(
                        llm_delay_ms=llm_delay_ms,
                        tts_delay_ms=tts_delay_ms,
                    )
                    if measured:
                        latency = float(sample["latency_ms"])
                        provider_elapsed = float(sample["provider_elapsed_ms"])
                        latency_samples[framework].append(latency)
                        overhead_samples[framework].append(latency - provider_elapsed)
    finally:
        for worker in workers.values():
            worker.close()

    results: dict[str, Any] = {}
    for framework in frameworks:
        latency_values = latency_samples[framework]
        overhead_values = overhead_samples[framework]
        results[framework] = {
            "version": workers[framework].version,
            "python": workers[framework].python,
            "latency_samples_ms": latency_values,
            "latency_p50_ms": percentile(latency_values, 0.50),
            "latency_p95_ms": percentile(latency_values, 0.95),
            "latency_p99_ms": percentile(latency_values, 0.99),
            "framework_overhead_samples_ms": overhead_values,
            "framework_overhead_p50_ms": percentile(overhead_values, 0.50),
            "framework_overhead_p95_ms": percentile(overhead_values, 0.95),
            "framework_overhead_p99_ms": percentile(overhead_values, 0.99),
        }
    ranking = sorted(frameworks, key=lambda name: results[name]["framework_overhead_p50_ms"])
    easycat_fastest_p50 = "easycat" in results and all(
        results["easycat"]["framework_overhead_p50_ms"]
        < results[name]["framework_overhead_p50_ms"]
        for name in frameworks
        if name != "easycat"
    )
    easycat_fastest_p95 = "easycat" in results and all(
        results["easycat"]["framework_overhead_p95_ms"]
        < results[name]["framework_overhead_p95_ms"]
        for name in frameworks
        if name != "easycat"
    )
    return {
        "schema_version": 1,
        "kind": "framework_latency_benchmark",
        "metric": "accepted_transcript_to_first_audio_ms",
        "comparison_metric": "framework_overhead_ms",
        "easycat_revision": _revision(),
        "workload": {
            "iterations": iterations,
            "warmups": warmups,
            "llm_delay_ms": llm_delay_ms,
            "tts_delay_ms": tts_delay_ms,
            "response_text": RESPONSE_TEXT,
            "expected_tts_text": EXPECTED_TTS_TEXT,
        },
        "methodology": {
            "framework_order": "randomized_per_round",
            "seed": seed,
            "persistent_workers": True,
            "isolated_competitor_environments": True,
            "gc_disabled_during_critical_path": True,
            "percentile_method": "linear_interpolation",
            "correctness_gate": (
                "exact_framework_tts_text_nonempty_audio_and_easycat_voice_transition"
            ),
            "framework_overhead": "latency_ms_minus_measured_provider_elapsed_ms",
            "timed_entry_points": {
                "easycat": "Session.end_turn (AgentRequestStarted dispatched in span)",
                "livekit": "AgentSession.run",
                "pipecat": "PipelineTask.queue_frame",
            },
        },
        "pins": {name: list(pins) for name, pins in PINS.items()},
        "environment_locks": _lock_metadata(),
        "results": results,
        "ranking_by_framework_overhead_p50": ranking,
        "easycat_fastest": {
            "p50": easycat_fastest_p50,
            "p95": easycat_fastest_p95,
            "all": easycat_fastest_p50 and easycat_fastest_p95,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--llm-delay-ms", type=float, default=20.0)
    parser.add_argument("--tts-delay-ms", type=float, default=20.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--worker-timeout-s", type=float, default=120.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-easycat-fastest", action="store_true")
    args = parser.parse_args()
    try:
        payload = run_benchmark(
            iterations=args.iterations,
            warmups=args.warmups,
            llm_delay_ms=args.llm_delay_ms,
            tts_delay_ms=args.tts_delay_ms,
            seed=args.seed,
            worker_timeout_s=args.worker_timeout_s,
        )
    except (RuntimeError, TimeoutError, ValueError) as exc:
        parser.error(str(exc))

    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(rendered)
    print(rendered, end="")
    if args.require_easycat_fastest and not payload["easycat_fastest"]["all"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
