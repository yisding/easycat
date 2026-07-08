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
        "runs",
        "site",
        "tests",
    }
)
FORBIDDEN_RELEASE_ARTIFACT_SUFFIXES = (".key", ".pem", ".pyc", ".pyo")
SCAFFOLD_COPY_IGNORED_DIRECTORIES = frozenset(
    {
        "__pycache__",
        ".agents",
        ".claude",
        ".codex",
        ".easycat",
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
        "build",
        "dist",
        "htmlcov",
        "mutants",
        "runs",
        "site",
    }
)
SCAFFOLD_COPY_IGNORED_FILES = frozenset({".coverage", "coverage.xml"})
SCAFFOLD_COPY_IGNORED_FILE_PREFIXES = (".coverage.",)
SCAFFOLD_COPY_IGNORED_PART_SUFFIXES = (".egg-info",)
SCAFFOLD_COPY_IGNORED_SUFFIXES = FORBIDDEN_RELEASE_ARTIFACT_SUFFIXES
GENERATED_PROJECT_GITIGNORE_PATTERNS = (
    ".env",
    ".env.*",
    "!.env.example",
    "*.pem",
    "*.key",
    ".venv/",
    "*.egg-info/",
    ".agents/",
    ".claude/",
    ".codex",
    ".codex/",
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    ".hypothesis/",
    ".mypy_cache/",
    ".pipecat-bench/",
    ".ruff_cache/",
    ".pytest_cache/",
    ".uv-cache/",
    ".coverage",
    ".coverage.*",
    "coverage.xml",
    "htmlcov/",
    "dist/",
    "build/",
    "site/",
    "mutants/",
    ".mutmut-cache",
    ".easycat/",
    "runs/",
)
# Scaffold templates deliberately ship an offline test suite
# (``tests/test_agent.py``); a ``tests`` path part under this marker —
# or anywhere inside a scaffolded project — is not an offender.
SCAFFOLD_TEMPLATE_PATH_MARKER = "cli/scaffold/templates/"


def release_artifact_offenders(members: list[str], *, scaffold_project: bool = False) -> list[str]:
    offenders = []
    for member in members:
        parts = set(Path(member).parts)
        forbidden = set(parts & FORBIDDEN_RELEASE_ARTIFACT_PARTS)
        if "tests" in forbidden and (
            scaffold_project or SCAFFOLD_TEMPLATE_PATH_MARKER in Path(member).as_posix()
        ):
            forbidden.discard("tests")
        if (
            forbidden
            or member.endswith(FORBIDDEN_RELEASE_ARTIFACT_SUFFIXES)
            or any(part.endswith(".egg-info") for part in parts)
        ):
            offenders.append(member)
    return offenders
