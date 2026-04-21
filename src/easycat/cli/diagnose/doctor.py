"""``easycat doctor`` — first-run environment verification.

Runs five checks against the local environment and prints a Rich
table.  Every failure row is tagged with its ``EASYCAT_Exxx`` code so
the user (or their coding agent) can look up the fix via
``easycat explain``.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from rich.table import Table

from easycat.cli._errors import cli_command
from easycat.cli._output import emit_json, json_envelope, stderr_console, stdout_console


@dataclass
class CheckResult:
    """One row in the doctor report."""

    name: str
    status: str  # "ok" | "fail" | "skip"
    detail: str = ""
    code: str = ""  # EASYCAT_Exxx when status == "fail"
    fix: str = ""  # one-liner suggestion shown to TTY users

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
        }
        if self.code:
            payload["code"] = self.code
        if self.fix:
            payload["fix"] = self.fix
        return payload


# ── Individual checks ─────────────────────────────────────────────


def check_python_version() -> CheckResult:
    found = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info < (3, 11):  # noqa: UP036 — intentional diagnostic check
        return CheckResult(
            name="python_version",
            status="fail",
            detail=f"Python {found}",
            code="EASYCAT_E201",
            fix="Install Python 3.11+ (e.g. `uv python install 3.12`).",
        )
    return CheckResult(name="python_version", status="ok", detail=f"Python {found}")


def check_easycat_version() -> CheckResult:
    try:
        version = importlib.metadata.version("easycat")
    except importlib.metadata.PackageNotFoundError:
        return CheckResult(
            name="easycat_version",
            status="fail",
            detail="easycat package not found",
            code="EASYCAT_E202",
            fix="uv add easycat",
        )
    # Detect which integration extras are importable — informational,
    # not fail/ok.
    integrations: list[str] = []
    for module, name in (
        ("agents", "openai-agents"),
        ("pydantic_ai", "pydantic-ai"),
        ("sounddevice", "local"),
        ("deepgram", "deepgram"),
        ("elevenlabs", "elevenlabs"),
        ("onnxruntime", "smart-turn"),
    ):
        try:
            importlib.import_module(module)
            integrations.append(name)
        except ImportError:
            pass
    detail = f"easycat {version}"
    if integrations:
        detail += f"  [dim](extras: {', '.join(integrations)})[/]"
    return CheckResult(name="easycat_version", status="ok", detail=detail)


# Provider → env var that holds its API key.  Used for both the
# env-var presence check (E203) and the reachability check (E204).
_PROVIDER_ENV: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "cartesia": "CARTESIA_API_KEY",
}


def check_env_vars(only_provider: str | None = None) -> list[CheckResult]:
    # Scoped mode: user asked to verify a specific provider.  A missing
    # key for *that* provider must fail — otherwise `doctor --provider X`
    # can false-green when a different provider happens to be configured.
    if only_provider is not None:
        var = _PROVIDER_ENV.get(only_provider)
        if var is None:
            return []
        if os.getenv(var, ""):
            return [CheckResult(name=f"env_{only_provider}", status="ok", detail=f"{var} set")]
        return [
            CheckResult(
                name=f"env_{only_provider}",
                status="fail",
                detail=f"{var} is not set",
                code="EASYCAT_E203",
                fix=f"Set {var}: `export {var}=...`.",
            )
        ]

    results: list[CheckResult] = []
    any_set = False
    for provider, var in _PROVIDER_ENV.items():
        value = os.getenv(var, "")
        if value:
            any_set = True
            results.append(CheckResult(name=f"env_{provider}", status="ok", detail=f"{var} set"))
        else:
            results.append(
                CheckResult(
                    name=f"env_{provider}",
                    status="skip",
                    detail=f"{var} not set",
                )
            )
    if not any_set:
        # Overall env var check fails only if ZERO API keys are configured —
        # per-provider "skip" rows already show which ones are missing.
        results.append(
            CheckResult(
                name="env_any",
                status="fail",
                detail="no provider API keys set",
                code="EASYCAT_E203",
                fix=(
                    "Set at least one of OPENAI_API_KEY, DEEPGRAM_API_KEY, "
                    "ELEVENLABS_API_KEY, or CARTESIA_API_KEY."
                ),
            )
        )
    return results


# Provider → base URL probed with a HEAD request.  Failures here are
# almost always network/DNS/regional, not auth — the HEAD request
# does not include the API key.
_PROVIDER_PROBE_URL: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "deepgram": "https://api.deepgram.com",
    "elevenlabs": "https://api.elevenlabs.io/v1",
    "cartesia": "https://api.cartesia.ai",
}


def check_provider_reachability(
    only_provider: str | None = None, timeout: float = 2.0
) -> list[CheckResult]:
    import httpx

    results: list[CheckResult] = []
    for provider, var in _PROVIDER_ENV.items():
        if only_provider and only_provider != provider:
            continue
        if not os.getenv(var):
            # Skip probes for unconfigured providers; we only care that
            # the configured ones are reachable.
            continue
        url = _PROVIDER_PROBE_URL[provider]
        try:
            r = httpx.head(url, timeout=timeout, follow_redirects=True)
            # Any response (even 4xx) means the host is reachable.
            results.append(
                CheckResult(
                    name=f"reach_{provider}",
                    status="ok",
                    detail=f"{provider} reachable ({r.status_code})",
                )
            )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            results.append(
                CheckResult(
                    name=f"reach_{provider}",
                    status="fail",
                    detail=f"{provider}: {type(exc).__name__}",
                    code="EASYCAT_E204",
                    fix=(
                        f"Check network connectivity, DNS, and provider status. HEAD {url} failed."
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                CheckResult(
                    name=f"reach_{provider}",
                    status="fail",
                    detail=f"{provider}: {exc}",
                    code="EASYCAT_E204",
                    fix="Unexpected probe error — check network connectivity.",
                )
            )
    return results


def check_microphone() -> CheckResult:
    """Probe whether a default input device is available.

    Only meaningful when the ``local`` extra's ``sounddevice`` dep is
    present — server-side deployments (WebRTC, Twilio, WebSocket) don't
    need a local mic so a missing ``sounddevice`` is a skip, not a
    failure.  When sounddevice is installed but reports no default
    input, surface ``EASYCAT_E206`` with the platform-specific fix
    pointing the user at OS-level permissions.
    """
    try:
        import sounddevice as sd  # type: ignore[import-untyped]
    except ImportError:
        return CheckResult(
            name="microphone",
            status="skip",
            detail="sounddevice not installed (only required for local transport)",
        )
    try:
        # ``sd.default.device`` is a two-tuple ``(input, output)`` when
        # set, or a pair of -1 when nothing is configured.  Some
        # sounddevice builds return a single int for non-default
        # configurations; handle both shapes defensively.
        raw = sd.default.device
        default_input = raw[0] if isinstance(raw, (tuple, list)) else raw
        if default_input is None or default_input == -1:
            return CheckResult(
                name="microphone",
                status="fail",
                detail="no default input device",
                code="EASYCAT_E206",
                fix=(
                    "macOS: grant mic access to the terminal. "
                    "Linux: check PulseAudio/PipeWire. Windows: check Sound settings."
                ),
            )
        # Try to resolve the device name for an informative OK row.
        try:
            info = sd.query_devices(default_input, "input")
            name = info.get("name", "unknown") if isinstance(info, dict) else "unknown"
        except Exception:  # noqa: BLE001
            name = "available"
        return CheckResult(name="microphone", status="ok", detail=f"default input: {name}")
    except Exception as exc:  # noqa: BLE001
        # Unexpected sounddevice errors (portaudio missing, etc.) are
        # reported as a skip because they don't invalidate the rest of
        # the doctor output — the user still has a working machine for
        # non-local transports.
        return CheckResult(
            name="microphone",
            status="skip",
            detail=f"sounddevice probe failed: {type(exc).__name__}",
        )


def _journal_dir() -> Path:
    """Resolve the default journal directory.

    Mirrors the fallback order the runtime uses so the check reports on
    the path the runtime will actually try to write to.
    """
    xdg = os.getenv("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "easycat" / "journals"


def check_journal_writable() -> CheckResult:
    """Verify the journal directory exists and is writable.

    A silently read-only journal dir is the highest-pain failure mode
    because the session looks healthy but loses every record; catching
    this at ``doctor`` time is the whole point of having E207 in the
    registry.
    """
    path = _journal_dir()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return CheckResult(
            name="journal_writable",
            status="fail",
            detail=f"cannot create {path}: {exc}",
            code="EASYCAT_E207",
            fix=f"mkdir -p {path} && chmod u+w {path}",
        )
    probe = path / ".doctor-write-probe"
    try:
        probe.write_bytes(b"ok")
    except OSError as exc:
        return CheckResult(
            name="journal_writable",
            status="fail",
            detail=f"{path} is not writable: {exc}",
            code="EASYCAT_E207",
            fix=f"chmod u+w {path}",
        )
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
    return CheckResult(name="journal_writable", status="ok", detail=str(path))


def check_disk_space(min_free_mb: int = 500) -> CheckResult:
    """Warn before the journal dir runs out of space.

    ``min_free_mb`` matches the threshold documented in the
    ``EASYCAT_E208`` registry entry.
    """
    import shutil as _shutil

    path = _journal_dir()
    # Walk up to the nearest existing parent so the check works even if
    # the journal dir hasn't been created yet.
    probe_path = path
    while not probe_path.exists() and probe_path != probe_path.parent:
        probe_path = probe_path.parent
    try:
        usage = _shutil.disk_usage(probe_path)
    except OSError as exc:
        return CheckResult(
            name="disk_space",
            status="skip",
            detail=f"cannot stat {probe_path}: {exc}",
        )
    free_mb = usage.free // (1024 * 1024)
    if free_mb < min_free_mb:
        return CheckResult(
            name="disk_space",
            status="fail",
            detail=f"{free_mb}MB free at {probe_path} (need >= {min_free_mb}MB)",
            code="EASYCAT_E208",
            fix="Free up disk space or set XDG_CACHE_HOME to a larger filesystem.",
        )
    return CheckResult(
        name="disk_space",
        status="ok",
        detail=f"{free_mb}MB free at {probe_path}",
    )


def check_onnxruntime() -> CheckResult:
    """Report whether onnxruntime is importable.

    Smart Turn endpoint detection is optional; a missing onnxruntime
    should surface as a *skip* (informational) rather than a failure.
    ``EASYCAT_E205`` still exists for the code path that tries to
    activate Smart Turn without onnxruntime available — that failure
    happens at config time, not at doctor time.
    """
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return CheckResult(
            name="onnxruntime",
            status="skip",
            detail="onnxruntime not installed (smart-turn extra is optional)",
        )
    return CheckResult(name="onnxruntime", status="ok", detail="onnxruntime importable")


# ── Orchestration ────────────────────────────────────────────────


def _apply_safe_fixes(results: list[CheckResult]) -> int:
    """Apply narrow auto-fixes for failures marked safe to remediate.

    Returns the number of fixes actually applied.  Only touches the
    journal directory today (``EASYCAT_E207``) because mkdir is the
    one class of fix that has no ambiguity, no user-data side effect,
    and no security implication.  Future entries here must meet the
    same bar.
    """
    applied = 0
    for result in results:
        if result.code == "EASYCAT_E207":
            try:
                _journal_dir().mkdir(parents=True, exist_ok=True)
                applied += 1
            except OSError:
                continue
    return applied


def _run_all_checks(only_provider: str | None) -> list[CheckResult]:
    results: list[CheckResult] = []
    results.append(check_python_version())
    results.append(check_easycat_version())
    results.extend(check_env_vars(only_provider=only_provider))
    results.extend(check_provider_reachability(only_provider=only_provider))
    results.append(check_onnxruntime())
    results.append(check_microphone())
    results.append(check_journal_writable())
    results.append(check_disk_space())
    return results


_STATUS_GLYPH = {"ok": "[green]✓[/]", "fail": "[red]✗[/]", "skip": "[dim]~[/]"}


def _render_report(results: list[CheckResult], profile: str) -> None:
    stderr_console.print(f"[bold]EasyCat doctor[/] — {profile} profile")
    stderr_console.print()
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column(style="bold", no_wrap=True)
    table.add_column()
    table.add_column(overflow="fold")
    for r in results:
        glyph = _STATUS_GLYPH.get(r.status, "?")
        detail = r.detail
        if r.status == "fail":
            detail = f"[red]{detail}[/] [red]({r.code})[/]"
        table.add_row(glyph, r.name, detail)
        if r.status == "fail" and r.fix:
            short = r.code.removeprefix("EASYCAT_")
            table.add_row(
                "",
                "",
                f"  [dim]Fix:[/] {r.fix}",
            )
            table.add_row(
                "",
                "",
                f"  [dim]Explain:[/] [cyan]easycat explain {short}[/]",
            )
    stderr_console.print(table)
    stderr_console.print()
    passed = sum(1 for r in results if r.status == "ok")
    failed = sum(1 for r in results if r.status == "fail")
    skipped = sum(1 for r in results if r.status == "skip")
    total = passed + failed + skipped
    if failed:
        stderr_console.print(
            f"[red]{failed} failed[/], {passed} passed, {skipped} skipped (of {total})."
        )
    else:
        stderr_console.print(f"[green]{passed} passed[/], {skipped} skipped (of {total}).")


@cli_command
def doctor(
    environment: str = typer.Option(
        "dev",
        "--environment",
        help="Profile to check.  Choices: dev, production.",
    ),
    only_provider: str | None = typer.Option(
        None,
        "--provider",
        help="Only check this provider (e.g. --provider openai).",
    ),
    fix: bool = typer.Option(
        False,
        "--fix",
        help="Offer auto-fixes for safe issues (placeholder in M1 — TBD).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Check environment, credentials, reachability, and ONNX availability."""
    if environment not in {"dev", "production"}:
        stderr_console.print(
            f"  [red]✗[/] Unknown --environment {environment!r}. Use 'dev' or 'production'."
        )
        raise typer.Exit(2)

    if only_provider is not None and only_provider not in _PROVIDER_ENV:
        # A typo or mis-cased provider must fail loudly rather than fall
        # through to the generic checks and exit 0 — automation that
        # scopes doctor to one provider would otherwise treat the typo as
        # a green run.
        supported = ", ".join(sorted(_PROVIDER_ENV))
        stderr_console.print(
            f"  [red]✗[/] Unknown --provider {only_provider!r}. Supported: {supported}."
        )
        raise typer.Exit(2)

    results = _run_all_checks(only_provider=only_provider)

    if fix:
        # ``--fix`` handles the narrow, safe remediations: creating the
        # journal directory (E207).  API-key and mic-permission fixes
        # stay manual — no CLI should be writing to ``~/.bashrc`` or
        # flipping macOS privacy prompts on the user's behalf.
        applied = _apply_safe_fixes(results)
        if applied:
            stderr_console.print(
                f"[dim]--fix applied {applied} remediation(s); re-running checks.[/]"
            )
            results = _run_all_checks(only_provider=only_provider)
        else:
            stderr_console.print("[dim]--fix: no auto-remediatable issues found.[/]")

    if json_output:
        failed = any(r.status == "fail" for r in results)
        emit_json(
            json_envelope(
                "doctor",
                status="error" if failed else "ok",
                environment=environment,
                checks=[r.as_dict() for r in results],
            )
        )
    else:
        _render_report(results, profile=environment)

    failed = any(r.status == "fail" for r in results)
    raise typer.Exit(1 if failed else 0)


__all__: list[str] = ["doctor"]

# ``stdout_console`` is kept in the import list for parity with other
# commands even though the current doctor implementation uses stderr for
# the human report — future JSON path variants may swap to stdout.
_ = stdout_console
