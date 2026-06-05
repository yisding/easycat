"""Static guards for optional-extra install guidance."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".conf",
    ".html",
    ".md",
    ".py",
    ".rst",
    ".service",
    ".sh",
    ".toml",
    ".txt",
    ".yml",
    ".yaml",
}
GUIDANCE_TARGETS = (
    REPO_ROOT / "src" / "easycat",
    REPO_ROOT / "docs",
    REPO_ROOT / "examples",
    REPO_ROOT / "README.md",
    REPO_ROOT / "CONTRIBUTING.md",
    REPO_ROOT / "AGENTS.md",
)
STALE_INSTALL_PATTERNS = (
    (
        "pip install easycat extra",
        re.compile(r"\bpip install\s+[`'\"]?easycat\["),
    ),
    (
        "editable local extra install",
        re.compile(r"\b(?:uv\s+)?pip\s+install\s+-e\s+[`'\"]?\.\["),
    ),
    (
        "unquoted uv add easycat extra",
        re.compile(r"\buv add\s+easycat\["),
    ),
    (
        "bare install easycat extra",
        re.compile(r"\binstall\s+easycat\[", re.IGNORECASE),
    ),
)
UV_EXTRA_RE = re.compile(r"--extra\s+(?P<extra>[A-Za-z0-9_.-]+)")
EASYCAT_EXTRA_RE = re.compile(r"easycat\[(?P<extras>[^\]]+)\]")


def _iter_guidance_files() -> list[Path]:
    files: list[Path] = []
    for target in GUIDANCE_TARGETS:
        if target.is_file():
            files.append(target)
            continue
        if target.is_dir():
            files.extend(path for path in target.rglob("*") if path.suffix in TEXT_SUFFIXES)
    return sorted(files)


def _known_extras() -> set[str]:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return set(pyproject["project"]["optional-dependencies"])


def _looks_like_placeholder(extra: str) -> bool:
    return any(char in extra for char in "<>{}$") or extra in {"...", "NAME"}


def _normalize_extra(extra: str) -> str:
    return extra.strip().rstrip(".,;:")


def test_optional_extra_guidance_uses_current_uv_commands() -> None:
    """Keep onboarding hints aligned for package users and repo-local developers."""
    stale: list[str] = []

    for path in _iter_guidance_files():
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT).as_posix()
        for label, pattern in STALE_INSTALL_PATTERNS:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                stale.append(f"{rel}:{line}: {label}")

    assert not stale, (
        "Optional-extra guidance should use `uv add 'easycat[...]'` and, for repo-local "
        "setup, `uv sync --extra ...`: " + "; ".join(stale)
    )


def test_optional_extra_guidance_references_known_extras() -> None:
    """Catch typoed extras in source/doc install hints before users copy them."""
    known = _known_extras()
    unknown: list[str] = []

    for path in _iter_guidance_files():
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT).as_posix()

        for match in UV_EXTRA_RE.finditer(text):
            extra = _normalize_extra(match.group("extra"))
            if extra not in known:
                line = text.count("\n", 0, match.start()) + 1
                unknown.append(f"{rel}:{line}: unknown --extra {extra!r}")

        for match in EASYCAT_EXTRA_RE.finditer(text):
            for extra in (_normalize_extra(part) for part in match.group("extras").split(",")):
                if not extra or _looks_like_placeholder(extra):
                    continue
                if extra not in known:
                    line = text.count("\n", 0, match.start()) + 1
                    unknown.append(f"{rel}:{line}: unknown easycat extra {extra!r}")

    assert not unknown, "Unknown EasyCat optional extras in install guidance:\n" + "\n".join(
        unknown
    )
