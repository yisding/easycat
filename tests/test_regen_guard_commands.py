"""Staleness guard for the generated docs/onboarding guard command surfaces.

The justfile is the single source of truth for the ``guard-*`` recipes.
``scripts/regen_guard_commands.py`` renders them into the
``auto:guard-commands`` Markdown blocks and the generated
``easycat.cli._guard_commands`` module; this test fails when any rendered
surface drifts from the justfile.
"""

from __future__ import annotations

from easycat.cli._app import _DOCS_ONBOARDING_GUARD_COMMANDS
from scripts._justfile import just_guard_recipes
from scripts.regen_guard_commands import ROOT, render_targets


def test_guard_command_surfaces_match_justfile() -> None:
    stale = [
        path.relative_to(ROOT).as_posix()
        for path, original, updated in render_targets()
        if original != updated
    ]

    assert not stale, (
        "Guard command surfaces are stale. Edit the guard recipe in the justfile, "
        "then run `uv run python scripts/regen_guard_commands.py`: " + ", ".join(stale)
    )


def test_cli_route_hints_import_generated_guard_commands() -> None:
    guards = just_guard_recipes(ROOT)

    assert _DOCS_ONBOARDING_GUARD_COMMANDS == tuple(f"just {guard.name}" for guard in guards)
    module = (ROOT / "src/easycat/cli/_guard_commands.py").read_text(encoding="utf-8")
    assert "DOCS_ONBOARDING_RAW_GUARD_COMMANDS" not in module
    assert "uv run pytest" not in module
