from __future__ import annotations

import argparse
from collections.abc import Sequence

from easycat.validation import (
    _latency_runner,
    _live_runner,
    _release_runner,
)
from easycat.validation._lane_harness import ValidationRunResult as ValidationRunResult
from easycat.validation._runner_support import CommandResult as CommandResult
from easycat.validation._runner_support import CommandRunner
from easycat.validation._slice_runner import VALIDATION_SELECTORS, run_validation_slice

DEFAULT_RELEASE_EXTRAS = _release_runner.DEFAULT_RELEASE_EXTRAS
DEFAULT_RELEASE_PROVIDERS = _release_runner.DEFAULT_RELEASE_PROVIDERS
DEFAULT_RELEASE_SURFACES = _release_runner.DEFAULT_RELEASE_SURFACES
RELEASE_SLICES = _release_runner.RELEASE_SLICES
RELEASE_TEST_DEPENDENCIES = _release_runner.RELEASE_TEST_DEPENDENCIES
LATENCY_SYNTHETIC_FAILURE_SAMPLE = _latency_runner.LATENCY_SYNTHETIC_FAILURE_SAMPLE
LATENCY_SYNTHETIC_SAMPLE_DEBUG_KEY = _latency_runner.LATENCY_SYNTHETIC_SAMPLE_DEBUG_KEY
run_latency_validation = _latency_runner.run_latency_validation
classify_live_failure = _live_runner.classify_live_failure
run_live_validation = _live_runner.run_live_validation
run_release_validation = _release_runner.run_release_validation


def main(
    argv: Sequence[str] | None = None,
    *,
    command_runner: CommandRunner | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Run EasyCat validation slices.")
    parser.add_argument("slice", choices=sorted(VALIDATION_SELECTORS))
    parser.add_argument(
        "--artifacts-dir",
        default=".easycat/validation",
        help="Directory where validation reports and logs are written.",
    )
    parser.add_argument("--report", help="Optional additional validation report JSON path.")
    parser.add_argument("--junit", help="Optional JUnit XML output path.")
    parser.add_argument("--junit-prefix", help="Optional pytest JUnit prefix.")
    args = parser.parse_args(argv)

    result = run_validation_slice(
        args.slice,
        artifacts_dir=args.artifacts_dir,
        report_path=args.report,
        junit_path=args.junit,
        junit_prefix=args.junit_prefix,
        command_runner=command_runner,
    )
    print(f"{args.slice}: {result.run.status}; report: {result.report_path}")
    return result.exit_code
