"""List or run one credential-free checkpoint from every teaching chapter.

List the spine from the repository root::

    uv run python docs/teaching/offline_spine.py
    uv run python docs/teaching/offline_spine.py --json

Run every checkpoint (captured output stays quiet unless one fails)::

    uv run python docs/teaching/offline_spine.py --run --jobs 4
    uv run python docs/teaching/offline_spine.py --run --jobs 4 --json

Replay only the cumulative spine through a completed chapter::

    uv sync --extra quickstart --group dev
    uv run python docs/teaching/offline_spine.py --run --through 5 --jobs 4

The runner removes every ``*_API_KEY`` variable from child environments and
disables bytecode-cache writes. The selected scripts are designed not to open
audio devices, make provider requests, or leave files in the checkout; run an
individual printed command when you want its full evidence. A checkpoint passes
only when it exits zero, emits one JSON document on stdout, and keeps stderr
empty; intentional failure scenarios belong inside that JSON evidence.
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

    @property
    def setup_command(self) -> str:
        extra = "local" if self.chapter <= 1 else "quickstart"
        return f"uv sync --extra {extra} --group dev"

    def as_row(self) -> dict[str, object]:
        return {
            **asdict(self),
            "setup_command": self.setup_command,
            "command": self.command,
        }


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
        "partial_policy_probe.py",
        "partial vs final commitment",
        "revised partials cancel speculation; only the final `fifty` commits the safe action",
    ),
    Checkpoint(
        3,
        "03-parrot-naive",
        "timeout_policy_probe.py",
        "silence-timeout tradeoff",
        "500 ms fires 45 ms before the next word; 2,000 ms adds a 2,005 ms commit wait",
    ),
    Checkpoint(
        4,
        "04-vad-preroll",
        "preroll_probe.py",
        "VAD pre-roll frame order",
        "pre-roll restores both cached frames before trigger/live; disabling it starts at trigger",
    ),
    Checkpoint(
        5,
        "05-blocking-agent",
        "gap_decomposition_probe.py",
        "blocking first-audio gap",
        "1,200 ms agent plus 450 ms TTS equals 1,650 ms total; full enqueue takes 800 ms",
    ),
    Checkpoint(
        6,
        "06-streaming-agent",
        "tts_delivery_probe.py",
        "sentence-level TTS handoff",
        "sentence delivery rows preserve acceptance separately and roll up to one matching turn",
    ),
    Checkpoint(
        7,
        "07-tools",
        "filler_delivery_probe.py",
        "tool filler delivery",
        "fast tools skip filler; rejected filler has zero accepted chunks and reply audio "
        "comes first",
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
        "matrix_probe.py",
        "provider × transport matrix",
        "two provider mixes cross three transport configs into six cells without changing axes",
    ),
    Checkpoint(
        14,
        "14-bring-your-own-agent",
        "workflow_state_probe.py",
        "plain workflow bridge contract",
        "`MyWorkflow` yields a reply plus `EndCallAction`; the bridge reports deep mode",
    ),
    Checkpoint(
        15,
        "15-operate-in-production",
        "manager_probe.py",
        "multi-session manager rollback",
        "failed starts release slots; stop-all records one error and still attempts both sessions",
    ),
)


def catalog(
    checkpoints: tuple[Checkpoint, ...] = CHECKPOINTS,
) -> list[dict[str, object]]:
    return [checkpoint.as_row() for checkpoint in checkpoints]


def _select_checkpoints(through: int | None) -> tuple[Checkpoint, ...]:
    if through is None:
        return CHECKPOINTS
    return tuple(checkpoint for checkpoint in CHECKPOINTS if checkpoint.chapter <= through)


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
    elif completed.stderr != "":
        stderr_lines = [line for line in completed.stderr.splitlines() if line.strip()]
        unexpected_output = stderr_lines[-1] if stderr_lines else repr(completed.stderr)
        detail = f"unexpected stderr: {unexpected_output}"
    else:
        try:
            json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            detail = f"stdout is not one JSON document: {exc.msg}"
    return {
        **checkpoint.as_row(),
        "status": "pass" if not detail else "fail",
        "returncode": completed.returncode,
        "detail": detail,
    }


def run_all(
    *,
    jobs: int,
    timeout_s: float,
    checkpoints: tuple[Checkpoint, ...] = CHECKPOINTS,
) -> list[dict[str, object]]:
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        return list(
            executor.map(
                lambda checkpoint: _run_checkpoint(checkpoint, timeout_s=timeout_s),
                checkpoints,
            )
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="execute selected checkpoints")
    parser.add_argument(
        "--through",
        type=int,
        metavar="CHAPTER",
        help="limit list or run to chapters 0 through CHAPTER",
    )
    parser.add_argument("--jobs", type=int, default=1, help="parallel checkpoints (default: 1)")
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=90.0,
        help="per-checkpoint timeout in seconds (default: 90)",
    )
    parser.add_argument("--json", action="store_true", help="emit a parseable report")
    return parser


def _parse_args() -> argparse.Namespace:
    parser = _parser()
    args = parser.parse_args()
    if args.jobs < 1:
        raise SystemExit("--jobs must be at least 1")
    if args.timeout_s <= 0:
        raise SystemExit("--timeout-s must be positive")
    final_chapter = CHECKPOINTS[-1].chapter
    if args.through is not None and not 0 <= args.through <= final_chapter:
        parser.error(f"--through must be between 0 and {final_chapter}")
    return args


def main() -> None:
    args = _parse_args()
    checkpoints = _select_checkpoints(args.through)

    if not args.run:
        rows = catalog(checkpoints)
        report = {
            "mode": "list",
            "through": args.through,
            "count": len(rows),
            "checkpoints": rows,
        }
        if args.json:
            print(json.dumps(report, indent=2))
            return
        for row in rows:
            print(f"{row['chapter']:>2}  {row['concept']}")
            print(f"    Setup: {row['setup_command']}")
            print(f"    Run: {row['command']}")
            print(f"    Look for: {row['evidence']}")
        return

    rows = run_all(jobs=args.jobs, timeout_s=args.timeout_s, checkpoints=checkpoints)
    passed = sum(row["status"] == "pass" for row in rows)
    report = {
        "mode": "run",
        "through": args.through,
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
