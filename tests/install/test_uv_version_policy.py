"""Keep local uv compatibility separate from CI's exact reproducibility pin."""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]
UV_VERSION_FILE = ROOT / ".uv-version"
WORKFLOWS = ROOT / ".github" / "workflows"


def test_local_uv_requirement_accepts_compatible_patch_releases() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    requirement = pyproject["tool"]["uv"]["required-version"]
    specifier = SpecifierSet(requirement)

    assert requirement == ">=0.11.0,<0.12.0"
    assert Version("0.11.0") in specifier
    assert Version("0.11.30") in specifier
    assert Version("0.12.0") not in specifier


def test_every_setup_uv_step_uses_an_exact_ci_pin() -> None:
    assert UV_VERSION_FILE.read_text().strip() == "0.11.30"

    setup_steps = 0
    for workflow_path in sorted((*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml"))):
        lines = workflow_path.read_text().splitlines()
        for index, line in enumerate(lines):
            if "astral-sh/setup-uv@" not in line:
                continue
            setup_steps += 1
            step = "\n".join(lines[index : index + 10])
            has_central_pin = "version-file: .uv-version" in step
            has_standalone_pin = 'version: "0.11.30"' in step
            assert has_central_pin or has_standalone_pin, workflow_path.name

    assert setup_steps > 0
