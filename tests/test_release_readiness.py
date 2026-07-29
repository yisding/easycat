"""Release metadata and tag/version consistency guards."""

from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path

import pytest

import easycat
from scripts.check_release_tag import expected_release_tag, validate_release_tag

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_public_version_matches_installed_distribution_metadata() -> None:
    assert easycat.__version__ == importlib.metadata.version("easycat")
    assert "__version__" in dir(easycat)
    assert "__version__" not in easycat.__all__


def test_expected_release_tag_tracks_pyproject_version() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    expected = f"v{pyproject['project']['version']}"

    assert expected_release_tag() == expected
    assert validate_release_tag(expected) == expected


@pytest.mark.parametrize("tag", ["0.1.0", "v0.1.1", "release-0.1.0"])
def test_release_tag_rejects_nonmatching_names(tag: str) -> None:
    with pytest.raises(ValueError, match="does not match package version"):
        validate_release_tag(tag)


def test_release_workflow_checks_tag_before_building() -> None:
    workflow = (REPO_ROOT / ".github/workflows/release-validation.yml").read_text(encoding="utf-8")
    check = 'uv run python scripts/check_release_tag.py "$RELEASE_TAG"'

    assert "if: startsWith(github.ref, 'refs/tags/')" in workflow
    assert check in workflow
    assert workflow.index(check) < workflow.index("uv build --sdist --wheel")


def test_release_readiness_is_documented() -> None:
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    public_api = (REPO_ROOT / "docs/public-api.md").read_text(encoding="utf-8")

    assert "## Unreleased" in changelog
    assert "## 0.1.0 - Unreleased" in changelog
    assert "## Preparing a release" in contributing
    assert "reserve the `easycat` project name" in contributing
    assert "rehearse" in contributing and "TestPyPI" in contributing
    assert "Starting with version 1.0.0" in public_api
    assert "Semantic Versioning" in public_api
