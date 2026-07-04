"""``easycat diff`` — two-source bundle diff of milestones and transcripts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.markup import escape
from rich.table import Table

from easycat.cli._errors import cli_command
from easycat.cli._output import (
    emit_command_error,
    emit_json,
    json_envelope,
    stderr_console,
    stdout_console,
    warn,
)
from easycat.cli.debug._common import _format_ms, _load_bundle_or_journal, _print_wide
from easycat.debug._turn_diff import diff_bundles
from easycat.validation.redaction import REDACTED_TRANSCRIPT

# Transcript fields whose free-form text must be suppressed before any diff
# result is emitted (JSON envelope or human table).  A regressed milestone or
# cost delta is just numbers; transcript bodies can carry arbitrary sensitive
# caller/agent text, so substring redaction is not sufficient here.
_DIFF_TRANSCRIPT_TEXT_FIELDS = ("user_a", "user_b", "agent_a", "agent_b")


def _redact_diff_result(result: dict[str, Any]) -> dict[str, Any]:
    """Suppress every transcript body in a ``diff_bundles`` result in place.

    The diff engine carries raw user/agent transcripts only long enough to
    compute the ``changed`` flag.  Before any CLI output, replace those bodies
    with a constant marker so arbitrary conversation content cannot leak to
    stdout, CI logs, or shared JSON artifacts.  Milestones, costs, and the
    summary are numbers and pass through untouched.
    """
    for turn in result.get("turns", ()):
        transcript = turn.get("transcript")
        if not isinstance(transcript, dict):
            continue
        for field_name in _DIFF_TRANSCRIPT_TEXT_FIELDS:
            value = transcript.get(field_name)
            if isinstance(value, str) and value:
                transcript[field_name] = REDACTED_TRANSCRIPT
    return result


def _diff_turn_filter(result: dict[str, Any], turn: str | None) -> dict[str, Any]:
    """Restrict a diff result to a single positional turn ``index`` (string).

    ``turn`` is matched against each turn's positional ``index`` so a user can
    drill into one before/after pair.  The summary is left intact so the
    worst-regression headline still reflects the whole run.
    """
    if turn is None:
        return result
    try:
        wanted = int(turn)
    except (TypeError, ValueError):
        wanted = None
    filtered = [t for t in result.get("turns", ()) if wanted is not None and t["index"] == wanted]
    return {**result, "turns": filtered}


def _diff_table(turns: list[dict[str, Any]]) -> Table:
    """Render the per-turn diff: regressed milestones in red, drift.

    One row per aligned turn pair: the positional index, both turn ids, each
    milestone's ``a→b`` delta (red when it regressed), and whether the
    transcript changed.  Unmatched turns (a dropped or extra turn) render the
    missing side as ``-``.
    """
    table = Table(
        title="Two-source diff (ms) — regressions in red",
        show_header=True,
        header_style="bold",
        box=None,
        padding=(0, 1),
        title_justify="left",
    )
    table.add_column("idx", justify="right", no_wrap=True)
    table.add_column("turn (a→b)", no_wrap=True, overflow="fold")
    table.add_column("milestones (Δms)", overflow="fold")
    table.add_column("transcript", no_wrap=True)
    for turn in turns:
        turn_a = turn.get("turn_id_a")
        turn_b = turn.get("turn_id_b")
        turn_label = f"{turn_a or '-'}→{turn_b or '-'}"
        if turn.get("unmatched"):
            turn_label = f"[yellow]{escape(turn_label)} (unmatched)[/]"
        else:
            turn_label = escape(turn_label)
        cells = []
        for name, cell in (turn.get("milestones") or {}).items():
            delta = cell.get("delta_ms")
            text = f"{name.removesuffix('_ms')}={_format_ms(delta)}"
            cells.append(f"[red]{escape(text)}[/]" if cell.get("regressed") else escape(text))
        milestone_text = ", ".join(cells) if cells else "[dim](none)[/]"
        transcript = turn.get("transcript") or {}
        drift = "[yellow]changed[/]" if transcript.get("changed") else "same"
        table.add_row(
            str(turn.get("index", "")),
            turn_label,
            milestone_text,
            drift,
        )
    return table


@cli_command
def diff_command(
    path_a: Path = typer.Argument(
        ...,
        help="Baseline bundle or ``.sqlite`` journal (the 'before' run).",
    ),
    path_b: Path = typer.Argument(
        ...,
        help="Comparison bundle or ``.sqlite`` journal (the 'after' run).",
    ),
    turn: str | None = typer.Option(
        None,
        "--turn",
        help="Restrict the diff to a single positional turn index.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Diff two bundles turn-by-turn: milestone and transcript deltas.

    Aligns turns positionally (turn 0 of A vs turn 0 of B) and reports each
    milestone's ``b - a`` delta, whether it regressed (default: >10% AND >5ms
    slower), and whether the transcript drifted.  The summary names the single
    worst regression across the whole run.  Transcript text is redacted before
    it is printed.  See docs/latency.md.
    """
    if turn is not None:
        try:
            int(turn)
        except (TypeError, ValueError):
            emit_command_error(
                "diff",
                f"--turn must be an integer turn index (got {turn!r}).",
                json_output=json_output,
                exit_code=2,
            )
            raise typer.Exit(2)

    bundle_a = _load_bundle_or_journal(path_a, command="diff", json_output=json_output)
    bundle_b = _load_bundle_or_journal(path_b, command="diff", json_output=json_output)

    result = diff_bundles(list(bundle_a.records()), list(bundle_b.records()))
    _redact_diff_result(result)
    result = _diff_turn_filter(result, turn)

    if json_output:
        emit_json(
            json_envelope(
                "diff",
                a=str(path_a),
                b=str(path_b),
                **result,
            )
        )
        raise typer.Exit(0)

    stderr_console.print(
        f"[bold]Diff[/] [cyan]{escape(str(path_a))}[/] → [cyan]{escape(str(path_b))}[/]"
    )
    stderr_console.print()
    turns = result["turns"]
    if not turns:
        warn("No aligned turns to diff.")
        return
    _print_wide(_diff_table(turns), max(stdout_console.width, 120))
    worst = result["summary"]["worst_regression"]
    if worst:
        stdout_console.print()
        stdout_console.print(
            f"[red]Worst regression[/]: turn {worst['index']} "
            f"{escape(str(worst['milestone']))} +{_format_ms(worst['delta_ms'])}ms"
        )
