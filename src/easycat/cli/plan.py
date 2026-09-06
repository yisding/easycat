"""``easycat plan`` — provider/capability plan for a manifest profile (M6b).

Loads an ``easycat.toml`` (discovery: ``--manifest`` / ``EASYCAT_MANIFEST`` /
``easycat.toml``), resolves the selected ``[voice.<profile>]`` into a
:class:`~easycat.planning.ProviderPlan` across all seven roles, and emits either
human output or the standard JSON envelope (``schema_version=1``).

The planner is side-effect-free: it reports missing env vars / missing optional
extras WITHOUT instantiating providers or importing a heavy SDK (it resolves the
manifest ``VoiceProfile`` directly, so ``resolve_agent`` never runs). Manifest
load/usage errors (``EASYCAT_E601`` / ``EASYCAT_E602``) flow through
``handle_easycat_error`` via the :func:`cli_command` wrapper.
"""

from __future__ import annotations

from typing import Any

import typer

from easycat.cli._errors import cli_command
from easycat.cli._output import emit_json, json_envelope, stdout_console


def _selection_to_dict(selection: Any) -> dict[str, Any]:
    return {
        "role": selection.role,
        "provider": selection.provider,
        "model": selection.model,
        "config_type": selection.config_type,
        "extra": selection.extra,
        "required_env": selection.required_env,
        "capabilities": sorted(selection.capabilities),
    }


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
    extras / incompatible-combo warnings — without instantiating providers.
    """
    # Manifest load errors (E601/E602) and an unresolvable selection both raise
    # EasyCatError -> handled by the cli_command wrapper. The load/plan/coded-
    # error mapping is shared with ``easycat doctor`` (and, later, the server) so
    # one manifest typo cannot report three different faces.
    from easycat.planning.selection import load_selected_profile, plan_selected_profile

    _manifest, voice_profile = load_selected_profile(manifest, profile=profile)
    provider_plan = plan_selected_profile(voice_profile, profile=profile)

    if json_output:
        emit_json(
            json_envelope(
                "plan",
                profile=provider_plan.profile,
                selected={
                    role: _selection_to_dict(selection)
                    for role, selection in provider_plan.selected.items()
                },
                missing_env=list(provider_plan.missing_env),
                missing_extras=list(provider_plan.missing_extras),
                warnings=list(provider_plan.warnings),
                blocking_errors=list(provider_plan.blocking_errors()),
                has_blocking_errors=provider_plan.has_blocking_errors,
            )
        )
        return
    _render_human(provider_plan)


__all__ = ["plan"]
