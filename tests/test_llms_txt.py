"""Guards for the generated machine-facing docs (llms.txt / llms-full.txt)."""

from easycat.cli._app import _docs_entries
from scripts.regen_llms_txt import (
    LLMS_FULL_TXT,
    LLMS_TXT,
    render_llms_full_txt,
    render_llms_txt,
)


def test_llms_txt_matches_docs_route_map() -> None:
    assert LLMS_TXT.exists(), "llms.txt is missing. Run `uv run python scripts/regen_llms_txt.py`."
    assert LLMS_TXT.read_text(encoding="utf-8") == render_llms_txt(), (
        "llms.txt is stale. Run `uv run python scripts/regen_llms_txt.py`."
    )


def test_llms_full_txt_matches_docs_route_map() -> None:
    assert LLMS_FULL_TXT.exists(), (
        "llms-full.txt is missing. Run `uv run python scripts/regen_llms_txt.py`."
    )
    assert LLMS_FULL_TXT.read_text(encoding="utf-8") == render_llms_full_txt(), (
        "llms-full.txt is stale. Run `uv run python scripts/regen_llms_txt.py`."
    )


def test_llms_full_txt_preserves_route_taxonomy() -> None:
    rendered = render_llms_full_txt()

    for entry in _docs_entries():
        route_header = (
            f"## {entry['label']}\n\n"
            f"- Path: {entry['path']}\n"
            f"- URL: {entry['url']}\n"
            f"- Audience: {entry['audience']}\n"
            f"- Diataxis: {entry['diataxis']}\n"
        )
        assert route_header in rendered
