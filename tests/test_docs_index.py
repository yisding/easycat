"""Guards for the top-level documentation map."""

import re
from pathlib import Path

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


def test_cli_docs_routes_are_represented_in_docs_index() -> None:
    docs_links = _root_relative_doc_links()
    missing = [
        entry["path"]
        for entry in _DOCS_LINKS
        if isinstance(entry.get("path"), str) and entry["path"] not in docs_links
    ]

    assert not missing, "easycat docs routes missing from docs/README.md: " + ", ".join(missing)


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
    assert all(
        entry["url"].startswith("https://github.com/yisding/easycat/")
        for entry in entries.values()
    )
