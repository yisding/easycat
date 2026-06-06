"""Guard maintained Markdown docs against broken local links."""

from __future__ import annotations

import re
import string
import tomllib
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"!?\[(?:\\.|[^\]\\])+\]\((?P<target>[^)\n]+)\)")
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
UV_EXTRA_RE = re.compile(r"--extra\s+(?P<extra>[A-Za-z0-9_.-]+)")
EASYCAT_EXTRA_RE = re.compile(r"easycat\[(?P<extras>[^\]]+)\]")
EXTERNAL_SCHEMES = ("http://", "https://", "mailto:", "tel:")


def _maintained_markdown_files() -> list[Path]:
    files: set[Path] = set(REPO_ROOT.glob("*.md"))
    for directory in (REPO_ROOT / "docs", REPO_ROOT / "plan"):
        files.update(directory.rglob("*.md"))
    files.update(
        (REPO_ROOT / "src" / "easycat" / "cli" / "scaffold" / "templates").rglob("README.md")
    )
    files.update(
        {
            REPO_ROOT / "examples" / "README.md",
            REPO_ROOT / "tests" / "cli" / "TEST_PLANS.md",
            REPO_ROOT / "tests" / "contracts" / "README.md",
        }
    )
    return sorted(path for path in files if path.exists())


def _current_user_markdown_files() -> list[Path]:
    files: set[Path] = set(REPO_ROOT.glob("*.md"))
    files.update((REPO_ROOT / "docs").rglob("*.md"))
    files.add(REPO_ROOT / "examples" / "README.md")
    return sorted(path for path in files if path.exists())


def _links_in(path: Path) -> Iterable[tuple[int, str]]:
    in_fence = False
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        line = INLINE_CODE_RE.sub("", line)
        for match in LINK_RE.finditer(line):
            yield line_number, match.group("target")


def _normalize_target(raw_target: str) -> str:
    target = raw_target.strip().strip("<>")
    if " " in target:
        target = target.split()[0]
    return target


def _is_external(target: str) -> bool:
    lower = target.lower()
    return lower.startswith(EXTERNAL_SCHEMES)


def _resolve_local_path(source: Path, target: str) -> Path:
    path = unquote(target.split("#", 1)[0])
    if not path:
        return source.resolve()
    base = REPO_ROOT if path.startswith("/") else source.parent
    return (base / path.lstrip("/")).resolve()


def _heading_anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#{1,6}\s+(?P<title>.+?)\s*$", line)
        if not match:
            continue
        title = match.group("title").strip()
        title = re.sub(r"\s+#+$", "", title)
        slug = title.lower()
        slug = slug.translate(str.maketrans("", "", string.punctuation.replace("-", "")))
        slug = re.sub(r"\s+", "-", slug).strip("-")
        if slug:
            duplicate_index = counts.get(slug, 0)
            counts[slug] = duplicate_index + 1
            anchors.add(slug if duplicate_index == 0 else f"{slug}-{duplicate_index}")
    return anchors


def _known_extras() -> set[str]:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return set(pyproject["project"]["optional-dependencies"])


def _looks_like_placeholder(extra: str) -> bool:
    return any(char in extra for char in "<>{}$") or extra in {"...", "NAME"}


def test_maintained_markdown_local_links_resolve() -> None:
    broken: list[str] = []

    for path in _maintained_markdown_files():
        for line_number, raw_target in _links_in(path):
            target = _normalize_target(raw_target)
            if _is_external(target):
                continue

            destination = _resolve_local_path(path, target)
            rel_source = path.relative_to(REPO_ROOT)
            if not destination.exists():
                broken.append(f"{rel_source}:{line_number}: missing target {target!r}")
                continue

            fragment = target.split("#", 1)[1] if "#" in target else ""
            if fragment and destination.suffix == ".md":
                anchors = _heading_anchors(destination)
                if unquote(fragment) not in anchors:
                    rel_destination = destination.relative_to(REPO_ROOT)
                    broken.append(
                        f"{rel_source}:{line_number}: missing anchor #{fragment} "
                        f"in {rel_destination}"
                    )

    assert not broken, "Broken local Markdown links:\n" + "\n".join(broken)


def test_markdown_heading_anchors_match_github_duplicate_suffixes(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    page.write_text(
        "\n".join(
            (
                "# Root",
                "## Repeated",
                "## Repeated",
                "## Repeated!",
                "## Repeated",
            )
        ),
        encoding="utf-8",
    )

    assert _heading_anchors(page) == {
        "root",
        "repeated",
        "repeated-1",
        "repeated-2",
        "repeated-3",
    }


def test_current_user_docs_reference_known_easycat_extras() -> None:
    known = _known_extras()
    unknown: list[str] = []

    for path in _current_user_markdown_files():
        rel_path = path.relative_to(REPO_ROOT)
        text = path.read_text(encoding="utf-8")
        for match in UV_EXTRA_RE.finditer(text):
            extra = match.group("extra")
            if extra not in known:
                unknown.append(f"{rel_path}: unknown --extra {extra!r}")
        for match in EASYCAT_EXTRA_RE.finditer(text):
            for extra in (part.strip() for part in match.group("extras").split(",")):
                if not extra or _looks_like_placeholder(extra):
                    continue
                if extra not in known:
                    unknown.append(f"{rel_path}: unknown easycat extra {extra!r}")

    assert not unknown, "Unknown EasyCat optional extras in user docs:\n" + "\n".join(unknown)
