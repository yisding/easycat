"""``easycat explain`` — Rust ``cargo --explain`` pattern.

Reads from the single source-of-truth registry in
:mod:`easycat.errors` and the meta-entries in
:mod:`easycat.cli.diagnose._codes`.
"""

from __future__ import annotations

from typing import Any

import typer
from rich.panel import Panel
from rich.table import Table

from easycat.cli._errors import cli_command
from easycat.cli._output import emit_json, json_envelope, stderr_console, stdout_console
from easycat.cli.diagnose._codes import META_ENTRIES
from easycat.errors import REGISTRY, all_codes, get_entry, suggest_codes


def _normalize(code: str) -> str:
    """Allow users to type ``E102``, ``e102``, ``EASYCAT_E102``, etc."""
    normalized = code.strip().upper()
    if normalized.startswith("EASYCAT_"):
        return normalized
    if normalized.startswith("E") and normalized[1:].isdigit():
        return f"EASYCAT_{normalized}"
    return normalized


def _render_entry(code: str) -> None:
    entry = get_entry(code)
    assert entry is not None, f"_render_entry called for unregistered {code}"
    stdout_console.print(
        Panel(
            entry.headline,
            title=f"[bold]{entry.code}[/]",
            border_style="cyan",
            padding=(0, 2),
        )
    )
    stdout_console.print()
    stdout_console.print("[bold]Cause[/]")
    stdout_console.print(f"  {entry.cause}")
    stdout_console.print()
    stdout_console.print("[bold]Fix[/]")
    stdout_console.print(f"  {entry.fix}")
    if entry.example:
        stdout_console.print()
        stdout_console.print("[bold]Example[/]")
        stdout_console.print(f"  [dim]{entry.example}[/]")
    if entry.related:
        stdout_console.print()
        stdout_console.print("[bold]Related[/]")
        stdout_console.print(f"  {', '.join(entry.related)}")


def _render_meta(slug: str) -> None:
    entry = META_ENTRIES[slug]
    stdout_console.print(
        Panel(
            entry.headline,
            title=f"[bold]{entry.slug}[/]",
            border_style="magenta",
            padding=(0, 2),
        )
    )
    stdout_console.print()
    stdout_console.print(entry.body)


def _list_headline(template: str) -> str:
    """Render a registry template as a readable one-liner for the list view.

    Substitutes every ``{var!r}`` / ``{var}`` placeholder with ``<var>``
    so ``"Target directory {target!r} already exists."`` reads as
    ``"Target directory <target> already exists."``.
    """
    import re

    return re.sub(r"\{([a-zA-Z_][a-zA-Z_0-9]*)(![rs])?\}", r"<\1>", template)


def _print_list() -> None:
    table = Table(title="EasyCat error codes", title_justify="left")
    table.add_column("Code", style="cyan", no_wrap=True)
    table.add_column("Headline", overflow="fold")
    for code in all_codes():
        entry = REGISTRY[code]
        table.add_row(code, _list_headline(entry.headline))
    stdout_console.print(table)
    stdout_console.print()
    stdout_console.print("[dim]Meta topics:[/]")
    for slug in sorted(META_ENTRIES):
        stdout_console.print(f"  [magenta]{slug}[/] — {META_ENTRIES[slug].headline}")


def _emit_json_list() -> None:
    payload: dict[str, Any] = json_envelope(
        "explain",
        status="ok",
        codes=[{"code": code, "headline": REGISTRY[code].headline} for code in all_codes()],
        meta=[
            {"slug": slug, "headline": META_ENTRIES[slug].headline}
            for slug in sorted(META_ENTRIES)
        ],
    )
    emit_json(payload)


def _emit_json_entry(code: str) -> None:
    entry = REGISTRY[code]
    emit_json(
        json_envelope(
            "explain",
            status="ok",
            code=entry.code,
            headline=entry.headline,
            cause=entry.cause,
            fix=entry.fix,
            example=entry.example,
            related=list(entry.related),
        )
    )


def _emit_json_meta(slug: str) -> None:
    entry = META_ENTRIES[slug]
    emit_json(
        json_envelope(
            "explain",
            status="ok",
            slug=entry.slug,
            headline=entry.headline,
            body=entry.body,
        )
    )


@cli_command
def explain(
    code: str = typer.Argument(
        None,
        metavar="CODE",
        help="Error code (e.g., E102) or meta topic (exit-codes, init-schema, json-schema).",
    ),
    list_codes: bool = typer.Option(
        False, "--list", help="Print every registered code and meta topic."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Look up an error code.  Example: ``easycat explain E102``."""
    if list_codes:
        if json_output:
            _emit_json_list()
        else:
            _print_list()
        raise typer.Exit(0)

    if code is None:
        stderr_console.print(
            "[red]✗[/] Pass an error code (e.g., [cyan]easycat explain E102[/]) or "
            "[cyan]--list[/] to see every registered code."
        )
        raise typer.Exit(2)

    raw = code.strip()
    # Meta topics use hyphenated slugs.
    if raw.lower() in META_ENTRIES:
        if json_output:
            _emit_json_meta(raw.lower())
        else:
            _render_meta(raw.lower())
        raise typer.Exit(0)

    normalized = _normalize(raw)
    if normalized in REGISTRY:
        if json_output:
            _emit_json_entry(normalized)
        else:
            _render_entry(normalized)
        raise typer.Exit(0)

    # Unknown — render suggestions if any.
    matches = suggest_codes(normalized)
    matches += [slug for slug in META_ENTRIES if slug.startswith(raw.lower())]
    if json_output:
        emit_json(
            json_envelope(
                "explain",
                status="error",
                code="EASYCAT_E501",
                query=raw,
                suggestions=matches,
                exit_code=2,
            )
        )
    else:
        stderr_console.print(f"  [red]✗[/] [red]EASYCAT_E501[/]: Unknown error code {raw!r}.")
        if matches:
            stderr_console.print(f"    Did you mean: {', '.join(matches)}?")
        stderr_console.print("    Run [cyan]easycat explain --list[/] for the full catalog.")
    raise typer.Exit(2)
