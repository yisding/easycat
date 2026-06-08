from __future__ import annotations

from pathlib import Path

FORBIDDEN_RELEASE_ARTIFACT_PARTS = frozenset(
    {
        ".easycat",
        ".coverage",
        ".agents",
        ".claude",
        ".codex",
        ".git",
        ".github",
        ".hypothesis",
        ".mypy_cache",
        ".mutmut-cache",
        ".pipecat-bench",
        ".pytest_cache",
        ".ruff_cache",
        ".uv-cache",
        ".venv",
        "__pycache__",
        "build",
        "coverage.xml",
        "dist",
        "docs",
        "htmlcov",
        "mutants",
        "plan",
        "site",
        "tests",
    }
)
FORBIDDEN_RELEASE_ARTIFACT_SUFFIXES = (".key", ".pem", ".pyc", ".pyo")
REQUIRED_BUILD_SOURCE_EXCLUDES = frozenset(
    {
        "__pycache__",
        "*.pyc",
        "*.pyo",
        "*.key",
        "*.pem",
        "*.egg-info",
        "*.egg-info/**",
        ".agents",
        ".agents/**",
        ".claude",
        ".claude/**",
        ".codex",
        ".codex/**",
        ".coverage",
        ".coverage.*",
        ".easycat",
        ".easycat/**",
        ".git",
        ".git/**",
        ".github",
        ".github/**",
        ".hypothesis",
        ".hypothesis/**",
        ".mypy_cache",
        ".mypy_cache/**",
        ".mutmut-cache",
        ".mutmut-cache/**",
        ".pipecat-bench",
        ".pipecat-bench/**",
        ".pytest_cache",
        ".pytest_cache/**",
        ".ruff_cache",
        ".ruff_cache/**",
        ".uv-cache",
        ".uv-cache/**",
        ".venv",
        ".venv/**",
        "build",
        "build/**",
        "coverage.xml",
        "dist",
        "dist/**",
        "docs",
        "docs/**",
        "htmlcov",
        "htmlcov/**",
        "mutants",
        "mutants/**",
        "plan",
        "plan/**",
        "site",
        "site/**",
        "tests",
        "tests/**",
    }
)


def release_artifact_offenders(members: list[str]) -> list[str]:
    offenders = []
    for member in members:
        parts = set(Path(member).parts)
        if (
            parts & FORBIDDEN_RELEASE_ARTIFACT_PARTS
            or member.endswith(FORBIDDEN_RELEASE_ARTIFACT_SUFFIXES)
            or any(part.endswith(".egg-info") for part in parts)
        ):
            offenders.append(member)
    return offenders
