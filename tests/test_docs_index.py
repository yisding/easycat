"""Guards for the top-level documentation map."""

import re
import string
from pathlib import Path
from urllib.parse import unquote

from easycat.cli._app import _DOCS_LINKS, _docs_entries

REPO_ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"\[[^\]]+\]\((?P<target>[^)\n]+)\)")


def _root_relative_doc_links() -> set[str]:
    path = REPO_ROOT / "docs" / "README.md"
    links = {"docs/README.md"}
    for match in LINK_RE.finditer(path.read_text(encoding="utf-8")):
        raw_target = match.group("target")
        target_path, sep, fragment = raw_target.partition("#")
        if target_path.startswith(("http://", "https://")):
            continue
        resolved = (path.parent / target_path).resolve()
        rel = resolved.relative_to(REPO_ROOT).as_posix()
        if raw_target.endswith("/") and not rel.endswith("/"):
            rel += "/"
        if sep:
            rel = f"{rel}#{fragment}"
        links.add(rel)
    return links


def _heading_anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#{1,6}\s+(?P<title>.+?)\s*$", line)
        if match is None:
            continue
        title = re.sub(r"\s+#+$", "", match.group("title").strip())
        slug = title.lower()
        slug = slug.translate(str.maketrans("", "", string.punctuation.replace("-", "")))
        slug = re.sub(r"\s+", "-", slug).strip("-")
        if slug:
            duplicate_index = counts.get(slug, 0)
            counts[slug] = duplicate_index + 1
            anchors.add(slug if duplicate_index == 0 else f"{slug}-{duplicate_index}")
    return anchors


def test_docs_heading_anchors_match_github_duplicate_suffixes(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    page.write_text("# Root\n## Route\n## Route\n## Route!\n", encoding="utf-8")

    assert _heading_anchors(page) == {"root", "route", "route-1", "route-2"}


def test_docs_index_routes_primary_reader_paths() -> None:
    text = (REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    required_links = [
        "../README.md#install",
        "teaching/",
        "teaching/00-hello-audio/",
        "../README.md#cli",
        "../examples/README.md",
        "public-api.md",
        "../CONTRIBUTING.md",
        "deployment/docker.md",
        "observability.md",
        "../README.md#validation-workflow",
        "../plan/validation/reference.md",
    ]

    missing = [link for link in required_links if link not in text]

    assert not missing, "docs/README.md missing route links: " + ", ".join(missing)


def test_docs_index_points_to_docs_command() -> None:
    text = (REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")

    assert "uv run easycat docs" in text
    assert "installed app environment" in text
    assert "prints the same map" in text
    assert "uv run easycat doctor --env-file .env" in text
    assert "uv run easycat explain json-schema" in text
    assert "standard `--json` envelope" in text


def test_cli_docs_routes_are_represented_in_docs_index() -> None:
    docs_links = _root_relative_doc_links()
    missing = [
        entry["path"]
        for entry in _DOCS_LINKS
        if isinstance(entry.get("path"), str) and entry["path"] not in docs_links
    ]

    assert not missing, "easycat docs routes missing from docs/README.md: " + ", ".join(missing)


def test_cli_docs_routes_are_unique() -> None:
    labels = [entry["label"] for entry in _DOCS_LINKS]
    paths = [entry["path"] for entry in _DOCS_LINKS]

    duplicate_labels = sorted({label for label in labels if labels.count(label) > 1})
    duplicate_paths = sorted({path for path in paths if paths.count(path) > 1})

    assert not duplicate_labels, "easycat docs route labels are duplicated: " + ", ".join(
        duplicate_labels
    )
    assert not duplicate_paths, "easycat docs route paths are duplicated: " + ", ".join(
        duplicate_paths
    )


def test_cli_docs_routes_resolve_locally() -> None:
    broken: list[str] = []

    for entry in _DOCS_LINKS:
        route, _, fragment = entry["path"].partition("#")
        destination = REPO_ROOT / route.rstrip("/")
        if not destination.exists():
            broken.append(f"{entry['label']}: missing {entry['path']}")
            continue
        if fragment and destination.suffix == ".md":
            anchors = _heading_anchors(destination)
            if unquote(fragment) not in anchors:
                broken.append(f"{entry['label']}: missing #{fragment} in {route}")

    assert not broken, "easycat docs routes are stale:\n" + "\n".join(broken)


def test_cli_docs_routes_have_descriptions() -> None:
    missing = [
        f"{entry['label']} ({entry['path']})"
        for entry in _DOCS_LINKS
        if len(entry.get("description", "").split()) < 4
    ]

    assert not missing, "easycat docs routes missing useful descriptions: " + ", ".join(missing)


def test_cli_docs_routes_have_online_urls() -> None:
    entries = {entry["path"]: entry for entry in _docs_entries()}

    assert entries["README.md#install"]["url"].endswith("/blob/main/README.md#install")
    assert entries["docs/README.md"]["url"].endswith("/blob/main/docs/README.md")
    assert entries["docs/teaching/"]["url"].endswith("/tree/main/docs/teaching")
    assert entries["docs/teaching/00-hello-audio/"]["url"].endswith(
        "/tree/main/docs/teaching/00-hello-audio"
    )
    for route, entry in entries.items():
        route_path = route.split("#", 1)[0]
        expected_kind = "/tree/main/" if route_path.endswith("/") else "/blob/main/"
        assert expected_kind in entry["url"], route
    assert all(
        entry["url"].startswith("https://github.com/yisding/easycat/")
        for entry in entries.values()
    )
