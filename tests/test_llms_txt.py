"""Guards for the generated machine-facing docs (llms.txt / llms-full.txt)."""

from pathlib import Path

from scripts.regen_llms_txt import (
    LLMS_FULL_TXT,
    LLMS_TXT,
    render_llms_full_txt,
    render_llms_txt,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


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


def test_llms_txt_points_machine_readers_to_json_surfaces() -> None:
    text = LLMS_TXT.read_text(encoding="utf-8")

    assert "easycat docs --json" in text
    assert "easycat explain json-schema" in text
    assert "llms-full.txt" in text


def test_llms_full_txt_carries_route_command_hints() -> None:
    text = LLMS_FULL_TXT.read_text(encoding="utf-8")

    assert "uv run easycat validate quick --json" in text
    assert "uv run easycat doctor --env-file .env --json" in text
    assert "Command note: " in text
