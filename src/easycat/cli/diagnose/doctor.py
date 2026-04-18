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
}


def check_env_vars() -> list[CheckResult]:
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
                    "Set at least one of OPENAI_API_KEY, DEEPGRAM_API_KEY, or ELEVENLABS_API_KEY."
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


def _run_all_checks(only_provider: str | None) -> list[CheckResult]:
    results: list[CheckResult] = []
    results.append(check_python_version())
    results.append(check_easycat_version())
    results.extend(check_env_vars())
    results.extend(check_provider_reachability(only_provider=only_provider))
    results.append(check_onnxruntime())
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

    results = _run_all_checks(only_provider=only_provider)

    if fix:
        # ``--fix`` is documented but not wired in M1.  Be transparent.
        stderr_console.print(
            "[dim]--fix is a placeholder in this release; no automatic fixes will be applied.[/]"
        )

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
