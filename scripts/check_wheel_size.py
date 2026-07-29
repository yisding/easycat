"""Fail when a built EasyCat wheel exceeds its deliberate size budget."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

MIB = 1024 * 1024
MAX_WHEEL_MIB = 13
MAX_WHEEL_BYTES = MAX_WHEEL_MIB * MIB


def wheel_size(path: Path) -> int:
    """Return the wheel size after validating the artifact path."""
    if path.suffix != ".whl":
        raise ValueError(f"{path} is not a .whl artifact")
    if not path.is_file():
        raise FileNotFoundError(f"{path} does not exist")
    return path.stat().st_size


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheels", nargs="+", type=Path, help="Built .whl artifact(s) to check.")
    parser.add_argument(
        "--max-mib",
        type=int,
        default=MAX_WHEEL_MIB,
        help=f"Maximum size per wheel in MiB (default: {MAX_WHEEL_MIB}).",
    )
    args = parser.parse_args(argv)
    if args.max_mib <= 0:
        parser.error("--max-mib must be positive")

    max_bytes = args.max_mib * MIB
    failed = False
    for path in args.wheels:
        try:
            size = wheel_size(path)
        except (FileNotFoundError, ValueError) as exc:
            print(f"wheel-size: {exc}", file=sys.stderr)
            failed = True
            continue

        summary = f"{path}: {size:,} bytes ({size / MIB:.2f} MiB); budget {args.max_mib} MiB"
        if size > max_bytes:
            print(f"wheel-size: FAIL: {summary}", file=sys.stderr)
            failed = True
        else:
            print(f"wheel-size: PASS: {summary}")

    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
