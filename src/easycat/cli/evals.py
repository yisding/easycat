"""``easycat eval`` — run conversation scenarios and inspect eval reports.

* ``easycat eval run PATH`` runs a scenario file (or every ``*.yaml`` / ``*.yml``
  / ``*.json`` under a directory) against an agent and prints a summary.
* ``easycat eval run PATH --json`` emits the eval report envelope
  (``schema_version=1``).
* ``easycat eval report FILE`` re-renders a persisted eval report; ``--json``
  re-emits the report envelope (mirrors ``easycat validate report``).

* ``easycat eval promote PATH TURN_ID --out tests/test_regressions.py`` is the
  hardened, FORKED promotion verb: it is redact-by-default, ``--no-audio`` by
  default, and refuses to write unredacted sensitive text unless ``--allow-pii``
  is set. The legacy ``journal promote`` command is the UNSAFE ``.zip``-slice
  path (full raw NDJSON + every audio blob + verbatim reply, no redaction) and
  is retained only for back-compat.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from easycat.cli._output import (
    emit_command_error,
    emit_json,
    json_envelope,
    stdout_console,
)
from easycat.evals.promote import PromotionError, promote_turn_to_test
from easycat.evals.runner import EvalRunner, ScenarioResult
from easycat.evals.scenario import load_scenario

eval_app = typer.Typer(
    name="eval",
    help="Run conversation eval scenarios and inspect eval reports.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_SCENARIO_SUFFIXES = (".yaml", ".yml", ".json")
_EVAL_REPORT_KIND = "eval_run"


def _print_literal(line: str) -> None:
    from rich.markup import escape

    stdout_console.print(escape(line))


def _load_agent(spec: str | None) -> Any:
    """Resolve an agent from a ``module:attr`` spec, or the echo NoopAgent.

    The default agent echoes input, which keeps ``eval run`` deterministic and
    dependency-free in CI; point ``--agent`` at a real agent factory/object for
    a meaningful run.
    """
    if not spec:
        from easycat.stubs import NoopAgent

        return NoopAgent()
    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        raise ValueError(f"--agent must be 'module:attr'; got {spec!r}")
    module = importlib.import_module(module_name)
    target = getattr(module, attr)
    return target() if callable(target) else target


def _discover_scenarios(path: Path) -> list[Path]:
    if path.is_dir():
        files = sorted(
            p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in _SCENARIO_SUFFIXES
        )
        return files
    return [path]


def _eval_report_payload(
    scenarios: list[ScenarioResult],
    *,
    source: Path,
) -> dict[str, Any]:
    passed = all(result.passed for result in scenarios)
    return {
        "schema_version": 1,
        "kind": _EVAL_REPORT_KIND,
        "source": str(source),
        "status": "pass" if passed else "fail",
        "scenarios": [result.to_dict() for result in scenarios],
        "scenario_count": len(scenarios),
        "passed_count": sum(1 for result in scenarios if result.passed),
    }


def _run_scenarios(paths: list[Path], agent: Any) -> list[ScenarioResult]:
    async def _run() -> list[ScenarioResult]:
        runner = EvalRunner(agent)
        results: list[ScenarioResult] = []
        for scenario_path in paths:
            scenario = load_scenario(scenario_path)
            results.append(await runner.run(scenario))
        return results

    return asyncio.run(_run())


@eval_app.command(name="run")
def run_command(
    path: Annotated[
        Path,
        typer.Argument(help="Scenario file or directory of scenario files."),
    ],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the eval report envelope (schema_version=1)."),
    ] = False,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Agent as 'module:attr'; defaults to the echo agent."),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Write the eval report JSON to this path."),
    ] = None,
) -> None:
    """Run scenario file(s) against an agent and report pass/fail."""
    scenario_paths = _discover_scenarios(path)
    if not scenario_paths:
        _command_error(
            "eval run",
            f"no scenario files found under {path}",
            json_output=json_output,
            source=str(path),
        )

    try:
        resolved_agent = _load_agent(agent)
        results = _run_scenarios(scenario_paths, resolved_agent)
    except (ValueError, RuntimeError, ImportError, AttributeError, OSError) as exc:
        _command_error("eval run", str(exc), json_output=json_output, source=str(path))

    report = _eval_report_payload(results, source=path)
    if out is not None:
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    status = report["status"]
    exit_code = 0 if status == "pass" else 1

    if json_output:
        emit_json(
            json_envelope(
                "eval run",
                status="ok" if status == "pass" else "error",
                source=str(path),
                exit_code=exit_code,
                report=report,
            )
        )
        raise typer.Exit(exit_code)

    _render_report(report)
    raise typer.Exit(exit_code)


@eval_app.command(name="report")
def report_command(
    path: Annotated[
        Path,
        typer.Argument(help="Eval report JSON path, e.g. .easycat/evals/latest.json."),
    ],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the eval report envelope (schema_version=1)."),
    ] = False,
) -> None:
    """Render a concise summary of a persisted eval report."""
    payload = _load_report_payload(path, json_output=json_output)
    status = str(payload.get("status", "unknown"))
    exit_code = 0 if status == "pass" else 1

    if json_output:
        emit_json(
            json_envelope(
                "eval report",
                status="ok" if status == "pass" else "error",
                report_path=str(path),
                exit_code=exit_code,
                report=payload,
            )
        )
        raise typer.Exit(exit_code)

    _render_report(payload)
    raise typer.Exit(exit_code)


@eval_app.command(name="promote")
def promote_command(
    path: Annotated[
        Path,
        typer.Argument(help="ZIP bundle (.zip/.bundle) or .sqlite journal to promote from."),
    ],
    turn_id: Annotated[
        str,
        typer.Argument(help="Turn id to promote into a regression test."),
    ],
    out: Annotated[
        Path,
        typer.Option("--out", "-o", help="Destination .py regression test path."),
    ],
    include_audio: Annotated[
        bool,
        typer.Option(
            "--include-audio/--no-audio",
            help="Copy artifact blobs into the slice. Off by default.",
        ),
    ] = False,
    allow_pii: Annotated[
        bool,
        typer.Option(
            "--allow-pii",
            help="Disable the unredacted-sensitive-text tripwire. Off by default.",
        ),
    ] = False,
    mode: Annotated[
        str,
        typer.Option("--mode", help="record-assertion (default) or artifact-replay."),
    ] = "record-assertion",
    assert_on: Annotated[
        str,
        typer.Option(
            "--assert-on",
            help="Reply assertion: hash (default, redaction-safe), regex, or exact (opt-in).",
        ),
    ] = "hash",
    name: Annotated[
        str | None,
        typer.Option("--name", help="Override the generated test function name."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the promotion envelope (schema_version=1)."),
    ] = False,
) -> None:
    """Promote one recorded turn into a hardened, redact-by-default pytest test.

    This is the FORKED, hardened replacement for ``journal promote``: every
    record is routed through ``redact_value`` before serialization, audio is
    excluded unless ``--include-audio``, and the committed slice is rejected if
    it still carries unredacted sensitive text unless ``--allow-pii`` is set.
    """
    try:
        written = promote_turn_to_test(
            path,
            turn_id,
            out=out,
            name=name,
            include_audio=include_audio,
            allow_pii=allow_pii,
            mode=mode,
            assert_on=assert_on,
        )
    except PromotionError as exc:
        _command_error(
            "eval promote",
            str(exc),
            json_output=json_output,
            path=str(path),
            turn_id=turn_id,
            out=str(out),
        )
    except (ValueError, OSError) as exc:
        _command_error(
            "eval promote",
            str(exc),
            json_output=json_output,
            path=str(path),
            turn_id=turn_id,
            out=str(out),
        )

    bundle_out = written.with_suffix(".bundle")
    if json_output:
        emit_json(
            json_envelope(
                "eval promote",
                path=str(path),
                turn_id=turn_id,
                out=str(written),
                bundle=str(bundle_out),
                mode=mode,
                assert_on=assert_on,
                include_audio=include_audio,
                redacted=not allow_pii,
            )
        )
        raise typer.Exit(0)

    _print_literal(f"Promoted turn {turn_id} to {written} (slice: {bundle_out})")
    _print_literal(written.read_text(encoding="utf-8"))
    raise typer.Exit(0)


def _render_report(payload: dict[str, Any]) -> None:
    _print_literal(
        f"eval {payload.get('source', '')}: {payload.get('status', 'unknown')} "
        f"({payload.get('passed_count', 0)}/{payload.get('scenario_count', 0)} scenarios)"
    )
    for scenario in payload.get("scenarios", []):
        if not isinstance(scenario, dict):
            continue
        verdict = "pass" if scenario.get("passed") else "fail"
        _print_literal(f"- {scenario.get('name', 'unknown')}: {verdict}")
        for error in scenario.get("turns", []):
            if isinstance(error, dict) and not error.get("passed"):
                for message in error.get("assertion_errors", []):
                    _print_literal(f"    {message}")


def _command_error(
    command: str,
    message: str,
    *,
    json_output: bool,
    **extra: Any,
) -> NoReturn:
    emit_command_error(
        command,
        message,
        json_output=json_output,
        human_console=stdout_console,
        **extra,
    )
    raise typer.Exit(2)


def _load_report_payload(path: Path, *, json_output: bool) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        _command_error(
            "eval report",
            f"eval report not found: {path} ({exc})",
            json_output=json_output,
            report_path=str(path),
        )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        _command_error(
            "eval report",
            f"invalid eval report JSON: {path} ({exc})",
            json_output=json_output,
            report_path=str(path),
        )

    if not isinstance(payload, dict):
        _command_error(
            "eval report",
            "invalid eval report JSON: expected object",
            json_output=json_output,
            report_path=str(path),
        )
    if payload.get("schema_version") != 1:
        _command_error(
            "eval report",
            f"unsupported eval report schema_version: {payload.get('schema_version')}",
            json_output=json_output,
            report_path=str(path),
        )
    if payload.get("kind") != _EVAL_REPORT_KIND:
        _command_error(
            "eval report",
            f"unknown eval report kind: {payload.get('kind')}",
            json_output=json_output,
            report_path=str(path),
        )
    return payload
