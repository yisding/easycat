"""``easycat doctor`` — first-run environment verification.

Runs checks against the local environment (python, easycat, env vars,
provider reachability, onnxruntime, audio resampling, microphone, journal, disk)
and prints a Rich table.  Every failure row is tagged with its
``EASYCAT_Exxx`` code so the user (or their coding agent) can look up
the fix via ``easycat explain``.

The ``production`` environment profile drops the local-microphone check
(server deployments have no mic) since those checks are only relevant to
the local-transport ``dev`` profile.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import math
import os
import re
import shlex
import sys
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NoReturn

import typer
from rich.markup import escape
from rich.table import Table

from easycat._audio_utils import resample_backend
from easycat._credentials import has_usable_credential
from easycat._extras import PORTAUDIO_INSTALL_FIX
from easycat._provider_registry import credential_env_vars
from easycat.cli._errors import cli_command
from easycat.cli._output import emit_command_error, emit_json, json_envelope, stderr_console


@dataclass
class CheckResult:
    """One row in the doctor report."""

    name: str
    status: str  # "ok" | "fail" | "skip"
    detail: str = ""
    requirement: Literal["required", "optional", "unused", "not_applicable"] = "required"
    code: str = ""  # EASYCAT_Exxx when status == "fail"
    fix: str = ""  # one-liner suggestion shown to TTY users

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "requirement": self.requirement,
        }
        if self.code:
            payload["code"] = self.code
        if self.fix:
            payload["fix"] = self.fix
        return payload


@dataclass(frozen=True, slots=True)
class ScaffoldRequirements:
    """Readiness requirements embedded by ``easycat init`` in pyproject.toml."""

    template: str
    required_env: tuple[str, ...]
    optional_env: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FixResult:
    """One explicit mutation attempted by ``doctor --fix``."""

    action: Literal["create_directory"]
    target: str
    status: Literal["applied", "failed"]
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {
            "action": self.action,
            "target": self.target,
            "status": self.status,
            "detail": self.detail,
        }


# ── Individual checks ─────────────────────────────────────────────


def check_python_version() -> CheckResult:
    found = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info < (3, 11):  # noqa: UP036 — intentional diagnostic check
        return CheckResult(
            name="python_version",
            status="fail",
            detail=f"Python {found}",
            code="EASYCAT_E201",
            fix=(
                "Install Python 3.11+ (e.g. `uv python install 3.12`). "
                "From this repo, rerun setup with `uv sync --python 3.12 --group dev`."
            ),
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
    # NOTE: the Deepgram/ElevenLabs/Cartesia providers talk to their HTTP
    # APIs directly via ``httpx`` and never import a vendor SDK, so there
    # is no SDK module to probe for here — their availability is gated by
    # the API-key env-var checks (E203) instead, not an importable extra.
    integrations: list[str] = []
    for module, name in (
        ("agents", "openai-agents"),
        ("pydantic_ai", "pydantic-ai"),
        ("sounddevice", "local"),
        ("onnxruntime", "smart-turn"),
    ):
        try:
            importlib.import_module(module)
            integrations.append(name)
        except (ImportError, OSError):
            pass
    detail = f"easycat {version}"
    if integrations:
        detail += f" (extras: {', '.join(integrations)})"
    return CheckResult(name="easycat_version", status="ok", detail=detail)


def _provider_env() -> dict[str, str]:
    """Provider → env var that holds its API key.

    Used for both the env-var presence check (E203) and the reachability
    check (E204).  Derived from the live STT/TTS provider catalogs (which
    run entry-point discovery), so third-party providers registered via
    ``register_stt_provider`` / ``register_tts_provider`` or the
    ``easycat.stt_providers`` / ``easycat.tts_providers`` entry-point
    groups get the same checks as built-ins.  Providers sharing an env
    var are collapsed to one row (e.g. ``openai`` and ``openai-realtime``
    both use ``OPENAI_API_KEY``).
    """
    return credential_env_vars()


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SCAFFOLD_TABLE_HEADER = "[tool.easycat.scaffold]"


def _load_scaffold_requirements(
    path: Path = Path("pyproject.toml"),
) -> ScaffoldRequirements | None:
    """Load generated-project requirements, or return ``None`` outside a scaffold.

    EasyCat-generated projects carry a small metadata table in ``pyproject.toml``.
    Reading that table lets the ordinary README preflight command validate the
    project's complete required environment, including non-provider settings such
    as Twilio's public stream URL and auth token. Generic projects remain untouched.
    """
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"could not read {path}: {exc}") from exc
    if _SCAFFOLD_TABLE_HEADER not in text:
        return None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path} has invalid TOML: {exc}") from exc

    scaffold = data.get("tool", {}).get("easycat", {}).get("scaffold")
    if not isinstance(scaffold, dict):
        raise ValueError(f"{path} {_SCAFFOLD_TABLE_HEADER} must be a table")  # noqa: TRY004 domain-specific validation error
    template = scaffold.get("template")
    required_env = scaffold.get("required_env")
    optional_env = scaffold.get("optional_env", [])
    if not isinstance(template, str) or not template.strip():
        raise ValueError(f"{path} scaffold template must be a non-empty string")
    if not isinstance(required_env, list) or not all(
        isinstance(name, str) and _ENV_NAME_RE.match(name) for name in required_env
    ):
        raise ValueError(f"{path} scaffold required_env must be an array of env-var names")
    if not isinstance(optional_env, list) or not all(
        isinstance(name, str) and _ENV_NAME_RE.match(name) for name in optional_env
    ):
        raise ValueError(f"{path} scaffold optional_env must be an array of env-var names")
    required_names = tuple(dict.fromkeys(required_env))
    optional_names = tuple(dict.fromkeys(optional_env))
    overlap = sorted(set(required_names) & set(optional_names))
    if overlap:
        raise ValueError(
            f"{path} scaffold env vars cannot be both required and optional: " + ", ".join(overlap)
        )
    return ScaffoldRequirements(
        template=template,
        required_env=required_names,
        optional_env=optional_names,
    )


_PLACEHOLDER_VALUES = frozenset(
    {
        "...",
        "changeme",
        "change-me",
        "change_me",
        "replace-me",
        "replace_me",
        "placeholder",
        "todo",
    }
)
_PLACEHOLDER_MARKERS = (
    "your-key",
    "your_key",
    "your-api-key",
    "your_api_key",
    "your-auth-token",
    "your_auth_token",
    "your-twilio",
    "your_twilio",
    "your-telnyx",
    "your_telnyx",
    "://your-public-host",
    "://example.",
)


def _looks_like_placeholder(value: str | None) -> bool:
    """Return whether a non-empty env value is an obvious example sentinel."""
    if not has_usable_credential(value):
        return False
    normalized = str(value).strip().casefold()
    return (
        normalized in _PLACEHOLDER_VALUES
        or normalized.startswith(("your-", "your_", "<"))
        or normalized.endswith(">")
        or any(marker in normalized for marker in _PLACEHOLDER_MARKERS)
    )


def _env_value_state(value: str | None) -> str:
    if not has_usable_credential(value):
        return "missing"
    if _looks_like_placeholder(value):
        return "placeholder"
    return "usable"


def _parse_env_file(path: Path, *, allowed_names: set[str]) -> dict[str, str]:  # noqa: C901, PLR0912
    """Parse provider credentials from a dotenv file.

    Doctor imports optional integrations and probes local file/network resources,
    so project dotenv files must not be allowed to rewrite process-wide knobs such
    as PATH, HOME, proxy, SSL, or cache variables before those checks run.
    Variables already exported in the process environment win over file values,
    matching standard dotenv precedence (file entries act as defaults only).
    """
    if not path.is_file():
        raise ValueError(f"{path} is not a file")

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Handle optional `export` prefix (allow `export FOO=bar`)
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        elif stripped == "export":
            continue
        if "=" not in stripped:
            raise ValueError(f"{path}:{line_number}: expected KEY=VALUE")
        key_part, value_part = stripped.split("=", 1)
        key = key_part.strip()
        if not _ENV_NAME_RE.match(key):
            raise ValueError(f"{path}:{line_number}: invalid env var name {key!r}")
        value_raw = value_part.strip()
        # Strip inline comment only when `#` is preceded by whitespace (dotenv semantics, gh 1009)
        # and not inside quotes.
        # Remove trailing comment outside quotes.
        in_single = False
        in_double = False
        comment_idx = None
        for idx, ch in enumerate(value_raw):
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif (
                ch == "#"
                and not in_single
                and not in_double
                and (idx == 0 or value_raw[idx - 1].isspace())
            ):
                comment_idx = idx
                break
        if comment_idx is not None:
            value_raw = value_raw[:comment_idx].rstrip()
        # Parse quoted value via shlex (posix) without comment handling
        if value_raw and (value_raw[0] in ('"', "'")):
            try:
                parts = shlex.split(value_raw, posix=True)
                if len(parts) != 1:
                    raise ValueError(
                        f"{path}:{line_number}: invalid .env syntax: extra tokens after quoted value"  # noqa: E501
                    )
                value = parts[0] if parts else ""
            except ValueError as exc:
                raise ValueError(f"{path}:{line_number}: invalid .env syntax: {exc}") from exc
        else:
            # Unquoted: value is up to comment boundary already stripped
            value = value_raw
            # Unescape? dotenv doesn't unescape unquoted, keep as is
        if key in allowed_names and key not in os.environ:
            values[key] = value
    return values


@contextmanager
def _temporary_env(values: dict[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _missing_provider_result(
    provider: str,
    var: str,
    *,
    state: str,
    requirement: Literal["required", "optional"] = "required",
) -> CheckResult:
    detail = f"{var} looks like a placeholder" if state == "placeholder" else f"{var} is not set"
    return CheckResult(
        name=f"env_{provider}",
        status="fail",
        detail=detail,
        requirement=requirement,
        code="EASYCAT_E203",
        fix=(
            f"Set {var} to a real credential: `export {var}=...`, or fill it in "
            "inside the project `.env` and rerun `easycat doctor --env-file .env`."
        ),
    )


def _scaffold_env_value_error(var: str, value: str) -> str | None:
    """Return the generated runtime's numeric validation error, if any."""
    if var in {
        "TWILIO_WS_PORT",
        "TWILIO_MAX_SESSIONS",
        "TELNYX_WS_PORT",
        "TELNYX_MAX_SESSIONS",
    }:
        try:
            parsed_int = int(value)
        except ValueError:
            parsed_int = 0
        if var in {"TWILIO_WS_PORT", "TELNYX_WS_PORT"} and not 1 <= parsed_int <= 65_535:
            return f"{var} must be an integer from 1 to 65535"
        if var in {"TWILIO_MAX_SESSIONS", "TELNYX_MAX_SESSIONS"} and parsed_int <= 0:
            return f"{var} must be a positive integer"

    if var in {
        "TWILIO_START_TIMEOUT_S",
        "TWILIO_DRAIN_TIMEOUT_S",
        "TWILIO_FORCE_SHUTDOWN_TIMEOUT_S",
        "TELNYX_START_TIMEOUT_S",
        "TELNYX_DRAIN_TIMEOUT_S",
        "TELNYX_FORCE_SHUTDOWN_TIMEOUT_S",
    }:
        try:
            parsed_float = float(value)
        except ValueError:
            parsed_float = float("nan")
        start_timeout = var in {"TWILIO_START_TIMEOUT_S", "TELNYX_START_TIMEOUT_S"}
        valid = math.isfinite(parsed_float) and (
            parsed_float > 0 if start_timeout else parsed_float >= 0
        )
        if not valid:
            if start_timeout:
                return f"{var} must be a finite number greater than zero"
            return f"{var} must be a finite non-negative number"
    return None


def _check_scaffold_env_var(
    var: str,
    *,
    requirement: Literal["required", "optional"],
) -> CheckResult:
    value = os.getenv(var)
    state = _env_value_state(value)
    detail: str | None = None
    if state == "missing":
        if requirement == "optional":
            return CheckResult(
                name=f"env_{var.casefold()}",
                status="skip",
                detail=f"{var} not set (optional)",
                requirement="optional",
            )
        detail = f"{var} is not set"
    elif state == "placeholder":
        detail = f"{var} looks like a placeholder"
    elif var in {"TWILIO_STREAM_URL", "TELNYX_STREAM_URL"} and not str(
        value
    ).casefold().startswith("wss://"):
        detail = f"{var} must use wss://"
    else:
        detail = _scaffold_env_value_error(var, str(value))

    name = f"env_{var.casefold()}"
    if detail is None:
        return CheckResult(
            name=name,
            status="ok",
            detail=f"{var} set",
            requirement=requirement,
        )
    return CheckResult(
        name=name,
        status="fail",
        detail=detail,
        requirement=requirement,
        code="EASYCAT_E210",
        fix=f"Set {var} to the real project value in `.env`, then rerun doctor.",
    )


def _provider_env_result(
    provider: str,
    var: str,
    *,
    state: str,
    required: bool,
    optional: bool,
    requirements_scoped: bool,
) -> tuple[CheckResult, bool]:
    """Classify one hosted-provider credential and whether it is active."""
    if requirements_scoped and not required and not optional:
        detail = (
            f"{var} set but unused by this scaffold" if state == "usable" else f"{var} not set"
        )
        return (
            CheckResult(
                name=f"env_{provider}",
                status="skip",
                detail=detail,
                requirement="unused",
            ),
            False,
        )
    if state == "usable":
        return (
            CheckResult(
                name=f"env_{provider}",
                status="ok",
                detail=f"{var} set",
                requirement="required" if required else "optional",
            ),
            True,
        )
    if state == "placeholder" or required:
        return (
            _missing_provider_result(
                provider,
                var,
                state=state,
                requirement="required" if required else "optional",
            ),
            False,
        )
    if optional:
        return (
            CheckResult(
                name=f"env_{provider}",
                status="skip",
                detail=f"{var} not set (optional)",
                requirement="optional",
            ),
            False,
        )
    return (
        CheckResult(
            name=f"env_{provider}",
            status="skip",
            detail=f"{var} not set",
            requirement="unused",
        ),
        False,
    )


def check_env_vars(
    only_provider: str | None = None,
    *,
    required_env_names: tuple[str, ...] = (),
    optional_env_names: tuple[str, ...] = (),
    requirements_scoped: bool = False,
) -> list[CheckResult]:
    # Scoped mode: user asked to verify a specific provider.  A missing
    # key for *that* provider must fail — otherwise `doctor --provider X`
    # can false-green when a different provider happens to be configured.
    provider_env = _provider_env()
    if only_provider is not None:
        var = provider_env.get(only_provider)
        if var is None:
            return []
        state = _env_value_state(os.getenv(var))
        if state == "usable":
            return [
                CheckResult(
                    name=f"env_{only_provider}",
                    status="ok",
                    detail=f"{var} set",
                    requirement="required",
                )
            ]
        return [_missing_provider_result(only_provider, var, state=state)]

    results: list[CheckResult] = []
    any_set = False
    required = set(required_env_names)
    optional = set(optional_env_names)
    for provider, var in provider_env.items():
        result, active = _provider_env_result(
            provider,
            var,
            state=_env_value_state(os.getenv(var, "")),
            required=var in required,
            optional=var in optional,
            requirements_scoped=requirements_scoped,
        )
        results.append(result)
        any_set = any_set or active
    provider_vars = set(provider_env.values())
    for var in required_env_names:
        if var not in provider_vars:
            results.append(_check_scaffold_env_var(var, requirement="required"))
    for var in optional_env_names:
        if var not in provider_vars:
            results.append(_check_scaffold_env_var(var, requirement="optional"))

    if not any_set:
        # No hosted key is a valid generic state: local and custom providers can
        # be keyless. A selected scaffold/provider requirement already has a
        # dedicated failed row above, while this aggregate remains informational.
        results.append(
            CheckResult(
                name="env_any",
                status="skip",
                detail="no hosted provider API keys set (valid for keyless/local/custom setups)",
                requirement="not_applicable",
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
    only_provider: str | None = None,
    timeout: float = 2.0,
    *,
    required_env_names: tuple[str, ...] = (),
    optional_env_names: tuple[str, ...] = (),
    requirements_scoped: bool = False,
) -> list[CheckResult]:
    import httpx

    results: list[CheckResult] = []
    required = set(required_env_names)
    scoped = required | set(optional_env_names)
    for provider, var in _provider_env().items():
        if only_provider and only_provider != provider:
            continue
        if requirements_scoped and var not in scoped:
            continue
        if _env_value_state(os.getenv(var)) != "usable":
            # Skip probes for unconfigured providers; we only care that
            # the configured ones are reachable.
            continue
        url = _PROVIDER_PROBE_URL.get(provider)
        if url is None:
            # Discovered third-party providers carry no probe URL; their
            # availability is covered by the env-var check above.
            continue
        try:
            r = httpx.head(url, timeout=timeout, follow_redirects=True)
            # Any response (even 4xx) means the host is reachable.
            results.append(
                CheckResult(
                    name=f"reach_{provider}",
                    status="ok",
                    detail=(
                        f"{provider} network reachable (HTTP {r.status_code}); "
                        "credential validity not checked"
                    ),
                    requirement=(
                        "required" if only_provider is not None or var in required else "optional"
                    ),
                )
            )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            results.append(
                CheckResult(
                    name=f"reach_{provider}",
                    status="fail",
                    detail=f"{provider}: {type(exc).__name__}",
                    requirement=(
                        "required" if only_provider is not None or var in required else "optional"
                    ),
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
                    requirement=(
                        "required" if only_provider is not None or var in required else "optional"
                    ),
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
    failure. A present Python package that cannot load the PortAudio
    runtime is a failed local setup (``EASYCAT_E209``). When
    sounddevice reports no default input, surface ``EASYCAT_E206``
    with the platform-specific permissions fix.
    """
    try:
        sd = importlib.import_module("sounddevice")
    except ImportError:
        return CheckResult(
            name="microphone",
            status="skip",
            detail="sounddevice not installed (only required for local transport)",
            requirement="optional",
        )
    except OSError as exc:
        return CheckResult(
            name="microphone",
            status="fail",
            detail=f"sounddevice could not load PortAudio: {exc}",
            requirement="optional",
            code="EASYCAT_E209",
            fix=PORTAUDIO_INSTALL_FIX,
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
                requirement="optional",
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
        return CheckResult(
            name="microphone",
            status="ok",
            detail=f"default input: {name}",
            requirement="optional",
        )
    except Exception as exc:  # noqa: BLE001
        # Unexpected sounddevice errors (portaudio missing, etc.) are
        # reported as a skip because they don't invalidate the rest of
        # the doctor output — the user still has a working machine for
        # non-local transports.
        return CheckResult(
            name="microphone",
            status="skip",
            detail=f"sounddevice probe failed: {type(exc).__name__}",
            requirement="optional",
        )


def _journal_dir() -> Path:
    """Resolve the default journal directory.

    Mirrors the fallback order the runtime uses so the check reports on
    the path the runtime will actually try to write to.
    """
    return Path(os.environ.get("EASYCAT_DATA_DIR", ".easycat")) / "journals"


def check_journal_writable() -> CheckResult:
    """Verify an existing journal directory is writable without creating it.

    A silently read-only journal dir is the highest-pain failure mode
    because the session looks healthy but loses every record; catching
    this at ``doctor`` time is the whole point of having E207 in the
    registry. A missing directory is reported as an unverified skip: the
    read-only default never mutates the checkout, while ``doctor --fix``
    explicitly creates it and re-runs this probe.
    """
    path = _journal_dir()
    if not path.exists():
        existing_parent = path.parent
        while not existing_parent.exists() and existing_parent != existing_parent.parent:
            existing_parent = existing_parent.parent
        if not existing_parent.is_dir():
            return CheckResult(
                name="journal_writable",
                status="fail",
                detail=f"cannot create {path}: {existing_parent} is not a directory",
                code="EASYCAT_E207",
                fix=f"choose a directory for EASYCAT_DATA_DIR, then mkdir -p {path}",
            )
        return CheckResult(
            name="journal_writable",
            status="skip",
            detail=f"{path} does not exist; writable state was not probed",
            code="EASYCAT_E207",
            fix=f"easycat doctor --fix  # creates {path}",
        )
    if not path.is_dir():
        return CheckResult(
            name="journal_writable",
            status="fail",
            detail=f"{path} exists but is not a directory",
            code="EASYCAT_E207",
            fix=f"set EASYCAT_DATA_DIR to a writable directory; refusing to replace {path}",
        )
    # Keep the default diagnostic observational: permission inspection can
    # produce a useful readiness answer without creating and deleting a probe
    # file in a user's journal. ``--fix`` remains the only mutating path.
    if not os.access(path, os.W_OK | os.X_OK):
        return CheckResult(
            name="journal_writable",
            status="fail",
            detail=f"{path} is not writable by the current process",
            code="EASYCAT_E207",
            fix=f"chmod u+w {path}",
        )
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
            fix="Free up disk space or set EASYCAT_DATA_DIR to a larger filesystem.",
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
            requirement="optional",
        )
    return CheckResult(
        name="onnxruntime",
        status="ok",
        detail="onnxruntime importable",
        requirement="optional",
    )


def check_resampling_backend() -> CheckResult:
    """Report whether high-quality audio resampling is available."""
    backend = resample_backend()
    if backend == "linear":
        return CheckResult(
            name="audio_resampling",
            status="skip",
            detail=(
                "filtered linear fallback; install an audio extra such as "
                "easycat[quickstart] for SoXR"
            ),
            requirement="optional",
        )
    return CheckResult(
        name="audio_resampling",
        status="ok",
        detail=f"{backend} high-quality backend",
        requirement="optional",
    )


# ── Orchestration ────────────────────────────────────────────────


def _apply_safe_fixes(results: list[CheckResult]) -> list[FixResult]:
    """Apply narrow auto-fixes for failures marked safe to remediate.

    Returns a result for every attempted mutation. Only touches the
    journal directory today (``EASYCAT_E207``) because mkdir is the
    one class of fix that has no ambiguity, no user-data side effect,
    and no security implication.  Future entries here must meet the
    same bar.
    """
    path = _journal_dir()
    should_create = (
        any(
            result.name == "journal_writable" and result.code == "EASYCAT_E207"
            for result in results
        )
        and not path.exists()
    )
    if not should_create:
        return []
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return [
            FixResult(
                action="create_directory",
                target=str(path),
                status="failed",
                detail=str(exc),
            )
        ]
    return [
        FixResult(
            action="create_directory",
            target=str(path),
            status="applied",
            detail="created journal directory",
        )
    ]


def _apply_requested_fixes(
    results: list[CheckResult],
    *,
    only_provider: str | None,
    environment: str,
    scaffold: ScaffoldRequirements | None,
) -> tuple[list[CheckResult], list[FixResult]]:
    """Apply, report, and verify explicit ``--fix`` mutations."""
    fix_results = _apply_safe_fixes(results)
    for result in fix_results:
        target = escape(result.target)
        detail = escape(result.detail)
        stderr_console.print(f"[dim]--fix {result.status}: {result.action} {target} ({detail})[/]")
    if any(result.status == "applied" for result in fix_results):
        stderr_console.print("[dim]--fix: re-running checks after remediation.[/]")
        results = _run_all_checks(
            only_provider=only_provider,
            environment=environment,
            scaffold=scaffold,
        )
    elif not fix_results:
        stderr_console.print("[dim]--fix: no auto-remediatable issues found.[/]")
    return results, fix_results


def _run_all_checks(
    only_provider: str | None,
    environment: str = "dev",
    scaffold: ScaffoldRequirements | None = None,
) -> list[CheckResult]:
    results: list[CheckResult] = []
    results.append(check_python_version())
    results.append(check_easycat_version())
    if scaffold is not None:
        results.append(
            CheckResult(
                name="scaffold",
                status="ok",
                detail=(
                    f"{scaffold.template} ({len(scaffold.required_env)} required, "
                    f"{len(scaffold.optional_env)} optional env vars)"
                ),
            )
        )
    results.extend(
        check_env_vars(
            only_provider=only_provider,
            required_env_names=scaffold.required_env if scaffold is not None else (),
            optional_env_names=scaffold.optional_env if scaffold is not None else (),
            requirements_scoped=scaffold is not None,
        )
    )
    results.extend(
        check_provider_reachability(
            only_provider=only_provider,
            required_env_names=scaffold.required_env if scaffold is not None else (),
            optional_env_names=scaffold.optional_env if scaffold is not None else (),
            requirements_scoped=scaffold is not None,
        )
    )
    results.append(check_onnxruntime())
    results.append(check_resampling_backend())
    # The local microphone is only meaningful for the dev profile's
    # local transport; server deployments (production) have no mic, so
    # running the probe there only produces a noisy, irrelevant skip.
    if environment != "production":
        results.append(check_microphone())
    results.append(check_journal_writable())
    results.append(check_disk_space())
    return results


def _doctor_usage_error(message: str, *, json_output: bool) -> NoReturn:
    emit_command_error("doctor", message, json_output=json_output)
    raise typer.Exit(2)


_STATUS_GLYPH = {"ok": "[green]✓[/]", "fail": "[red]✗[/]", "skip": "[dim]~[/]"}


def _render_report(
    results: list[CheckResult],
    profile: str,
    *,
    failed_fixes: int = 0,
) -> None:
    stderr_console.print(f"[bold]EasyCat doctor[/] — {profile} profile")
    stderr_console.print()
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column(style="bold", no_wrap=True)
    table.add_column()
    table.add_column(overflow="fold")
    for r in results:
        glyph = _STATUS_GLYPH.get(r.status, "?")
        detail = escape(r.detail)
        if r.status == "fail":
            detail = f"[red]{detail}[/] [red]({escape(r.code)})[/]"
        table.add_row(glyph, escape(r.name), detail)
        if r.status == "fail" and r.fix:
            short = r.code.removeprefix("EASYCAT_")
            table.add_row(
                "",
                "",
                f"  [dim]Fix:[/] {escape(r.fix)}",
            )
            table.add_row(
                "",
                "",
                f"  [dim]Explain:[/] [cyan]easycat explain {escape(short)}[/]",
            )
    stderr_console.print(table)
    stderr_console.print()
    passed = sum(1 for r in results if r.status == "ok")
    failed = sum(1 for r in results if r.status == "fail")
    skipped = sum(1 for r in results if r.status == "skip")
    total = passed + failed + skipped
    if failed_fixes:
        fix_label = "fix" if failed_fixes == 1 else "fixes"
        stderr_console.print(
            f"[red]{failed} checks failed[/], [red]{failed_fixes} {fix_label} failed[/], "
            f"{passed} passed, {skipped} skipped (of {total})."
        )
    elif failed:
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
        help=(
            "Apply safe auto-fixes, then re-run checks. Currently only "
            "creates a missing journal directory (EASYCAT_E207)."
        ),
    ),
    env_file: Path | None = typer.Option(
        None,
        "--env-file",
        help="Load environment variables from a .env file before checks (for example, .env).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Check credentials, extras, providers, audio backends, and local resources."""
    if environment not in {"dev", "production"}:
        _doctor_usage_error(
            f"Unknown --environment {environment!r}. Use 'dev' or 'production'.",
            json_output=json_output,
        )

    provider_env = _provider_env()
    if only_provider is not None and only_provider not in provider_env:
        # A typo or mis-cased provider must fail loudly rather than fall
        # through to the generic checks and exit 0 — automation that
        # scopes doctor to one provider would otherwise treat the typo as
        # a green run.
        supported = ", ".join(sorted(provider_env))
        _doctor_usage_error(
            f"Unknown --provider {only_provider!r}. Supported: {supported}.",
            json_output=json_output,
        )

    try:
        scaffold = _load_scaffold_requirements() if only_provider is None else None
    except ValueError as exc:
        _doctor_usage_error(f"Invalid scaffold metadata: {exc}", json_output=json_output)

    allowed_env_names = set(provider_env.values())
    if scaffold is not None:
        allowed_env_names.update(scaffold.required_env)
        allowed_env_names.update(scaffold.optional_env)
    try:
        env_values = (
            _parse_env_file(env_file, allowed_names=allowed_env_names)
            if env_file is not None
            else {}
        )
    except ValueError as exc:
        _doctor_usage_error(f"Invalid --env-file: {exc}", json_output=json_output)

    with _temporary_env(env_values):
        results = _run_all_checks(
            only_provider=only_provider,
            environment=environment,
            scaffold=scaffold,
        )
        fix_results: list[FixResult] = []

        if fix:
            # ``--fix`` handles the narrow, safe remediations: creating the
            # journal directory (E207).  API-key and mic-permission fixes
            # stay manual — no CLI should be writing to ``~/.bashrc`` or
            # flipping macOS privacy prompts on the user's behalf.
            results, fix_results = _apply_requested_fixes(
                results,
                only_provider=only_provider,
                environment=environment,
                scaffold=scaffold,
            )

        failed_fixes = sum(result.status == "failed" for result in fix_results)
        failed = any(r.status == "fail" for r in results) or failed_fixes > 0
        if json_output:
            fields: dict[str, Any] = {
                "environment": environment,
                "checks": [r.as_dict() for r in results],
            }
            if fix:
                fields["fixes"] = [result.as_dict() for result in fix_results]
            emit_json(json_envelope("doctor", status="error" if failed else "ok", **fields))
        else:
            _render_report(results, profile=environment, failed_fixes=failed_fixes)

    raise typer.Exit(1 if failed else 0)


__all__: list[str] = ["doctor"]
