"""List or run one credential-free checkpoint from every teaching chapter.

List the spine from the repository root::

    uv run python docs/teaching/offline_spine.py
    uv run python docs/teaching/offline_spine.py --json

Run every checkpoint (captured output stays quiet unless one fails)::

    uv run python docs/teaching/offline_spine.py --run --jobs 4
    uv run python docs/teaching/offline_spine.py --run --jobs 4 --json

The runner removes every ``*_API_KEY`` variable from child environments and
disables bytecode-cache writes. The selected scripts are designed not to open
audio devices, make provider requests, or leave files in the checkout; run an
individual printed command when you want its full evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class Checkpoint:
    chapter: int
    folder: str
    script: str
    concept: str
    evidence: str

    @property
    def path(self) -> Path:
        return Path("docs") / "teaching" / self.folder / self.script

    @property
    def command(self) -> str:
        return f"uv run python {self.path.as_posix()}"

    def as_row(self) -> dict[str, object]:
        return {**asdict(self), "command": self.command}


CHECKPOINTS = (
    Checkpoint(
        0,
        "00-hello-audio",
        "format_boundaries.py",
        "audio format boundaries",
        "wire, provider-input, pipeline, config-default, and media roles use different rates",
    ),
    Checkpoint(
        1,
        "01-echo",
        "transport_contract_probe.py",
        "transport acceptance",
        "two chunks are accepted, one is rejected, and `version_info()` changes full conformance",
    ),
    Checkpoint(
        2,
        "02-transcribe",
        "stream_lifecycle_probe.py",
        "stream lifetime cleanup",
        "every path closes STT before transport disconnect; feed failure cancels its sibling task",
    ),
    Checkpoint(
        3,
        "03-parrot-naive",
        "parrot_lifecycle_probe.py",
        "task/resource cleanup",
        "event-stream exhaustion cancels mic receive; every path closes STT before disconnect",
    ),
    Checkpoint(
        4,
        "04-vad-preroll",
        "delivery_probe.py",
        "delivery acceptance",
        "`parrot.delivery` preserves two accepted and one rejected chunks after STT closes",
    ),
    Checkpoint(
        5,
        "05-blocking-agent",
        "tts_outcome_probe.py",
        "first-audio outcomes",
        "no chunks, all rejected, and first accepted audio produce three distinct outcomes",
    ),
    Checkpoint(
        6,
        "06-streaming-agent",
        "tts_delivery_probe.py",
        "streamed TTS delivery",
        "sentence delivery counts roll up to matching turn counts in all three scenarios",
    ),
    Checkpoint(
        7,
        "07-tools",
        "action_catalog.py",
        "session-action catalog",
        "seven action types separate core-supported from executor-dependent actions",
    ),
    Checkpoint(
        8,
        "08-smart-turn",
        "endpoint_wait_probe.py",
        "endpoint wait decomposition",
        "smart accept takes 240 ms, VAD 800 ms, and fallback 1,040 ms from three components",
    ),
    Checkpoint(
        9,
        "09-interruption",
        "barge_in_turn_probe.py",
        "barge-in cancellation",
        "triggering speech remains unconsumed while bot cancellation precedes the next STT stream",
    ),
    Checkpoint(
        10,
        "10-cleaning-signal",
        "replay_metrics_probe.py",
        "NR/AEC replay metrics",
        "aligned reference audio changes RMS by -12.041 dB; missing or short references fail",
    ),
    Checkpoint(
        11,
        "11-journal",
        "query_coverage_probe.py",
        "journal query coverage",
        "a zero-result intersection has marginal matches, while a misspelled turn has none",
    ),
    Checkpoint(
        12,
        "12-evals-and-latency",
        "p95_sensitivity_probe.py",
        "small-sample P95 sensitivity",
        "removing `turn_02_slow_agent.bundle` alone drops P95 by 1,260 ms",
    ),
    Checkpoint(
        13,
        "13-swap-providers-and-transports",
        "session_scope_probe.py",
        "graceful vs forced teardown",
        "both scope paths export postmortem evidence before the caller-owned client closes",
    ),
    Checkpoint(
        14,
        "14-bring-your-own-agent",
        "workflow_state_probe.py",
        "workflow artifact boundary",
        "the artifact keeps metadata-only workflow state plus a pending session action",
    ),
    Checkpoint(
        15,
        "15-operate-in-production",
        "postmortem_probe.py",
        "postmortem journal preservation",
        "one read-only view preserves 15 records and exports a matching bundle after stop",
    ),
)


def catalog() -> list[dict[str, object]]:
    return [checkpoint.as_row() for checkpoint in CHECKPOINTS]


def _credential_free_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.endswith("_API_KEY")}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _run_checkpoint(checkpoint: Checkpoint, *, timeout_s: float) -> dict[str, object]:
    path = REPO_ROOT / checkpoint.path
    try:
        completed = subprocess.run(
            [sys.executable, str(path)],
            cwd=REPO_ROOT,
            env=_credential_free_environment(),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            **checkpoint.as_row(),
            "status": "timeout",
            "returncode": None,
            "detail": f"exceeded {timeout_s:g}s",
        }

    detail = ""
    if completed.returncode:
        lines = [line for line in completed.stderr.splitlines() if line.strip()]
        detail = lines[-1] if lines else "checkpoint exited without stderr"
    return {
        **checkpoint.as_row(),
        "status": "pass" if completed.returncode == 0 else "fail",
        "returncode": completed.returncode,
        "detail": detail,
    }


def run_all(*, jobs: int, timeout_s: float) -> list[dict[str, object]]:
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        return list(
            executor.map(
                lambda checkpoint: _run_checkpoint(checkpoint, timeout_s=timeout_s),
                CHECKPOINTS,
            )
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="execute every checkpoint")
    parser.add_argument("--jobs", type=int, default=1, help="parallel checkpoints (default: 1)")
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=90.0,
        help="per-checkpoint timeout in seconds (default: 90)",
    )
    parser.add_argument("--json", action="store_true", help="emit a parseable report")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.jobs < 1:
        raise SystemExit("--jobs must be at least 1")
    if args.timeout_s <= 0:
        raise SystemExit("--timeout-s must be positive")

    if not args.run:
        rows = catalog()
        report = {"mode": "list", "count": len(rows), "checkpoints": rows}
        if args.json:
            print(json.dumps(report, indent=2))
            return
        for row in rows:
            print(f"{row['chapter']:>2}  {row['concept']}")
            print(f"    {row['command']}")
            print(f"    Look for: {row['evidence']}")
        return

    rows = run_all(jobs=args.jobs, timeout_s=args.timeout_s)
    passed = sum(row["status"] == "pass" for row in rows)
    report = {
        "mode": "run",
        "count": len(rows),
        "passed": passed,
        "failed": len(rows) - passed,
        "checkpoints": rows,
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for row in rows:
            label = str(row["status"]).upper()
            print(f"{label:<7} {row['chapter']:>2}  {row['concept']}")
            print(f"           Look for: {row['evidence']}")
            if row["detail"]:
                print(f"           {row['detail']}")
        print(f"{passed}/{len(rows)} checkpoints passed")
    if passed != len(rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
