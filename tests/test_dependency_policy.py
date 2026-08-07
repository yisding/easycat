"""Dependency policy guards for optional extras and security-sensitive pins."""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _locked_packages(name: str) -> list[dict]:
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    return [package for package in lock["package"] if package["name"] == name]


def _locked_package_names() -> set[str]:
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    return {package["name"] for package in lock["package"]}


def _requirement(deps: list[str], name: str) -> str:
    for dep in deps:
        if Requirement(dep).name == name:
            return dep
    raise AssertionError(f"{name!r} not declared in {deps}")


def test_funasr_vad_extra_uses_in_tree_runtime_dependencies() -> None:
    """FunASR VAD must not reintroduce the stale funasr-onnx dependency."""
    extras = _pyproject()["project"]["optional-dependencies"]
    deps = extras["funasr-vad"]
    names = {Requirement(dep).name for dep in deps}

    assert "kaldi-native-fbank>=1.22.3" in deps
    assert "numpy>=1.24.0" in deps
    # Reuse the canonical in-tree onnxruntime constraint shared by the sibling
    # ONNX VAD extras instead of pinning a divergent version.
    assert _requirement(deps, "onnxruntime") == _requirement(extras["silero-vad"], "onnxruntime")
    for package_name in ("funasr-onnx", "modelscope", "jieba", "onnx"):
        assert package_name not in names


def test_lockfile_does_not_keep_removed_funasr_onnx_dependency_tree() -> None:
    locked_names = _locked_package_names()

    for package_name in ("funasr-onnx", "modelscope", "jieba", "onnx"):
        assert package_name not in locked_names


def test_all_extra_includes_funasr_runtime_frontend_dependency() -> None:
    deps = _pyproject()["project"]["optional-dependencies"]["all"]

    assert "kaldi-native-fbank>=1.22.3" in deps


def test_all_extra_is_union_of_non_conflicting_extras() -> None:
    """``all`` must be the union of every extra except three deliberate exclusions."""
    extras = _pyproject()["project"]["optional-dependencies"]
    # ten-vad: non-permissive license. pydantic-ai / pydantic-ai-v2:
    # mutually exclusive via [tool.uv].conflicts.
    excluded = {"ten-vad", "pydantic-ai", "pydantic-ai-v2"}

    # Stale-exclusion guard (mirrors scripts/extras_matrix.py contract).
    assert excluded <= set(extras), "exclusion set names an extra pyproject no longer declares"

    union: set[str] = set()
    for name, deps in extras.items():
        if name == "all" or name in excluded:
            continue
        union |= set(deps)

    assert set(extras["all"]) == union


def test_telephony_library_extra_excludes_reference_server_dependencies() -> None:
    extras = _pyproject()["project"]["optional-dependencies"]
    telephony = {Requirement(dep).name for dep in extras["telephony"]}
    fastapi_server = {Requirement(dep).name for dep in extras["telephony-fastapi"]}

    assert telephony == {"aiohttp", "phonenumberslite", "twilio"}
    assert fastapi_server == {"fastapi", "python-multipart", "uvicorn"}


def test_declared_dependency_floors_are_compatibility_tested() -> None:
    project = _pyproject()["project"]
    extras = project["optional-dependencies"]

    assert _requirement(project["dependencies"], "httpx") == "httpx>=0.27"
    assert _requirement(project["dependencies"], "rich") == "rich>=13.8"
    assert _requirement(project["dependencies"], "typer") == "typer>=0.26"
    assert _requirement(project["dependencies"], "websockets") == "websockets>=14.0,<17"
    assert _requirement(extras["langchain"], "langchain-core") == "langchain-core>=1.2.28"
    assert _requirement(extras["telephony"], "aiohttp") == "aiohttp>=3.13.3"

    workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    minimum_job = workflow[workflow.index("  minimum-dependencies:") :]
    minimum_job = minimum_job[: minimum_job.index("\n  coverage:")]
    assert "uv sync --resolution lowest-direct --upgrade --group dev" in minimum_job
    assert "--extra langchain --extra telephony --python 3.12" in minimum_job
    assert 'UV_NO_SYNC: "1"' in minimum_job
    assert "uv run --no-sync --python 3.12 easycat validate quick --show-output" in minimum_job
    for floor in (
        '"aiohttp==3.13.3"',
        '"httpx==0.27.0"',
        '"langchain-core==1.2.28"',
        '"rich==13.8.0"',
        '"typer==0.26.0"',
        '"websockets==14.0"',
    ):
        assert floor in minimum_job
    assert "Assert exact direct dependency floors" in minimum_job
    assert "tests/integrations/agents/test_langchain_bridge_invoke.py" in minimum_job
    assert "tests/server/test_webrtc_routes.py" in minimum_job


def test_dependabot_uses_uv_for_python_dependencies_and_keeps_pydantic_ai_v1() -> None:
    config = (REPO_ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")

    assert config.count('package-ecosystem: "uv"') == 1
    assert 'package-ecosystem: "pip"' not in config
    uv_updates = config.split('- package-ecosystem: "uv"', 1)[1].split(
        "\n  - package-ecosystem:", 1
    )[0]
    assert '- dependency-name: "pydantic-ai"' in uv_updates
    assert 'update-types: ["version-update:semver-major"]' in uv_updates


def test_default_off_rnnoise_backend_stays_out_of_quickstart() -> None:
    extras = _pyproject()["project"]["optional-dependencies"]
    quickstart_names = {Requirement(dep).name for dep in extras["quickstart"]}
    rnnoise_names = {Requirement(dep).name for dep in extras["rnnoise"]}
    all_names = {Requirement(dep).name for dep in extras["all"]}

    assert {"pyrnnoise", "requests"} <= rnnoise_names
    assert {"pyrnnoise", "requests"}.isdisjoint(quickstart_names)
    assert rnnoise_names <= all_names


def test_every_install_ships_high_quality_resampling() -> None:
    dependencies = _pyproject()["project"]["dependencies"]

    assert _requirement(dependencies, "soxr") == "soxr>=1.0.0"


def test_lockfile_does_not_pin_vulnerable_onnx() -> None:
    onnx_packages = _locked_packages("onnx")
    vulnerable = [
        package["version"]
        for package in onnx_packages
        if Version(package["version"]) < Version("1.21.0")
    ]
    assert not vulnerable, "uv.lock pins vulnerable ONNX versions: " + ", ".join(vulnerable)


def test_project_declares_license_metadata() -> None:
    project = _pyproject()["project"]

    assert project["license"] == "BSD-2-Clause"
    assert project["license-files"] == ["LICENSE"]
    assert (REPO_ROOT / "LICENSE").exists()


def test_mypy_gate_covers_whole_package_with_strict_core_override() -> None:
    justfile = (REPO_ROOT / "justfile").read_text(encoding="utf-8")
    match = re.search(r"^typecheck:\n    (?P<command>.+)$", justfile, re.MULTILINE)
    assert match is not None, "justfile `typecheck` recipe not found"
    assert match.group("command") == "uv run mypy src/easycat"

    overrides = _pyproject()["tool"]["mypy"]["overrides"]
    assert any(o.get("check_untyped_defs") for o in overrides), (
        "the strict-core mypy override must keep check_untyped_defs enabled"
    )


def _ruff_lint() -> dict:
    return _pyproject()["tool"]["ruff"]["lint"]


def test_ruff_lint_uses_v016_defaults_and_repository_extensions() -> None:
    """Ruff's expanded defaults and the repository-specific rules stay enabled."""
    ruff = _pyproject()["tool"]["ruff"]
    lint = _ruff_lint()

    assert ruff["required-version"] == ">=0.16.1"
    assert "select" not in lint, "lint.select would replace Ruff's curated default rules"
    assert {
        "E",
        "F",
        "I",
        "W",
        "UP",
        "C901",
        "PLR0912",
        "PLR0915",
        "ASYNC",
        "B",
        "RUF006",
        "T201",
        "A001",
        "A003",
        "LOG",
        "PERF203",
        "PERF403",
        "TID251",
    } <= set(lint["extend-select"])


def test_ruff_permanently_ignores_async109() -> None:
    """Explicit async timeout params (incl. timeouts.py) are deliberate public API."""
    assert "ASYNC109" in _ruff_lint().get("ignore", [])


def test_ruff_flake8_bugbear_treats_typer_defaults_as_immutable() -> None:
    """typer.Option / typer.Argument defaults are the Typer idiom, not B008 bugs."""
    bugbear = _ruff_lint()["flake8-bugbear"]
    assert bugbear["extend-immutable-calls"] == ["typer.Option", "typer.Argument"]


def test_ruff_hard_bans_the_zero_baseline_uncancel_api(tmp_path: Path) -> None:
    """Qualified uncancel access is rejected in addition to the AST instance-call guard."""
    tidy_imports = _ruff_lint()["flake8-tidy-imports"]
    assert "asyncio.Task.uncancel" in tidy_imports["banned-api"]

    fixture = tmp_path / "qualified_uncancel.py"
    fixture.write_text(
        "import asyncio\n\nqualified_uncancel = asyncio.Task.uncancel\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--config",
            str(REPO_ROOT / "pyproject.toml"),
            "--select",
            "TID251",
            str(fixture),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "TID251" in result.stdout
    assert "easycat._concurrency cancellation accounting" in result.stdout


def test_rule_list_docs_stay_in_sync_with_ruff_extensions() -> None:
    """CLAUDE.md and justfile prose must mirror tool.ruff.lint.extend-select."""
    expected = ", ".join(_ruff_lint()["extend-select"])

    claude_md = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert f"- Ruff extensions: {expected}" in claude_md

    justfile = (REPO_ROOT / "justfile").read_text(encoding="utf-8")
    assert f"# Lint with Ruff's defaults plus extensions ({expected})." in justfile
