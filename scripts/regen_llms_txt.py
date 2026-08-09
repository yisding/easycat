"""Refresh ``llms.txt`` and ``llms-full.txt`` from the docs route map.

Both files are generated from the same source of truth as
``easycat docs --json`` — the docs route table in ``easycat.cli._app``:

* ``llms.txt`` — the llmstxt.org-style index: one link per maintained
  docs route with its audience label and description.
* ``llms-full.txt`` — the same routes expanded with their Diátaxis
  classification, every copyable command hint, and the shared command note.

Human docs carry a short pointer explaining that coding rules live in
AGENTS.md/CLAUDE.md while these files are for machine-readable docs route
discovery. Run after editing the docs route map; ``--check`` exits non-zero if
either file would change, which is what CI should call.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from easycat.cli._app import (
    _DOCS_COMMAND_NOTE,
    _DOCS_SOURCE_URL,
    _docs_entries,
)

ROOT = Path(__file__).resolve().parent.parent
LLMS_TXT = ROOT / "llms.txt"
LLMS_FULL_TXT = ROOT / "llms-full.txt"

_HEADER = f"""\
# EasyCat

> EasyCat is a Python voice bot framework: noise reduction -> VAD -> STT ->
> agent -> TTS, with pluggable providers at each stage and idiomatic bridges
> for OpenAI Agents SDK, PydanticAI, LangChain, LangGraph, LlamaAgents, the
> remote Responses API, or your own async workflow.

Machine-readable surfaces: `easycat docs --json` emits this route map with
command hints and audience labels; `easycat explain json-schema` documents the
standard `--json` envelope every CLI command shares. Bare `easycat` commands
use the installed CLI form; from this repository, prefix them with `uv run`.

Source: {_DOCS_SOURCE_URL}
"""


def render_llms_txt() -> str:
    """Render the llms.txt index from the docs route map."""
    lines = [_HEADER, "## Docs", ""]
    for entry in _docs_entries():
        lines.append(
            f"- [{entry['label']}]({entry['url']}) — for {entry['audience']}: "
            f"{entry['description']}"
        )
    lines += [
        "",
        "## Optional",
        "",
        (
            f"- [llms-full.txt]({_DOCS_SOURCE_URL}/blob/main/llms-full.txt) — every docs "
            "route above expanded with its copyable command hints."
        ),
        "",
    ]
    return "\n".join(lines)


def render_llms_full_txt() -> str:
    """Render the llms-full.txt expansion from the docs route map."""
    lines = [_HEADER, "Command note: " + _DOCS_COMMAND_NOTE, ""]
    for entry in _docs_entries():
        lines += [
            f"## {entry['label']}",
            "",
            f"- Path: {entry['path']}",
            f"- URL: {entry['url']}",
            f"- Audience: {entry['audience']}",
            f"- Diataxis: {entry['diataxis']}",
            f"- {entry['description']}",
        ]
        commands = entry.get("commands", ())
        if commands:
            lines += ["", "Commands:", "", "```bash"]
            lines.extend(commands)
            lines.append("```")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if llms.txt or llms-full.txt would change.",
    )
    args = parser.parse_args(argv)

    stale: list[str] = []
    for path, rendered in (
        (LLMS_TXT, render_llms_txt()),
        (LLMS_FULL_TXT, render_llms_full_txt()),
    ):
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == rendered:
            continue
        if args.check:
            stale.append(path.relative_to(ROOT).as_posix())
        else:
            path.write_text(rendered, encoding="utf-8")
            print(f"wrote {path.relative_to(ROOT).as_posix()}")

    if stale:
        print(
            "Stale generated files: "
            + ", ".join(stale)
            + ". Run `uv run python scripts/regen_llms_txt.py`.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
