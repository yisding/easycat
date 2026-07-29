"""``easycat replay`` — replay a debug bundle or SQLite journal from the CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

import typer
from rich.markup import escape
from rich.table import Table

from easycat.cli._errors import cli_command, handle_easycat_error
from easycat.cli._output import (
    emit_command_error,
    emit_json,
    json_envelope,
    stderr_console,
    stdout_console,
)
from easycat.cli.debug._common import _load_bundle_or_journal
from easycat.debug._turn_timeline import safe_turn_id
from easycat.runtime.replay import (
    ProviderVersionMismatchError,
    ReplayDivergenceError,
    ReplayError,
    ReplayFidelity,
    ReplayResult,
    ReplaySideEffectBlocked,
    ReplaySpec,
    ToolReplayPolicy,
)


def _render_replay_summary(
    *,
    bundle_path: Path,
    result: ReplayResult,
    spec: ReplaySpec,
    json_output: bool,
) -> None:
    stages = sorted({frame.stage for frame in result.frames if frame.stage})
    summary: dict[str, Any] = {
        "path": str(bundle_path),
        "fidelity_requested": spec.fidelity.value,
        "fidelity_effective": result.fidelity_label.value,
        "frames": len(result.frames),
        "stages": stages,
        "stage_replays": len(result.stage_replays),
        "side_effecting": result.side_effecting,
        "tool_policy": spec.tool_policy.value,
        "blocked_tool_calls": result.blocked_tool_calls,
        "stubbed_tool_calls": result.stubbed_tool_calls,
        "allowed_tool_calls": result.allowed_tool_calls,
        "executed_tool_calls": result.executed_tool_calls,
        "from_sequence": spec.from_sequence,
        "to_sequence": spec.to_sequence,
        "stage_filter": spec.stage_filter or [],
        "timing": spec.timing,
        "force": spec.force,
    }

    if json_output:
        emit_json(json_envelope("replay", **summary))
        raise typer.Exit(0)

    stderr_console.print(f"[bold]Replay[/] [cyan]{escape(str(bundle_path))}[/]")
    stderr_console.print()
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column(style="bold", no_wrap=True)
    table.add_column()
    table.add_row("fidelity", str(summary["fidelity_effective"]))
    if summary["fidelity_effective"] != summary["fidelity_requested"]:
        table.add_row("requested_fidelity", str(summary["fidelity_requested"]))
    table.add_row("frames", str(summary["frames"]))
    table.add_row("stage_replays", str(summary["stage_replays"]))
    table.add_row("stages", escape(", ".join(stages)) if stages else "[dim](none)[/]")
    table.add_row("tool_policy", str(summary["tool_policy"]))
    table.add_row("side_effecting", "yes" if result.side_effecting else "no")
    if result.stubbed_tool_calls:
        table.add_row("stubbed_tools", escape(", ".join(result.stubbed_tool_calls)))
    if result.allowed_tool_calls:
        table.add_row("allowed_tools", escape(", ".join(result.allowed_tool_calls)))
    if result.executed_tool_calls:
        table.add_row("executed_tools", escape(", ".join(result.executed_tool_calls)))
    stdout_console.print(table)


@cli_command
def replay_bundle(
    bundle_path: Path = typer.Argument(
        ...,
        help=(
            "Path to a ZIP bundle archive (``.zip``, ``.bundle``, or "
            "``.easycat-bundle``) or a ``.sqlite`` journal."
        ),
    ),
    fidelity: ReplayFidelity = typer.Option(
        ReplayFidelity.ARTIFACT,
        "--fidelity",
        help="Replay fidelity: artifact, simulated, or live.",
        case_sensitive=False,
    ),
    from_sequence: int | None = typer.Option(
        None,
        "--from-sequence",
        help="Start replay at a committable journal sequence.",
    ),
    to_sequence: int | None = typer.Option(
        None,
        "--to-sequence",
        help="Stop replay after this journal sequence.",
    ),
    turn: str | None = typer.Option(
        None,
        "--turn",
        help=(
            "Restrict replay to one turn id; resolves the turn's min/max "
            "journal sequence and sets the replay window (overrides "
            "--from-sequence/--to-sequence)."
        ),
    ),
    stage: list[str] | None = typer.Option(
        None,
        "--stage",
        help="Restrict replay to one stage; may be repeated.",
    ),
    timing: str = typer.Option(
        "fast",
        "--timing",
        help="Timing mode: fast runs immediately; wall preserves recorded inter-frame delays.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Allow version-mismatch downgrades when replay context supplies installed versions.",
    ),
    tool_policy: ToolReplayPolicy = typer.Option(
        ToolReplayPolicy.DENY,
        "--tool-policy",
        help="Tool-call policy during replay: deny, stub, or allow.",
        case_sensitive=False,
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Replay a debug bundle or SQLite journal without opening a Python REPL."""
    if timing not in ("fast", "wall"):
        emit_command_error(
            "replay",
            "timing must be 'fast' or 'wall'",
            json_output=json_output,
            exit_code=2,
        )
        raise typer.Exit(2)
    timing_value = cast(Literal["fast", "wall"], timing)

    bundle = _load_bundle_or_journal(bundle_path, command="replay", json_output=json_output)
    if turn is not None:
        turn_id = safe_turn_id(turn)
        if turn_id is None:
            emit_command_error(
                "replay",
                f"Invalid turn id: {turn!r}.",
                json_output=json_output,
                exit_code=2,
                path=str(bundle_path),
            )
            raise typer.Exit(2)
        sequences = [
            r["sequence"]
            for r in bundle.filter_by_turn(turn_id)
            if isinstance(r.get("sequence"), int)
        ]
        if not sequences:
            emit_command_error(
                "replay",
                f"No journal records found for turn {turn_id!r}.",
                json_output=json_output,
                exit_code=5,
                path=str(bundle_path),
            )
            raise typer.Exit(5)
        from_sequence = min(sequences)
        to_sequence = max(sequences)
    spec = ReplaySpec(
        fidelity=fidelity,
        from_sequence=from_sequence,
        to_sequence=to_sequence,
        stage_filter=stage or None,
        timing=timing_value,
        force=force,
        tool_policy=tool_policy,
    )
    try:
        result = bundle.replay(spec)
    except ReplaySideEffectBlocked as exc:
        message = (
            f"Replay blocked: {exc}. Use --tool-policy stub to replace tool effects, "
            "or --tool-policy allow to pass recorded tool frames. The CLI does not "
            "invoke external tools."
        )
        emit_command_error(
            "replay",
            message,
            json_output=json_output,
            exit_code=6,
            path=str(bundle_path),
        )
        raise typer.Exit(6) from None
    except ProviderVersionMismatchError as exc:
        message = (
            f"Replay provider version check failed: {exc}. "
            "Use --force to continue with downgraded fidelity."
        )
        emit_command_error(
            "replay",
            message,
            json_output=json_output,
            exit_code=6,
            path=str(bundle_path),
        )
        raise typer.Exit(6) from None
    except ReplayDivergenceError as exc:
        exit_code = handle_easycat_error(exc, json_mode=json_output, command="replay")
        raise typer.Exit(exit_code) from None
    except ReplayError as exc:
        emit_command_error(
            "replay",
            f"Replay failed: {exc}",
            json_output=json_output,
            exit_code=6,
            path=str(bundle_path),
        )
        raise typer.Exit(6) from None

    _render_replay_summary(
        bundle_path=bundle_path,
        result=result,
        spec=spec,
        json_output=json_output,
    )
