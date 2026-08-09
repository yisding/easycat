"""Keep local uv compatibility separate from CI's exact reproducibility pin."""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def test_local_uv_requirement_accepts_the_audited_minor_releases() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    requirement = pyproject["tool"]["uv"]["required-version"]
    specifier = SpecifierSet(requirement)

    assert requirement == ">=0.11.0,<0.13.0"
    assert Version("0.11.0") in specifier
    assert Version("0.11.30") in specifier
    assert Version("0.12.0") in specifier
    assert Version("0.12.99") in specifier
    assert Version("0.13.0") not in specifier


def test_build_backend_accepts_the_current_ci_uv_minor() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    requirement = Requirement(pyproject["build-system"]["requires"][0])

    assert requirement.name == "uv_build"
    assert Version("0.12.0") not in requirement.specifier
    assert Version("0.12.1") in requirement.specifier
    assert Version("0.13.0") not in requirement.specifier


def test_every_setup_uv_step_uses_an_exact_ci_pin() -> None:
    setup_steps = 0
    for workflow_path in sorted((*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml"))):
        lines = workflow_path.read_text().splitlines()
        for index, line in enumerate(lines):
            if "astral-sh/setup-uv@" not in line:
                continue
            setup_steps += 1
            step = "\n".join(lines[index : index + 10])
            assert 'version: "0.12.1"' in step, workflow_path.name

    assert setup_steps > 0


def test_docs_toolchain_uses_the_shared_uv_lockfile() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    docs_group = pyproject["dependency-groups"]["docs"]
    workflow = (WORKFLOWS / "docs.yml").read_text(encoding="utf-8")

    assert docs_group == [
        "mkdocs==1.6.1",
        "mkdocs-material==9.7.7",
        "pygments==2.20.0",
        "pymdown-extensions==11.0.1",
    ]
    assert "astral-sh/setup-uv@" in workflow
    assert (
        "uv sync --locked --no-default-groups --group docs --extra quickstart --python 3.12"
        in workflow
    )
    assert "uv run --no-sync mkdocs build --strict" in workflow
    assert "pip install" not in workflow
    assert not (ROOT / "requirements-docs.in").exists()
    assert not (ROOT / "requirements-docs.txt").exists()
