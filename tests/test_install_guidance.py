"""Static guards for optional-extra install guidance."""

from __future__ import annotations

import ast
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
MARKDOWN_PREREQS_RE = re.compile(
    r"^## Prerequisites\n(?P<body>.*?)(?=^## |\Z)",
    re.MULTILINE | re.DOTALL,
)
PROVIDER_EXTRA_BY_ENV_VAR = {
    "DEEPGRAM_API_KEY": "--extra deepgram",
    "ELEVENLABS_API_KEY": "--extra elevenlabs",
}


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


def test_teaching_ladder_prerequisites_run_doctor_after_setup() -> None:
    """The teaching overview should send readers through the first-run preflight."""
    readme = (REPO_ROOT / "docs" / "teaching" / "README.md").read_text(encoding="utf-8")

    sync_index = readme.index("uv sync --extra quickstart --group dev")
    key_index = readme.index("OPENAI_API_KEY")
    doctor_index = readme.index("uv run easycat doctor")

    assert sync_index < key_index < doctor_index


def test_teaching_chapter_key_prerequisites_run_doctor() -> None:
    """Self-contained chapter READMEs with API keys should repeat the preflight."""
    missing: list[str] = []
    teaching_root = REPO_ROOT / "docs" / "teaching"

    for path in sorted(teaching_root.glob("[0-9][0-9]-*/README.md")):
        text = path.read_text(encoding="utf-8")
        match = MARKDOWN_PREREQS_RE.search(text)
        if match is None:
            continue
        section = match.group("body")
        if "API_KEY" in section and "uv run easycat doctor" not in section:
            missing.append(path.relative_to(REPO_ROOT).as_posix())

    assert not missing, "Teaching chapter prerequisites missing doctor preflight:\n" + "\n".join(
        missing
    )


def test_readme_optional_dependency_list_has_copyable_install_commands() -> None:
    """The optional-dependency list should not name packages without commands."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    optional_block = readme.split(
        "Optional dependencies you may need depending on providers/transports:", 1
    )[1].split("## CLI", 1)[0]

    for command in (
        "uv sync --extra local",
        "uv sync --extra webrtc",
        "uv sync --extra smart-turn",
        "uv sync --extra ten-vad",
        "uv sync --extra silero-vad",
        "uv sync --extra funasr-vad",
        "uv sync --extra rnnoise",
        "uv sync --extra openai",
        "uv sync --extra deepgram",
        "uv sync --extra elevenlabs",
        "uv sync --extra cartesia",
        "uv pip install krisp_audio",
    ):
        assert command in optional_block


def test_teaching_provider_key_setup_names_required_extras() -> None:
    """Provider-key setup snippets should include the matching optional extra."""
    missing: list[str] = []
    teaching_root = REPO_ROOT / "docs" / "teaching"

    for path in sorted(teaching_root.rglob("*.py")):
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        doc = ast.get_docstring(module) or ""
        if "Dependencies:" not in doc:
            continue
        rel = path.relative_to(REPO_ROOT)
        for env_var, extra in PROVIDER_EXTRA_BY_ENV_VAR.items():
            if env_var in doc and extra not in doc:
                missing.append(f"{rel}: {env_var} setup missing {extra}")

    readme_paths = sorted({*teaching_root.rglob("README.md"), teaching_root / "README.md"})
    for path in readme_paths:
        text = path.read_text(encoding="utf-8")
        match = MARKDOWN_PREREQS_RE.search(text)
        if match is None:
            continue
        section = match.group("body")
        rel = path.relative_to(REPO_ROOT)
        for env_var, extra in PROVIDER_EXTRA_BY_ENV_VAR.items():
            if env_var in section and extra not in section:
                missing.append(f"{rel}: {env_var} prerequisites missing {extra}")

    assert not missing, "Teaching setup docs missing provider extras:\n" + "\n".join(missing)


def test_quickstart_guidance_does_not_readd_bundled_extras() -> None:
    """``quickstart`` already includes common local extras; avoid redundant setup."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = pyproject["project"]["optional-dependencies"]
    bundled_extras = ("rnnoise", "smart-turn")
    for extra in bundled_extras:
        assert set(extras[extra]).issubset(set(extras["quickstart"]))

    redundant: list[str] = []
    extra_pattern = "|".join(re.escape(extra) for extra in bundled_extras)
    pattern = re.compile(rf"--extra\s+quickstart[^\n`|]*--extra\s+(?:{extra_pattern})")
    for path in _iter_guidance_files():
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT).as_posix()
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            redundant.append(f"{rel}:{line}")

    assert not redundant, (
        "Guidance should not re-add extras that `quickstart` already bundles: "
        + "; ".join(redundant)
    )
