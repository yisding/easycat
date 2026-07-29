"""Verify that a release tag exactly matches the package version."""

from __future__ import annotations

import argparse
import tomllib
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def expected_release_tag(pyproject_path: Path | None = None) -> str:
    """Return the only tag allowed to publish the configured project version."""
    path = pyproject_path or REPO_ROOT / "pyproject.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return f"v{data['project']['version']}"


def validate_release_tag(tag: str, pyproject_path: Path | None = None) -> str:
    """Return *tag* when it matches, otherwise raise a release-blocking error."""
    expected = expected_release_tag(pyproject_path)
    if tag != expected:
        raise ValueError(
            f"release tag {tag!r} does not match package version; expected {expected!r}"
        )
    return tag


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="Git tag supplied by the release workflow.")
    args = parser.parse_args(argv)
    try:
        tag = validate_release_tag(args.tag)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"release tag matches package version: {tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
