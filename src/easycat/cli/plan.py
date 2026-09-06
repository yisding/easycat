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

from typing import TYPE_CHECKING, Any

import typer
from rich.markup import escape

from easycat.cli._errors import cli_command
from easycat.cli._output import emit_json, json_envelope, stdout_console

if TYPE_CHECKING:
    from collections.abc import Sequence

    from easycat.errors import SetupIssue


def _render_human(plan: Any, issues: Sequence[SetupIssue]) -> None:
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
    # One coded row per blocking issue, in doctor's layout: the same code,
    # field, role, and fix ``easycat doctor --manifest`` prints and ``/plan``
    # returns, so one cause reads the same on every surface.
    for issue in issues:
        if issue.severity != "blocking":
            continue
        role = issue.role or "-"
        # ``escape`` on EVERY interpolation, like doctor's renderer: a field is
        # ``[voice.<profile>]`` and a fix quotes ``uv add 'easycat[webrtc]'``,
        # both of which Rich would otherwise eat as style tags — printing a
        # copy-pasteable command that installs the wrong thing, or raising
        # ``MarkupError`` on a lone ``[/]``-shaped substring.
        stdout_console.print(
            f"  [red]{escape(issue.code)}[/] {escape(issue.field)} "
            f"({escape(role)}): {escape(issue.detail)}"
        )
        if issue.fix:
            stdout_console.print(f"    Fix: {escape(issue.fix)}")


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
    # error mapping is shared with ``easycat doctor`` and ``VoiceServer`` so one
    # manifest typo cannot report three different faces.
    from easycat.planning.selection import (
        build_manifest_plan,
        load_selected_profile,
        plan_body,
        plan_issues,
    )

    project_manifest, voice_profile = load_selected_profile(manifest, profile=profile)
    # ``build_manifest_plan`` (not ``plan_selected_profile``) so the manifest's
    # own defects — a phone profile with no token, an unset ``bearer-env:``
    # reference — reach ``easycat plan`` too, with the scoped severity that
    # keeps a ``[server] auth`` gap reportable but not blocking.
    provider_plan = build_manifest_plan(
        project_manifest, profile=profile, voice_profile=voice_profile
    )
    issues = plan_issues(provider_plan)

    if json_output:
        emit_json(
            json_envelope(
                "plan",
                **plan_body(provider_plan),
                issues=[issue.as_dict() for issue in issues],
            )
        )
        return
    _render_human(provider_plan, issues)


__all__ = ["plan"]
