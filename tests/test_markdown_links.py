"""Guard maintained Markdown docs against broken local links."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import unquote

from tests._markdown import github_markdown_heading_anchors

REPO_ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"!?\[(?:\\.|[^\]\\])+\]\((?P<target>[^)\n]+)\)")
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
UV_EXTRA_RE = re.compile(r"--extra\s+(?P<extra>[A-Za-z0-9_.-]+)")
EASYCAT_EXTRA_RE = re.compile(r"easycat\[(?P<extras>[^\]]+)\]")
LINE_ANCHOR_RE = re.compile(r"^L(?P<start>\d+)(?:-L(?P<end>\d+))?$")
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
    files.update(_docs_route_markdown_files())
    files.update(
        (REPO_ROOT / "src" / "easycat" / "cli" / "scaffold" / "templates").rglob("README.md")
    )
    files.add(REPO_ROOT / "examples" / "README.md")
    return sorted(path for path in files if path.exists())


def _docs_route_markdown_files() -> set[Path]:
    from easycat.cli._app import _DOCS_LINKS

    files: set[Path] = set()
    for entry in _DOCS_LINKS:
        route = entry["path"].split("#", 1)[0]
        if not route:
            continue
        path = REPO_ROOT / route
        if path.is_dir():
            files.update(path.rglob("*.md"))
        elif path.suffix == ".md":
            files.add(path)
    return files


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


def _line_anchor_error(path: Path, fragment: str) -> str | None:
    match = LINE_ANCHOR_RE.match(fragment)
    if match is None:
        return None

    start = int(match.group("start"))
    end = int(match.group("end") or start)
    if start < 1 or end < start:
        return f"invalid line anchor #{fragment}"

    line_count = len(path.read_text(encoding="utf-8").splitlines())
    if end > line_count:
        return f"line anchor #{fragment} exceeds {line_count} lines"
    return None


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
                anchors = github_markdown_heading_anchors(destination)
                if unquote(fragment) not in anchors:
                    rel_destination = destination.relative_to(REPO_ROOT)
                    broken.append(
                        f"{rel_source}:{line_number}: missing anchor #{fragment} "
                        f"in {rel_destination}"
                    )
            elif fragment:
                rel_destination = destination.relative_to(REPO_ROOT)
                error = _line_anchor_error(destination, unquote(fragment))
                if error is not None:
                    broken.append(f"{rel_source}:{line_number}: {error} in {rel_destination}")

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

    assert github_markdown_heading_anchors(page) == {
        "root",
        "repeated",
        "repeated-1",
        "repeated-2",
        "repeated-3",
    }


def test_line_anchor_error_checks_github_line_ranges(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text("one\ntwo\nthree\n", encoding="utf-8")

    assert _line_anchor_error(source, "L1") is None
    assert _line_anchor_error(source, "L2-L3") is None
    assert _line_anchor_error(source, "L4") == "line anchor #L4 exceeds 3 lines"
    assert _line_anchor_error(source, "L3-L2") == "invalid line anchor #L3-L2"
    assert _line_anchor_error(source, "not-a-line-anchor") is None


def test_current_user_docs_reference_known_easycat_extras() -> None:
    known = _known_extras()
    unknown: list[str] = []

    for path in _current_user_markdown_files():
        rel_path = path.relative_to(REPO_ROOT)
        text = path.read_text(encoding="utf-8")
        for match in UV_EXTRA_RE.finditer(text):
            extra = match.group("extra").strip().rstrip(".,;:")
            if not extra or _looks_like_placeholder(extra):
                continue
            if extra not in known:
                unknown.append(f"{rel_path}: unknown --extra {extra!r}")
        for match in EASYCAT_EXTRA_RE.finditer(text):
            for extra in (part.strip() for part in match.group("extras").split(",")):
                if not extra or _looks_like_placeholder(extra):
                    continue
                if extra not in known:
                    unknown.append(f"{rel_path}: unknown easycat extra {extra!r}")

    assert not unknown, "Unknown EasyCat optional extras in user docs:\n" + "\n".join(unknown)
