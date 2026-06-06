"""Guards for the top-level documentation map."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_docs_index_routes_primary_reader_paths() -> None:
    text = (REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    required_links = [
        "../README.md#install",
        "teaching/",
        "teaching/00-hello-audio/",
        "../README.md#cli",
        "public-api.md",
        "../CONTRIBUTING.md",
        "deployment/docker.md",
        "observability.md",
        "../README.md#validation-workflow",
        "../plan/validation/reference.md",
    ]

    missing = [link for link in required_links if link not in text]

    assert not missing, "docs/README.md missing route links: " + ", ".join(missing)
