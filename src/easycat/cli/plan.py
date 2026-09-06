"""``easycat plan`` — provider/capability plan for a manifest profile (M6b).

Loads an ``easycat.toml`` (discovery: ``--manifest`` / ``EASYCAT_MANIFEST`` /
``easycat.toml``), resolves the selected ``[voice.<profile>]`` into a
:class:`~easycat.planning.ProviderPlan` across all seven roles, and emits either
human output or the standard JSON envelope (``schema_version=1``).

The planner is side-effect-free: it reports missing env vars / missing optional
extras / selected backends whose SDK is absent WITHOUT instantiating providers
or importing a heavy SDK (it resolves the manifest ``VoiceProfile`` directly, so
``resolve_agent`` never runs). Manifest load/usage errors (``EASYCAT_E601`` /
``EASYCAT_E602``) flow through ``handle_easycat_error`` via the
:func:`cli_command` wrapper.
"""

from __future__ import annotations

from typing import Any

import typer

from easycat.cli._errors import cli_command
from easycat.cli._output import emit_json, json_envelope, stdout_console


def _render_human(plan: Any) -> None:
    stdout_console.print(f"[bold]Provider plan[/] (profile: {plan.profile})")
    for role, selection in plan.selected.items():
        extra = f" extra={selection.extra}" if selection.extra else ""
        env = f" env={selection.required_env}" if selection.required_env else ""
        model = f"/{selection.model}" if selection.model else ""
        stdout_console.print(
            f"  [cyan]{role:14}[/] {selection.provider}{model}"
            f" ([dim]{selection.config_type}[/]){extra}{env}"
        )
    if plan.missing_env:
        stdout_console.print(f"  [red]missing env:[/] {', '.join(plan.missing_env)}")
    if plan.missing_extras:
        stdout_console.print(f"  [red]missing extras:[/] {', '.join(plan.missing_extras)}")
    if plan.missing_backends:
        # A selected backend the session cannot construct even though it needs no
        # pip extra (a commercial SDK ships no PyPI package). Blocking, so it is
        # printed in red next to its missing-extra sibling rather than as a
        # warning.
        stdout_console.print(f"  [red]missing backends:[/] {', '.join(plan.missing_backends)}")
    if plan.warnings:
        stdout_console.print(f"  [yellow]warnings:[/] {', '.join(plan.warnings)}")
    status = "blocked" if plan.has_blocking_errors else "ready"
    stdout_console.print(f"  [bold]status:[/] {status}")


@cli_command
def plan(
    manifest: str | None = typer.Option(
        None,
        "--manifest",
        help="Path to easycat.toml (defaults to EASYCAT_MANIFEST or ./easycat.toml).",
    ),
    profile: str = typer.Option(
        "default",
        "--profile",
        help="Voice profile table to plan (for example, voice.default).",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the standard JSON envelope (schema_version=1).",
    ),
) -> None:
    """Resolve the provider/capability plan for a manifest profile.

    Reports the selected provider per role plus any missing env vars / missing
    extras / missing backends / incompatible-combo warnings — without
    instantiating providers.

    Source documentation only: ``easycat plan --help`` prints the one-line
    ``_COMMAND_TEXT["plan"].help`` string that ``cli/_app.py`` registers the
    command with, not this docstring. The operator-facing description of the gap
    tuples lives in ``docs/cli.md``; the human renderer's own line is pinned by
    ``tests/cli/test_plan.py::test_plan_human_output_prints_missing_backends``.
    """
    from easycat.errors import EASYCAT_E602
    from easycat.planning import build_provider_plan, plan_to_dict
    from easycat.project import load_manifest

    # Manifest load errors (E601/E602) raise EasyCatError -> handled by the
    # cli_command wrapper. An unknown profile raises E602 too.
    project_manifest = load_manifest(manifest)
    voice_profile = project_manifest.profile(profile)
    try:
        provider_plan = build_provider_plan(voice_profile, profile=profile)
    except (ValueError, KeyError) as exc:
        # The planner RAISES a bare ValueError on an unknown provider/backend
        # shortcut (e.g. ``vad = "silro"``) to keep planner-vs-create_session
        # parity. A KeyError can surface from a registry lookup for a profile
        # selecting a not-fully-wired provider. Surface either as the coded
        # manifest error so the CLI prints a clean diagnosis instead of a raw
        # traceback (the same shape the readiness probe degrades to).
        raise EASYCAT_E602(path=f"[voice.{profile}]", problem=str(exc)) from exc

    if json_output:
        # ``plan_to_dict`` is the one plan -> JSON projection; the server's
        # ``/plan`` payload wraps the same dict, so a plan-level field cannot land
        # on one surface and be forgotten on the other.
        emit_json(json_envelope("plan", **plan_to_dict(provider_plan)))
        return
    _render_human(provider_plan)


__all__ = ["plan"]
