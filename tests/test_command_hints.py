from __future__ import annotations

from tests._command_hints import documented_commands


def test_documented_commands_handles_fences_and_inline_spans() -> None:
    section = """
```
uv run easycat docs
uv run easycat doctor --json  # Comment
```

Use `uv run easycat explain json-schema`.
Ignore `not a command`.
"""

    commands = documented_commands(section, prefixes=("uv run easycat ",))

    assert commands == (
        "uv run easycat docs",
        "uv run easycat doctor --json",
        "uv run easycat explain json-schema",
    )
