"""Manifest-profile selection: the shared load -> plan -> coded-issue boundary.

The pure planner (:mod:`easycat.planning.provider_plan`) must not touch the
filesystem or raise coded CLI errors. This module is the thin boundary that does
both, so ``easycat plan``, ``easycat doctor``, and ``VoiceServer`` map an
unresolvable profile to the SAME error instead of three private strategies.

Import weight: ``easycat.project`` and ``easycat.validation.redaction`` are
imported lazily inside the functions, so importing this module costs nothing
beyond ``easycat.errors`` (a stdlib-only leaf) and ``easycat.planning``.
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

from easycat.errors import (
    EASYCAT_E202,
    EASYCAT_E203,
    EASYCAT_E602,
    EASYCAT_E604,
    EasyCatError,
    SetupIssue,
)
from easycat.planning.provider_plan import (
    _ROLE_ORDER,
    ProviderPlan,
    ProviderSelection,
    build_provider_plan,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from easycat.project.manifest import ProjectManifest
    from easycat.project.schema import VoiceProfile

# The degraded-extra warning token shape produced by ``build_provider_plan``:
# ``f"{role}_extra_{extra}_missing_degraded"``.
_DEGRADED_SUFFIX = "_missing_degraded"
_DEGRADED_INFIX = "_extra_"


def load_selected_profile(
    manifest: str | Path | None,
    *,
    profile: str,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> tuple[ProjectManifest, VoiceProfile]:
    """Discover + load the manifest and select ``profile``.

    Discovery order is ``easycat.project.loader.discover_manifest_path``'s:
    explicit path, then ``EASYCAT_MANIFEST``, then ``./easycat.toml``. RAISES
    ``EASYCAT_E601`` (not found) or ``EASYCAT_E602`` (invalid manifest / unknown
    profile) exactly as ``easycat plan`` does today; the unknown-profile message
    keeps ``ProjectManifest.profile``'s available-profile list. Reads no provider
    env var, resolves no agent reference, resolves no secret.
    """
    from easycat.project import load_manifest

    project_manifest = load_manifest(
        manifest,
        environ=dict(environ) if environ is not None else None,
        cwd=cwd,
    )
    return project_manifest, project_manifest.profile(profile)


def selection_error(exc: BaseException, *, profile: str, path: str | None = None) -> EasyCatError:
    """Map an unresolvable-selection exception to its coded EasyCat error.

    * an :class:`~easycat.errors.EasyCatError` (e.g. ``EASYCAT_E104`` from
      ``ProviderCatalog.validate_name``) is returned UNCHANGED — the same
      object, because its code is already right;
    * a ``ValueError`` / ``KeyError`` (the planner's own unknown-backend raise)
      becomes ``EASYCAT_E602(path=path or f"[voice.{profile}]", problem=...)``
      with a REDACTED problem string.

    ``str(exc)`` is routed through ``easycat.validation.redaction.redact_value``
    (imported lazily) so a secret-shaped manifest value inside the exception text
    cannot reach a terminal or an HTTP body.

    NOTE the asymmetry this creates, and why :func:`selection_issue` exists: the
    ``EasyCatError`` branch returns the error UNCHANGED and therefore UNREDACTED
    (``EASYCAT_E104``'s headline interpolates the raw manifest value). An
    ``EasyCatError`` returned here must never be projected into an HTTP body or a
    JSON field without passing through :func:`selection_issue`.
    """
    if isinstance(exc, EasyCatError):
        return exc
    from easycat.validation.redaction import redact_value

    problem: str = redact_value(str(exc))
    return EASYCAT_E602(path=path or f"[voice.{profile}]", problem=problem)


def selection_issue(exc: BaseException, *, profile: str, path: str | None = None) -> SetupIssue:
    """The REDACTED coded projection of an unresolvable-selection exception.

    This is the ONLY constructor any surface may use for an issue derived from a
    caught exception. It closes the leak :meth:`SetupIssue.from_error` cannot:
    :func:`selection_error` passes an ``EasyCatError`` through unchanged, and
    ``EASYCAT_E104(provider=<raw manifest value>)`` would otherwise put that raw
    value in a JSON ``issues[].detail``.
    """
    from easycat.validation.redaction import redact_value

    err = selection_error(exc, profile=profile, path=path)
    detail: str = redact_value(err.message)
    fix: str = redact_value(err.rendered_fix() or "")
    return SetupIssue(
        code=err.code,
        reason="unresolvable_profile",
        field=path or f"[voice.{profile}]",
        detail=detail,
        fix=fix,
        severity="blocking",
    )


def _redacted(issue: SetupIssue) -> SetupIssue:
    """Return *issue* with ``detail``/``fix`` passed through ``redact_value``.

    :meth:`SetupIssue.from_error` and :meth:`SetupIssue.from_code` cannot redact
    (``errors.py`` is a stdlib-only leaf), yet every issue built here reaches an
    HTTP body through ``VoiceServer.plan_payload``. A defect's ``detail``
    interpolates the manifest's own text, so a future defect rule that quotes a
    manifest VALUE must not be one edit away from putting a secret on ``/plan``.

    Apply it ONLY where a manifest value can actually reach the string. The
    redactor is a pattern scrubber, not a formatter: registry catalog text that
    contains a ``NAME=...`` placeholder is rewritten into a falsely-redacted,
    non-copy-pasteable instruction whenever ``NAME`` looks credential-ish. So an
    issue whose ``detail``/``fix`` is pure catalog text over provably non-secret
    inputs must be appended WITHOUT this wrapper.
    """
    from easycat.validation.redaction import redact_value

    return replace(issue, detail=redact_value(issue.detail), fix=redact_value(issue.fix))


def plan_selected_profile(
    voice_profile: VoiceProfile,
    *,
    profile: str,
    environ: Mapping[str, str] | None = None,
) -> ProviderPlan:
    """``build_provider_plan`` with its bare raises mapped through
    :func:`selection_error`.
    """
    try:
        return build_provider_plan(voice_profile, environ=environ, profile=profile)
    except (ValueError, KeyError) as exc:
        coded = selection_error(exc, profile=profile)
        if coded is exc:
            raise
        raise coded from exc


def build_manifest_plan(
    manifest: ProjectManifest,
    *,
    profile: str = "default",
    environ: Mapping[str, str] | None = None,
    voice_profile: VoiceProfile | None = None,
) -> ProviderPlan:
    """Resolve *profile* from a manifest, INCLUDING manifest-only requirements.

    The one function that merges the two static sources for a manifest-selected
    app: the per-role provider plan (:func:`plan_selected_profile`) and the
    manifest's own env references and selection defects
    (``ProjectManifest.profile_requirements`` / ``.profile_defects``).

    Severity is scoped, and this is not cosmetic:

    * a ``profile_defects`` entry (a phone profile with no ``token``) is
      ``blocking`` — ``to_easyconfig`` raises on EVERY connection;
    * an unset ``[voice.<name>] token`` reference is ``blocking`` —
      ``EnvReference.resolve`` raises ``EASYCAT_E604`` per connection;
    * an unset ``[server] auth`` reference is a ``warning``: it is a
      SERVER-scope requirement, not a profile-selection defect, and
      ``VoiceServer.from_manifest`` already raises ``EASYCAT_E604`` before the
      server exists, so a server with an unset auth var never reaches
      ``/health/ready`` at all.

    Side-effect-free: never resolves the agent reference, never constructs a
    provider, never reads a referenced secret's value.

    Redaction is applied to the MANIFEST-DERIVED issue only. A
    ``profile_defects`` entry interpolates the manifest's own text (its
    ``source_path``, and a future rule could quote a value), so it is built
    through :func:`_redacted`. The ``unset_reference`` issue is not: it is built
    from ``requirement.var`` / ``requirement.reference``, which
    ``parse_auth_reference`` (``project/schema.py``) has already proved cannot
    carry a secret — the reference must be ``bearer-env:`` plus a well-formed
    env-var NAME and must not match the shared secret detector. Its
    ``detail``/``fix`` are therefore pure ``EASYCAT_E604`` catalog text, and
    passing catalog text through the redactor CORRUPTS it: the fix's
    ``export {var}=...`` placeholder matches the redactor's key/value rule for
    any var named ``*TOKEN*``/``*SECRET*``/``*KEY*``, which would both mangle
    the copy-pasteable command and make ``easycat plan`` disagree with
    ``easycat doctor`` about the identical ``EASYCAT_E604`` cause.
    """
    env = dict(environ) if environ is not None else dict(os.environ)
    selected_profile = voice_profile if voice_profile is not None else manifest.profile(profile)
    plan = plan_selected_profile(selected_profile, profile=profile, environ=env)

    defects: list[SetupIssue] = [
        _redacted(
            SetupIssue.from_error(
                defect,
                reason="incomplete_selection",
                field=f"[voice.{profile}]",
                severity="blocking",
            )
        )
        for defect in manifest.profile_defects(profile)
    ]
    for requirement in manifest.profile_requirements(profile):
        if env.get(requirement.var):
            continue
        severity: Literal["blocking", "warning"] = (
            "blocking" if requirement.field.startswith("[voice.") else "warning"
        )
        # NOT wrapped in ``_redacted`` — see this function's docstring: the
        # inputs are provably non-secret and the output is catalog text the
        # redactor would corrupt.
        defects.append(
            SetupIssue.from_code(
                EASYCAT_E604,
                reason="unset_reference",
                field=requirement.var,
                severity=severity,
                reference=requirement.reference,
                var=requirement.var,
            )
        )
    return replace(plan, defects=tuple(defects))


def degraded_extra_roles(plan: ProviderPlan) -> list[tuple[str, str]]:
    """``(role, extra)`` pairs recovered from the planner's degraded warnings.

    THE ONLY parser of ``build_provider_plan``'s
    ``f"{role}_extra_{extra}_missing_degraded"`` token. Every surface that needs
    the pairs — :func:`plan_issues` here, ``doctor``'s ``SelectedApp``
    derivation — calls this, so the token grammar has exactly one owner and a
    change to its shape cannot leave one reader silently reporting "no degraded
    extras".
    """
    pairs: list[tuple[str, str]] = []
    for warning in plan.warnings:
        if not warning.endswith(_DEGRADED_SUFFIX):
            continue
        body = warning[: -len(_DEGRADED_SUFFIX)]
        role, separator, extra = body.partition(_DEGRADED_INFIX)
        if separator and role and extra:
            pairs.append((role, extra))
    return pairs


def selection_to_dict(selection: ProviderSelection) -> dict[str, Any]:
    """The ONE ``ProviderSelection`` -> JSON shape.

    Keys, order, and value types are what ``easycat plan --json`` and the
    server's ``/plan`` body have always emitted: ``role``, ``provider``,
    ``model``, ``config_type``, ``extra``, ``required_env``, ``capabilities``
    (sorted list). Both surfaces call this, so the shape has one owner.
    """
    return {
        "role": selection.role,
        "provider": selection.provider,
        "model": selection.model,
        "config_type": selection.config_type,
        "extra": selection.extra,
        "required_env": selection.required_env,
        "capabilities": sorted(selection.capabilities),
    }


def plan_body(plan: ProviderPlan) -> dict[str, Any]:
    """The seven shared ``/plan`` + ``plan --json`` keys, in one place.

    ``{profile, selected, missing_env, missing_extras, warnings,
    blocking_errors, has_blocking_errors}``. The CLI spreads it into its
    envelope; ``VoiceServer.plan_payload`` adds ``manifest_loaded`` on top.
    Both then add ``issues`` from :func:`plan_issues`.
    """
    return {
        "profile": plan.profile,
        "selected": {
            role: selection_to_dict(selection) for role, selection in plan.selected.items()
        },
        "missing_env": list(plan.missing_env),
        "missing_extras": list(plan.missing_extras),
        "warnings": list(plan.warnings),
        "blocking_errors": list(plan.blocking_errors()),
        "has_blocking_errors": plan.has_blocking_errors,
    }


def plan_issues(plan: ProviderPlan) -> tuple[SetupIssue, ...]:
    """Role-attributed issues for a resolved plan, in pipeline-role order.

    Walks ``provider_plan._ROLE_ORDER`` and pairs each selected role with the
    plan's ALREADY-COMPUTED ``missing_env`` / ``missing_extras`` sets. It does
    NOT recompute the gaps, so a planner refactor cannot break the wire
    contract. Deduped on ``(reason, field)`` so a var two roles share is
    reported once, attributed to the first role in pipeline order.
    """
    missing_env = set(plan.missing_env)
    missing_extras = set(plan.missing_extras)
    degraded = dict(degraded_extra_roles(plan))

    issues: list[SetupIssue] = []
    seen: set[tuple[str, str]] = set()

    def _add(issue: SetupIssue) -> None:
        key = (issue.reason, issue.field)
        if key in seen:
            return
        seen.add(key)
        issues.append(issue)

    for role in _ROLE_ORDER:
        choice = plan.selected.get(role)
        if choice is None:
            continue
        if choice.required_env and choice.required_env in missing_env:
            _add(
                SetupIssue.from_code(
                    EASYCAT_E203,
                    reason="missing_env",
                    field=choice.required_env,
                    role=role,
                    severity="blocking",
                    var=choice.required_env,
                )
            )
        if choice.extra and choice.extra in missing_extras:
            _add(
                SetupIssue.from_code(
                    EASYCAT_E202,
                    reason="missing_extra",
                    field=choice.extra,
                    role=role,
                    severity="blocking",
                    extra=choice.extra,
                )
            )
        degraded_extra = degraded.get(role)
        if degraded_extra:
            _add(
                SetupIssue.from_code(
                    EASYCAT_E202,
                    reason="missing_extra",
                    field=degraded_extra,
                    role=role,
                    severity="warning",
                    extra=degraded_extra,
                )
            )
    for defect in plan.defects:
        _add(defect)

    order: dict[str, int] = {role: index for index, role in enumerate(_ROLE_ORDER)}
    return tuple(
        sorted(
            issues,
            key=lambda issue: (
                issue.severity,
                order.get(issue.role, len(order)),
                issue.code,
                issue.field,
            ),
        )
    )


__all__ = [
    "build_manifest_plan",
    "degraded_extra_roles",
    "load_selected_profile",
    "plan_body",
    "plan_issues",
    "plan_selected_profile",
    "selection_error",
    "selection_issue",
    "selection_to_dict",
]
