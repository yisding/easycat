"""What ``easycat doctor`` knows about the application the user selected.

``doctor`` can be scoped three ways: to the machine only, to a generated
project's ``[tool.easycat.scaffold]`` table, or — with ``--manifest`` /
``--profile`` — to the very ``[voice.<name>]`` profile ``easycat plan``
resolves. :class:`SelectedApp` is the one object that carries all three, so the
check functions stop threading a ``ScaffoldRequirements | None`` (and its six
``... if scaffold is not None else ()`` repetitions) through every signature.

The derivation here is PURE: it reads an already-loaded manifest and an explicit
environment snapshot, and never imports or runs the profile's application.
``easycat.planning`` / ``easycat.project`` are imported LAZILY inside the
functions so ``easycat doctor --help`` and every non-manifest run keep today's
import weight.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from easycat.errors import EASYCAT_E202, SetupIssue

if TYPE_CHECKING:
    from collections.abc import Mapping

    from easycat.cli.diagnose.doctor import ScaffoldRequirements
    from easycat.project.manifest import ProjectManifest
    from easycat.project.schema import VoiceProfile

DependencySource = Literal["pypi", "git", "path", "none"]

_EASYCAT_REQUIREMENT_RE = re.compile(r"^easycat(?![A-Za-z0-9_.-])", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class RoleRequirement:
    """One pipeline role of the selected app and what it needs."""

    role: str
    provider: str
    required_env: str | None = None
    extra: str | None = None
    extra_missing: bool = False
    degrades_without_extra: bool = False
    capabilities: frozenset[str] = field(default_factory=frozenset)

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "provider": self.provider,
            "required_env": self.required_env,
            "extra": self.extra,
            "capabilities": sorted(self.capabilities),
        }


@dataclass(frozen=True, slots=True)
class SelectedApp:
    """Everything doctor knows about the application the user selected.

    Built from a scaffold table, a manifest profile, or both.
    """

    source: Literal["scaffold", "manifest", "scaffold+manifest"]
    required_env: tuple[str, ...] = ()
    optional_env: tuple[str, ...] = ()
    roles: tuple[RoleRequirement, ...] = ()
    issues: tuple[SetupIssue, ...] = ()
    warnings: tuple[str, ...] = ()
    reference_vars: frozenset[str] = field(default_factory=frozenset)
    template: str | None = None
    profile: str | None = None
    manifest_path: str | None = None

    @property
    def requires_microphone(self) -> bool:
        """Whether any selected role declares the ``"microphone"`` capability.

        ``TRANSPORT_BACKENDS["local"]`` declares it and ``webrtc`` /
        ``websocket`` / ``twilio`` / ``telnyx`` do not, so a browser profile
        stops asking for a microphone with no new table. Always ``True`` for a
        scaffold-only :class:`SelectedApp`, preserving today's behavior when no
        profile was selected.
        """
        if not self.roles:
            return True
        return any("microphone" in role.capabilities for role in self.roles)

    @property
    def project_root(self) -> Path:
        """Directory whose ``pyproject.toml`` governs this app's install fix.

        The manifest's own directory when a manifest was selected — ``doctor
        --manifest /elsewhere/easycat.toml`` must classify THAT project's
        dependency source, not the one that happens to sit in the process's
        working directory. Falls back to the working directory for a
        scaffold-only selection, which is where the ``[tool.easycat.scaffold]``
        table was read from.
        """
        if self.manifest_path is not None:
            return Path(self.manifest_path).parent
        return Path()

    def role_for_env(self, var: str) -> str:
        """Role that needs *var*, or ``""`` for a scaffold-table-only var."""
        for role in self.roles:
            if role.required_env == var:
                return role.role
        return ""

    def code_for_env(self, var: str) -> str:
        """``"EASYCAT_E604"`` for a manifest-bound reference var, else E210."""
        return "EASYCAT_E604" if var in self.reference_vars else "EASYCAT_E210"

    def issue_for_env(self, var: str) -> SetupIssue | None:
        """The planner issue backing an env-var row, if the planner made one.

        An env var can be blocked for two reasons the planner already coded: a
        selected role needs a credential (``missing_env`` -> ``EASYCAT_E203``)
        or the manifest binds a ``bearer-env:`` reference that is unset
        (``unset_reference`` -> ``EASYCAT_E604``).
        """
        return self.issue_for("missing_env", var) or self.issue_for("unset_reference", var)

    def issue_for(self, reason: str, field_name: str) -> SetupIssue | None:
        """Look up the planner issue backing a check row, if any."""
        for issue in self.issues:
            if issue.reason == reason and issue.field == field_name:
                return issue
        return None

    def as_dict(self) -> dict[str, Any]:
        """The ``selection`` object in doctor's JSON envelope."""
        return {
            "source": self.source,
            "profile": self.profile,
            "manifest_path": self.manifest_path,
            "template": self.template,
            "roles": [role.as_dict() for role in self.roles],
            "required_env": list(self.required_env),
            "optional_env": list(self.optional_env),
        }


def selected_app_from_scaffold(scaffold: ScaffoldRequirements) -> SelectedApp:
    """Wrap today's ``pyproject.toml`` table result."""
    return SelectedApp(
        source="scaffold",
        required_env=scaffold.required_env,
        optional_env=scaffold.optional_env,
        template=scaffold.template,
    )


def reference_var_names(manifest: ProjectManifest, *, profile: str) -> frozenset[str]:
    """Env-var NAMES this manifest binds for *profile* (pure, no environment).

    Used to widen ``doctor``'s ``--env-file`` allow-list. Takes an ALREADY-LOADED
    manifest so ``doctor()`` reads the file exactly once.
    """
    return frozenset(requirement.var for requirement in manifest.profile_requirements(profile))


def selected_app_from_manifest(
    manifest: ProjectManifest,
    voice_profile: VoiceProfile,
    profile: str,
    *,
    environ: Mapping[str, str],
    scaffold: ScaffoldRequirements | None = None,
) -> SelectedApp:
    """Project an already-loaded manifest profile for doctor.

    Takes the ``(ProjectManifest, VoiceProfile)`` pair ``doctor()`` already got
    from :func:`easycat.planning.selection.load_selected_profile` (so the file is
    read once, before ``--env-file`` parsing) and calls ``build_manifest_plan``
    with the caller's ``environ`` snapshot. ``EASYCAT_E602`` / ``E104`` raised by
    the planner propagate as ``EasyCatError`` so ``cli_command`` renders them
    exactly as ``easycat plan`` does — same code, same message, same exit code.
    """
    from easycat.planning.selection import (
        build_manifest_plan,
        degraded_extra_roles,
        plan_issues,
    )

    plan = build_manifest_plan(
        manifest,
        profile=profile,
        environ=environ,
        voice_profile=voice_profile,
    )
    issues = plan_issues(plan)
    degraded = set(degraded_extra_roles(plan))
    missing_extras = set(plan.missing_extras)

    roles: list[RoleRequirement] = []
    for role_name, selection in plan.selected.items():
        extra = selection.extra
        extra_missing = bool(extra) and (extra in missing_extras or (role_name, extra) in degraded)
        roles.append(
            RoleRequirement(
                role=role_name,
                provider=selection.provider,
                required_env=selection.required_env,
                extra=extra,
                extra_missing=extra_missing,
                degrades_without_extra="degrades_to_passthrough" in selection.capabilities,
                capabilities=selection.capabilities,
            )
        )

    required: list[str] = [
        selection.required_env for selection in plan.selected.values() if selection.required_env
    ]
    reference_vars = set()
    for requirement in manifest.profile_requirements(profile):
        required.append(requirement.var)
        reference_vars.add(requirement.var)
    if scaffold is not None:
        required.extend(scaffold.required_env)
    required_env = tuple(dict.fromkeys(required))

    scaffold_optional = scaffold.optional_env if scaffold is not None else ()
    optional_env = tuple(var for var in scaffold_optional if var not in set(required_env))

    return SelectedApp(
        source="scaffold+manifest" if scaffold is not None else "manifest",
        required_env=required_env,
        optional_env=optional_env,
        roles=tuple(roles),
        issues=issues,
        warnings=plan.warnings,
        reference_vars=frozenset(reference_vars),
        template=scaffold.template if scaffold is not None else None,
        profile=profile,
        manifest_path=str(manifest.source_path) if manifest.source_path else None,
    )


def dependency_source(project_root: Path = Path()) -> DependencySource:
    """Classify how this project depends on easycat (pure, ``tomllib`` only).

    No ``pyproject.toml`` or no ``easycat`` dependency -> ``"none"``; a
    ``[tool.uv.sources] easycat = {git = ..., rev = ...}`` -> ``"git"``;
    ``{path = ..., editable = true}`` -> ``"path"``; otherwise ``"pypi"``. Both
    source shapes are written by ``easycat init``.
    """
    path = project_root / "pyproject.toml"
    if not path.is_file():
        return "none"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return "none"
    project = data.get("project")
    dependencies = project.get("dependencies", []) if isinstance(project, dict) else []
    if not isinstance(dependencies, list) or not any(
        isinstance(item, str) and _EASYCAT_REQUIREMENT_RE.match(item.strip())
        for item in dependencies
    ):
        return "none"
    tool = data.get("tool")
    sources = tool.get("uv", {}).get("sources", {}) if isinstance(tool, dict) else {}
    pin = sources.get("easycat") if isinstance(sources, dict) else None
    if isinstance(pin, dict):
        if pin.get("git"):
            return "git"
        if pin.get("path"):
            return "path"
    return "pypi"


def install_fix(extra: str, *, project_root: Path = Path()) -> str:
    """An install instruction that respects the project's easycat source.

    A PINNED source (``git`` / ``path``) is the only case that overrides the
    registry text: ``uv add 'easycat[<extra>]'`` against such a project would
    drop the ``[tool.uv.sources]`` pin. ``"none"`` — no ``pyproject.toml`` at
    all, or one with no ``easycat`` dependency, the common case for an installed
    CLI run from an arbitrary directory — keeps ``EASYCAT_E202``'s registry fix,
    which names both the published and the repo-local form.
    """
    if dependency_source(project_root) in {"git", "path"}:
        return (
            f'Add "{extra}" to the easycat[...] extras in pyproject.toml '
            "(its source is pinned in [tool.uv.sources]), then run: uv sync"
        )
    return EASYCAT_E202(extra=extra).rendered_fix() or ""


__all__ = [
    "DependencySource",
    "RoleRequirement",
    "SelectedApp",
    "dependency_source",
    "install_fix",
    "reference_var_names",
    "selected_app_from_manifest",
    "selected_app_from_scaffold",
]
