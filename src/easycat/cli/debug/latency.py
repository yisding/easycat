"""``easycat latency`` — critical-path latency percentiles for a bundle."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.markup import escape
from rich.table import Table

from easycat.cli._errors import cli_command
from easycat.cli._output import (
    emit_json,
    json_envelope,
    stderr_console,
    stdout_console,
    warn,
)
from easycat.cli.debug._common import _format_ms, _load_bundle_or_journal, _print_wide
from easycat.debug._turn_timeline import turn_waterfall
from easycat.validation.latency import LatencyPercentileStats

# The five critical-path milestone deltas, in pipeline order, paired with
# the compact column labels the per-turn table renders.  These mirror the
# debugger critical-path panel (debugger/static/index.html CP_SEGMENTS) and
# the ``turn_waterfall`` milestone keys, so the CLI and SPA stay in lockstep.
_LATENCY_MILESTONES: tuple[tuple[str, str], ...] = (
    ("vad->stt", "vad_endpoint_to_stt_final_ms"),
    ("stt->req", "stt_final_to_agent_request_ms"),
    ("req->token", "agent_request_to_first_token_ms"),
    ("token->tts", "agent_first_token_to_tts_first_byte_ms"),
    ("vad->tts", "vad_endpoint_to_tts_first_byte_ms"),
)


def _latency_percentiles(turns: list[dict[str, Any]]) -> dict[str, LatencyPercentileStats]:
    """Per-milestone p50/p90/p95/p99 across all turns.

    Reuses ``validation.latency.LatencyPercentileStats.from_values`` (which
    drops ``None`` deltas) so the CLI never reimplements percentile math.
    """
    stats: dict[str, LatencyPercentileStats] = {}
    for label, key in _LATENCY_MILESTONES:
        values = [turn.get("milestones", {}).get(key) for turn in turns]
        stats[label] = LatencyPercentileStats.from_values(values)
    return stats


def _latency_turn_table(turns: list[dict[str, Any]]) -> Table:
    """One row per turn: the five critical-path milestone deltas in ms."""
    table = Table(
        title="Per-turn critical path (ms) — see docs/latency.md",
        show_header=True,
        header_style="bold",
        box=None,
        padding=(0, 1),
        title_justify="left",
    )
    table.add_column("turn", no_wrap=True, overflow="fold")
    for label, _key in _LATENCY_MILESTONES:
        table.add_column(label, justify="right", no_wrap=True)
    for turn in turns:
        milestones = turn.get("milestones") or {}
        table.add_row(
            escape(str(turn.get("turn_id", ""))),
            *[_format_ms(milestones.get(key)) for _label, key in _LATENCY_MILESTONES],
        )
    return table


def _latency_percentile_table(stats: dict[str, LatencyPercentileStats]) -> Table:
    """Percentile summary: one row per milestone, count + p50/p90/p95/p99."""
    table = Table(
        title="Critical-path percentiles (ms) — see docs/latency.md",
        show_header=True,
        header_style="bold",
        box=None,
        padding=(0, 1),
        title_justify="left",
    )
    table.add_column("milestone", no_wrap=True)
    table.add_column("count", justify="right", no_wrap=True)
    table.add_column("p50", justify="right", no_wrap=True)
    table.add_column("p90", justify="right", no_wrap=True)
    table.add_column("p95", justify="right", no_wrap=True)
    table.add_column("p99", justify="right", no_wrap=True)
    for label, _key in _LATENCY_MILESTONES:
        stat = stats[label]
        table.add_row(
            label,
            str(stat.count),
            _format_ms(stat.p50),
            _format_ms(stat.p90),
            _format_ms(stat.p95),
            _format_ms(stat.p99),
        )
    return table


@cli_command
def latency_command(
    bundle_path: Path = typer.Argument(
        ...,
        help=(
            "Path to a ZIP bundle archive (``.zip``, ``.bundle``, or "
            "``.easycat-bundle``) or a ``.sqlite`` journal."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Summarise critical-path latency percentiles across a bundle's turns.

    Rolls up the VAD endpoint → STT final → agent request → first token →
    TTS first byte milestone deltas per turn and reports p50/p90/p95/p99
    for each, without opening the debugger UI.  See docs/latency.md.
    """
    bundle = _load_bundle_or_journal(bundle_path, command="latency", json_output=json_output)
    turns = turn_waterfall(list(bundle.records()))
    percentiles = _latency_percentiles(turns)

    if json_output:
        emit_json(
            json_envelope(
                "latency",
                path=str(bundle_path),
                turns=turns,
                percentiles={label: stat.to_dict() for label, stat in percentiles.items()},
            )
        )
        raise typer.Exit(0)

    stderr_console.print(f"[bold]Latency[/] [cyan]{escape(str(bundle_path))}[/]")
    stderr_console.print()
    if not turns:
        warn("No turn-scoped records found; nothing to summarise.")
        return
    width = max(stdout_console.width, 120)
    _print_wide(_latency_turn_table(turns), width)
    stdout_console.print()
    _print_wide(_latency_percentile_table(percentiles), width)
