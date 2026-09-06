"""EasyCat error base class and code registry.

This module is the single source of truth for ``EASYCAT_Exxx`` error
codes.  Every code is both a runtime factory (``EASYCAT_E101(target=...)``
produces a tagged :class:`EasyCatError`) and a documentation entry
that ``easycat explain`` reads from.

Codes are namespaced by range:

* ``E1xx`` — scaffolding (init, templates, config JSON)
* ``E2xx`` — environment (doctor checks)
* ``E3xx`` — runtime (session execution)
* ``E4xx`` — bundle / replay
* ``E5xx`` — CLI usage
* ``E6xx`` — project manifest (``easycat.toml``)

Adding a code is a one-file change: call :func:`register` at module
load time and (optionally) bind the returned factory to a module-level
``EASYCAT_Exxx`` name.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Any, Literal


class EasyCatError(Exception):
    """Base exception for all EasyCat errors with a stable code.

    Carries a ``code`` (``EASYCAT_Exxx``) and a ``context`` dict that
    the CLI error handler renders with Rich. All factories in this
    module produce instances of this class.
    """

    def __init__(self, error_code: str, message: str, **context: Any) -> None:
        self.code = error_code
        self.message = message
        self.context = context
        super().__init__(self._render())

    def _render(self) -> str:
        """Render ``CODE: message`` with the registry fix + explain hint.

        Reads ``REGISTRY`` at call time (module global), so an
        ``EasyCatError`` constructed before its code is registered still
        renders correctly. The ``entry.fix.format`` call is guarded like
        the factory's headline substitution so a future braced fix
        template cannot turn into a constructor-time ``KeyError``.
        """
        base = f"{self.code}: {self.message}"
        fix = self.rendered_fix()
        if fix is None:
            return base
        return f"{base}\n  Fix: {fix}\n  Run `easycat explain {self.code}` for details."

    def rendered_fix(self) -> str | None:
        """Return this error's registry fix with context applied, if registered."""
        entry = REGISTRY.get(self.code)
        if entry is None:
            return None
        try:
            return entry.fix.format(**self.context) if self.context else entry.fix
        except (KeyError, IndexError):
            return entry.fix


class EasyConfigError(ValueError, EasyCatError):
    """Invalid application/session configuration.

    This intentionally remains a :class:`ValueError` for compatibility while
    also sharing :class:`EasyCatError`, the stable public boundary callers use
    for provider, credential, and construction failures.
    """

    def __init__(self, message: str) -> None:
        self.code = "EASYCAT_E105"
        self.message = message
        self.context: dict[str, Any] = {"problem": message}
        ValueError.__init__(self, message)


def _attach_error_code(exc: Exception, coded: EasyCatError) -> None:
    """Tag an existing public exception type with a stable EasyCat code.

    Some established APIs expose domain-specific exceptions such as
    ``BundleValidationError`` and ``FileNotFoundError``. Replacing those with
    ``EasyCatError`` would break callers, so boundary code attaches the same
    machine-readable ``code`` and ``context`` while preserving the original
    exception type and traceback.
    """
    try:
        exc.code = coded.code  # type: ignore[attr-defined]
        exc.context = coded.context  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        return
    note = f"{coded.code}: {coded.message}"
    notes = getattr(exc, "__notes__", ())
    if note not in notes:
        exc.add_note(note)


@dataclass
class ErrorEntry:
    """One entry in the error-code registry.

    ``headline`` is a :meth:`str.format` template — the raising code
    supplies context kwargs. ``cause``, ``fix``, ``example`` are
    rendered verbatim by ``easycat explain``.
    """

    code: str
    headline: str
    cause: str
    fix: str
    example: str = ""
    related: list[str] = field(default_factory=list)


REGISTRY: dict[str, ErrorEntry] = {}


ErrorFactory = Callable[..., EasyCatError]


@dataclass(frozen=True, slots=True)
class SetupIssue:
    """One coded setup failure, in the shape every surface reports.

    ``code``     — the stable ``EASYCAT_Exxx``.
    ``reason``   — a stable, content-free token: ``"missing_env"``,
                   ``"missing_extra"``, ``"unset_reference"``,
                   ``"incomplete_selection"``, ``"unresolvable_profile"``.
                   The same vocabulary ``ProviderPlan.blocking_errors()``
                   already emits.
    ``field``    — the concrete thing at fault: an env-var NAME, an install
                   extra, or a manifest path such as ``"[voice.default]"``.
                   NEVER a value.
    ``role``     — the pipeline role that needs it (``"stt"`` / ``"tts"`` /
                   ``"transport"`` / …), or ``""`` when not role-scoped.
    ``detail``   — the rendered registry headline (or a situational message).
    ``fix``      — the rendered registry fix.
    ``severity`` — ``"blocking"`` when the issue stops the SELECTED VOICE
                   PROFILE from serving one call; ``"warning"`` when it is a
                   real, reportable setup gap that does NOT stop that profile
                   from serving (a role that degrades gracefully, or a
                   requirement owned by a different surface, such as
                   ``[server] auth``).
    """

    code: str
    reason: str
    field: str = ""
    role: str = ""
    detail: str = ""
    fix: str = ""
    severity: Literal["blocking", "warning"] = "blocking"

    def as_dict(self) -> dict[str, str]:
        """``{code, reason, severity}`` plus any non-empty optional field."""
        payload: dict[str, str] = {
            "code": self.code,
            "reason": self.reason,
            "severity": self.severity,
        }
        for name in ("field", "role", "detail", "fix"):
            value: str = getattr(self, name)
            if value:
                payload[name] = value
        return payload

    @classmethod
    def from_error(
        cls,
        err: EasyCatError,
        *,
        reason: str,
        field: str = "",
        role: str = "",
        severity: Literal["blocking", "warning"] = "blocking",
    ) -> SetupIssue:
        """Project an UNRAISED :class:`EasyCatError` into an issue.

        ``detail = err.message`` and ``fix = err.rendered_fix() or ""``, so an
        issue and the exception raised for the same cause carry byte-identical
        text. :meth:`EasyCatError.rendered_fix` already guards a missing
        substitution by returning the raw template, so this never raises.

        **This constructor does NOT redact.** ``errors.py`` is a stdlib-only
        leaf and must not import ``easycat.validation.redaction``. Some coded
        errors interpolate a RAW manifest value into their message
        (``EASYCAT_E104``'s headline is one), so any issue built from a CAUGHT
        EXCEPTION that will reach a terminal or an HTTP body MUST instead be
        built through :func:`easycat.planning.selection.selection_issue`, which
        redacts ``detail`` and ``fix``. Calling ``from_error`` directly is safe
        only for an error the caller constructed itself from known-safe context
        (an env-var name, an install extra, a manifest path).
        """
        return cls(
            code=err.code,
            reason=reason,
            field=field,
            role=role,
            detail=err.message,
            fix=err.rendered_fix() or "",
            severity=severity,
        )

    @classmethod
    def from_code(
        cls,
        factory: ErrorFactory,
        *,
        reason: str,
        field: str = "",
        role: str = "",
        severity: Literal["blocking", "warning"] = "blocking",
        **context: Any,
    ) -> SetupIssue:
        """Sugar for ``from_error(factory(**context), ...)``."""
        return cls.from_error(
            factory(**context),
            reason=reason,
            field=field,
            role=role,
            severity=severity,
        )


def register(
    code: str,
    headline: str,
    *,
    cause: str,
    fix: str,
    example: str = "",
    related: list[str] | None = None,
) -> ErrorFactory:
    """Register an error code and return a factory callable.

    The returned factory accepts arbitrary kwargs which are (a) used as
    ``str.format()`` substitutions on ``headline`` when present and
    (b) attached as the ``context`` on the produced :class:`EasyCatError`.
    """
    entry = ErrorEntry(code, headline, cause, fix, example, list(related or []))
    if code in REGISTRY:
        raise RuntimeError(f"Duplicate error code registration: {code}")
    REGISTRY[code] = entry

    def factory(**ctx: Any) -> EasyCatError:
        try:
            message = headline.format(**ctx)
        except KeyError as exc:
            raise RuntimeError(
                f"{code}: headline template missing substitution for {exc}"
            ) from exc
        return EasyCatError(code, message, **ctx)

    factory.__name__ = code
    factory.__qualname__ = code
    factory.__doc__ = f"{code}: {entry.headline}"
    return factory


def get_entry(code: str) -> ErrorEntry | None:
    """Return the registered entry for ``code`` or ``None``."""
    return REGISTRY.get(code)


def all_codes() -> list[str]:
    """Return every registered code, sorted."""
    return sorted(REGISTRY)


def suggest_codes(query: str, n: int = 3) -> list[str]:
    """Return up to ``n`` registered codes similar to ``query``."""
    return get_close_matches(query.upper(), all_codes(), n=n, cutoff=0.5)


# ══════════════════════════════════════════════════════════════════
# E1xx — scaffolding
# ══════════════════════════════════════════════════════════════════

EASYCAT_E101 = register(
    "EASYCAT_E101",
    "Target {target!r} already exists and would be clobbered by scaffolding.",
    cause=(
        "`easycat init` refuses to write into an existing non-empty "
        "directory, regular file, or symlink to avoid clobbering work "
        "in progress."
    ),
    fix=(
        "Choose a new name, or remove the target first (`rm -rf "
        "<target>`). For non-empty directories only, `--force` will "
        "write into the existing directory without removing its files."
    ),
    example="easycat init my-agent --force",
    related=["EASYCAT_E102"],
)

EASYCAT_E102 = register(
    "EASYCAT_E102",
    "Invalid --config JSON: {problem}",
    cause=(
        "The --config payload is not valid JSON, is missing "
        "`schema_version`, or contains an unknown key. The init schema "
        "rejects unknown keys on purpose so coding agents (Claude Code, "
        "Cursor, Codex) get loud feedback on typos."
    ),
    fix=(
        "Run `easycat explain init-schema` for the full schema. If the "
        "problem is an unknown key, check for typos — a fuzzy "
        "suggestion is usually printed alongside this error."
    ),
    example=('easycat init demo --config \'{"schema_version": 1, "template": "openai-agents"}\''),
    related=["EASYCAT_E101"],
)

EASYCAT_E103 = register(
    "EASYCAT_E103",
    "Unknown template {template!r}. Available: {available}",
    cause="The requested template is not in the shipped template catalog.",
    fix=(
        "Run `easycat init --list-templates` to see the full list. "
        "Check spelling — the CLI accepts hyphenated names only "
        "(e.g., `openai-agents`, not `openai_agents`)."
    ),
    example="easycat init demo --template openai-agents",
    related=["EASYCAT_E102"],
)

EASYCAT_E104 = register(
    "EASYCAT_E104",
    "Unknown provider {provider!r}. Available: {available}.{hint}",
    cause=(
        "The requested provider is not registered in the STT/TTS "
        "factory. Either the name is misspelled or the provider "
        "requires an optional extra that is not installed."
    ),
    fix=(
        "Check spelling — provider names are lowercased with hyphens "
        "(`deepgram`, `openai-realtime`). Install the provider extra "
        "if needed: `uv add 'easycat[deepgram]'`. From the EasyCat "
        "repo, use `uv sync --extra deepgram --group dev`."
    ),
    example='stt="deepgram/flux"',
    related=["EASYCAT_E203"],
)

EASYCAT_E105 = register(
    "EASYCAT_E105",
    "Invalid application configuration: {problem}",
    cause=(
        "An EasyConfig, TextSessionConfig, or low-level SessionConfig value is "
        "missing a mode-required collaborator or contains an unsupported policy value."
    ),
    fix=(
        "Use EasyConfig plus create_session/run for descriptor-based setup, or provide "
        "all live collaborators when constructing SessionConfig directly."
    ),
    example="run(EasyConfig.mic(agent=my_agent))",
    related=["EASYCAT_E104", "EASYCAT_E203"],
)


# ══════════════════════════════════════════════════════════════════
# E2xx — environment (doctor checks)
# ══════════════════════════════════════════════════════════════════

EASYCAT_E201 = register(
    "EASYCAT_E201",
    "Python {found} detected — EasyCat requires Python >= 3.11.",
    cause=(
        "EasyCat uses typing features and asyncio semantics that only "
        "landed in Python 3.11 (PEP 654 ExceptionGroup, PEP 678 "
        "exception notes, TaskGroup)."
    ),
    fix=(
        "Install Python 3.11 or newer. With uv: `uv python install 3.12`. "
        "From the EasyCat repo, use `uv sync --python 3.12 --group dev`."
    ),
    example="uv python install 3.12  # repo: uv sync --python 3.12 --group dev",
    related=[],
)

EASYCAT_E202 = register(
    "EASYCAT_E202",
    "Missing required extra: {extra}",
    cause=(
        "The agent or template needs a Python package that is in one "
        "of EasyCat's optional extras, but that extra is not installed."
    ),
    fix=(
        "Install the extra: `uv add 'easycat[{extra}]'`. From the "
        "EasyCat repo, use `uv sync --extra {extra} --group dev`."
    ),
    example="uv add 'easycat[openai-agents]'  # or: uv sync --extra openai-agents --group dev",
    related=["EASYCAT_E203", "EASYCAT_E602"],
)

EASYCAT_E203 = register(
    "EASYCAT_E203",
    "Missing API key: {var}",
    cause=(
        "The provider you selected needs an API key in an environment "
        "variable, but the variable is unset, empty, or still contains an "
        "obvious example placeholder."
    ),
    fix=(
        "Set the env var: `export {var}=...`. If the project uses a "
        "`.env` file, copy `.env.example` to `.env`, fill in keys there, "
        "and verify it with `easycat doctor --env-file .env`."
    ),
    example="export OPENAI_API_KEY=sk-...  # or: easycat doctor --env-file .env",
    related=["EASYCAT_E202"],
)

EASYCAT_E204 = register(
    "EASYCAT_E204",
    "Provider {provider!r} unreachable: {detail}",
    cause=(
        "`easycat doctor` sent an unauthenticated HEAD probe to the provider's "
        "API endpoint and received no response. This only checks network/DNS "
        "liveness; it does not validate the configured credential."
    ),
    fix=(
        "Check internet connectivity and DNS, then re-run. If the host still "
        "cannot be reached, check the provider's status page. Validate credentials "
        "with an explicitly selected live/provider workflow."
    ),
    example="easycat doctor --provider openai",
    related=["EASYCAT_E203"],
)

EASYCAT_E205 = register(
    "EASYCAT_E205",
    "onnxruntime is not importable (smart-turn extra requested).",
    cause=(
        "Smart Turn endpoint detection needs `onnxruntime`, which "
        "ships in the `smart-turn` extra but is not currently "
        "installed in this environment."
    ),
    fix=(
        "Install Smart Turn support: `uv add 'easycat[smart-turn]'`. "
        "From the EasyCat repo, use `uv sync --extra smart-turn --group dev`."
    ),
    example="uv add 'easycat[smart-turn]'  # or: uv sync --extra smart-turn --group dev",
    related=["EASYCAT_E202"],
)

EASYCAT_E206 = register(
    "EASYCAT_E206",
    "No default microphone device detected.",
    cause=(
        "`easycat doctor` queried `sounddevice` for the default input "
        "device and none was present. On macOS this usually means the "
        "terminal application has not been granted microphone access."
    ),
    fix=(
        "On macOS: System Settings → Privacy & Security → Microphone, "
        "grant access to your terminal. On Linux: check PulseAudio or "
        "PipeWire is running. On Windows: check Sound settings."
    ),
    example="",
    related=[],
)

EASYCAT_E207 = register(
    "EASYCAT_E207",
    "Journal directory is not writable: {path}",
    cause=(
        "EasyCat writes crash-durable session journals to "
        "`.easycat/journals/` by default. That directory is "
        "either missing, read-only, or on a filesystem that does not "
        "support SQLite WAL mode. Set `EASYCAT_DATA_DIR` to move the "
        "journal root."
    ),
    fix="mkdir -p .easycat/journals && chmod u+w .easycat/journals",
    example="",
    related=[],
)

EASYCAT_E208 = register(
    "EASYCAT_E208",
    "Low disk space at {path}: {free_mb}MB free (need >= 500MB).",
    cause=(
        "Journals and bundles can grow to tens of megabytes per "
        "session; a machine running low on disk will silently fail to "
        "persist recordings."
    ),
    fix="Free up disk space or point EasyCat at a larger filesystem with EASYCAT_DATA_DIR.",
    example="",
    related=["EASYCAT_E207"],
)

EASYCAT_E209 = register(
    "EASYCAT_E209",
    "PortAudio runtime library is unavailable.",
    cause=(
        "The `sounddevice` Python package is installed, but it could not load "
        "the native PortAudio library required by local microphone and speaker I/O."
    ),
    fix=(
        "Install PortAudio first (Debian/Ubuntu: `sudo apt-get install libportaudio2`; "
        "macOS: `brew install portaudio`), then retry."
    ),
    example="sudo apt-get install libportaudio2  # macOS: brew install portaudio",
    related=["EASYCAT_E202", "EASYCAT_E206"],
)

EASYCAT_E210 = register(
    "EASYCAT_E210",
    "Required project environment variable is missing or invalid: {var}",
    cause=(
        "The selected EasyCat project (its `[tool.easycat.scaffold]` table or its "
        "`easycat.toml` profile) declares this environment variable as a startup "
        "requirement, but doctor found it unset, still set to an example "
        "placeholder, or invalid for its declared use."
    ),
    fix=(
        "Copy `.env.example` to `.env`, replace every required placeholder with "
        "the real project value, and rerun `easycat doctor --env-file .env` "
        "(or `easycat doctor --manifest easycat.toml --env-file .env`)."
    ),
    example="easycat doctor --env-file .env",
    related=["EASYCAT_E203", "EASYCAT_E202"],
)


# ══════════════════════════════════════════════════════════════════
# E3xx — runtime (session execution)
# ══════════════════════════════════════════════════════════════════

EASYCAT_E301 = register(
    "EASYCAT_E301",
    "STT provider {provider!r} timed out after {timeout:.1f}s.",
    cause=(
        "The speech-to-text provider did not produce a transcript "
        "within the configured `stt_timeout`. The provider may be slow, "
        "unreachable, or the audio stream may have stalled."
    ),
    fix=(
        "Increase `TimeoutConfig.stt_timeout` if the provider is simply "
        "slow, or check network connectivity to the STT provider. "
        "Inspect the session journal Error record (tagged with this "
        "code) for the failing turn."
    ),
    example="TimeoutConfig(stt_timeout=20.0)",
    related=["EASYCAT_E204"],
)

EASYCAT_E302 = register(
    "EASYCAT_E302",
    "Agent timed out after {timeout:.1f}s.",
    cause=(
        "The agent did not return a response within the configured "
        "`agent_timeout`. A tool call, model call, or downstream service "
        "is likely hanging."
    ),
    fix=(
        "Increase `TimeoutConfig.agent_timeout` for long-running agents, "
        "or add per-tool timeouts inside the agent. Inspect the session "
        "journal Error record (tagged with this code) for the turn that "
        "stalled."
    ),
    example="TimeoutConfig(agent_timeout=60.0)",
    related=["EASYCAT_E301", "EASYCAT_E303"],
)

EASYCAT_E303 = register(
    "EASYCAT_E303",
    "TTS provider {provider!r} timed out after {timeout:.1f}s.",
    cause=(
        "The text-to-speech provider did not produce its first audio "
        "frame within the configured `tts_first_byte_timeout`. The "
        "provider may be slow, unreachable, or rejected the request."
    ),
    fix=(
        "Increase `TimeoutConfig.tts_first_byte_timeout` if the provider "
        "is slow to start, or check network connectivity to the TTS "
        "provider. Inspect the session journal Error record (tagged "
        "with this code)."
    ),
    example="TimeoutConfig(tts_first_byte_timeout=8.0)",
    related=["EASYCAT_E302"],
)

EASYCAT_E304 = register(
    "EASYCAT_E304",
    "Provider {provider!r} became unreachable mid-call: {detail}",
    cause=(
        "A live provider connection dropped during an active session "
        "(network blip, server-side disconnect, or the provider closed "
        "the stream). Unlike `easycat doctor` probes, this happens "
        "while audio is flowing."
    ),
    fix=(
        "EasyCat will attempt to reconnect automatically; persistent "
        "failures surface as EASYCAT_E305. Check network stability and "
        "the provider's status page. The session journal Error record "
        "carries this code for correlation."
    ),
    example="",
    related=["EASYCAT_E204", "EASYCAT_E305"],
)

EASYCAT_E305 = register(
    "EASYCAT_E305",
    "Provider {provider!r} exhausted {reason} after {attempts} attempt(s).",
    cause=(
        "EasyCat either exhausted the failed-attempt retry limit or the "
        "successful reconnect-cycle budget for a dropped provider connection. "
        "The session can no longer reach the provider."
    ),
    fix=(
        "Check sustained network connectivity and the provider's status "
        "page, then restart the session. Raise the reconnect attempt "
        "limit only if the outage is expected to be transient."
    ),
    example="",
    related=["EASYCAT_E304"],
)


# ══════════════════════════════════════════════════════════════════
# E4xx — bundle / replay
# ══════════════════════════════════════════════════════════════════

EASYCAT_E401 = register(
    "EASYCAT_E401",
    "Failed to write debug bundle to {path}: {detail}",
    cause=(
        "Serializing the session run bundle to disk failed — usually a "
        "read-only path, a full disk, or a permissions problem."
    ),
    fix=(
        "Verify the target directory is writable and has free space "
        "(see EASYCAT_E207 / EASYCAT_E208), then re-export the bundle."
    ),
    example="session.export_debug_bundle('/tmp/run.zip')",
    related=["EASYCAT_E207", "EASYCAT_E208", "EASYCAT_E402"],
)

EASYCAT_E402 = register(
    "EASYCAT_E402",
    "Failed to load debug bundle from {path}: {detail}",
    cause=(
        "The bundle could not be read or parsed — the file is missing, "
        "truncated, not a valid EasyCat bundle, or was produced by an "
        "incompatible schema version."
    ),
    fix=(
        "Confirm the path points at a complete bundle produced by a "
        "compatible EasyCat version. Re-export from the source session "
        "if the file is corrupt."
    ),
    example="load_bundle('/tmp/run.zip')",
    related=["EASYCAT_E401", "EASYCAT_E403"],
)

EASYCAT_E403 = register(
    "EASYCAT_E403",
    "Replay diverged from recorded bundle: {detail}",
    cause=(
        "Replaying a recorded bundle produced output that no longer "
        "matches the recording — pipeline behavior changed, or the "
        "bundle was recorded with a different configuration."
    ),
    fix=(
        "Inspect the divergence detail and the bundle's recorded config. "
        "If the change is intentional, re-record the bundle; otherwise "
        "treat the divergence as a regression."
    ),
    example="",
    related=["EASYCAT_E402"],
)

EASYCAT_E404 = register(
    "EASYCAT_E404",
    "Not an EasyCat journal: {path}",
    cause=(
        "The file is not a SQLite database, or it is a SQLite database "
        "with no `journal` table. `easycat tail` retries a missing table "
        "briefly — a live session creates it moments after the file "
        "appears — then reports the target as unusable rather than "
        "waiting forever."
    ),
    fix=(
        "Point at a `.sqlite` journal written by a session with "
        '`debug="light"` or `debug="full"` (by default under '
        "`.easycat/journals/`). Use `easycat bundles show <path>` for an "
        "exported ZIP bundle."
    ),
    example="easycat tail .easycat/journals/session-abc123.sqlite",
    related=["EASYCAT_E207", "EASYCAT_E402"],
)


# ══════════════════════════════════════════════════════════════════
# E5xx — CLI usage
# ══════════════════════════════════════════════════════════════════

EASYCAT_E501 = register(
    "EASYCAT_E501",
    "Unknown error code {code!r}.",
    cause="`easycat explain` could not find this code in the registry.",
    fix=(
        "Run `easycat explain --list` to see every registered code. "
        "Common codes: E101 (init target exists), E203 (missing API "
        "key), E204 (provider unreachable)."
    ),
    example="easycat explain --list",
    related=[],
)


# ══════════════════════════════════════════════════════════════════
# E6xx — project manifest (easycat.toml)
# ══════════════════════════════════════════════════════════════════

EASYCAT_E601 = register(
    "EASYCAT_E601",
    "Manifest file not found: {path}",
    cause=(
        "`easycat serve --manifest` (or `VoiceServer.from_manifest`) could "
        "not find an `easycat.toml`. The loader looks at `--manifest`, then "
        "`EASYCAT_MANIFEST`, then `easycat.toml` in the working directory."
    ),
    fix=(
        "Create an `easycat.toml`, pass `--manifest path/to/easycat.toml`, "
        "or set `EASYCAT_MANIFEST`."
    ),
    example="easycat serve --manifest easycat.toml",
    related=["EASYCAT_E602"],
)

EASYCAT_E602 = register(
    "EASYCAT_E602",
    "Invalid manifest {path}: {problem}",
    cause=(
        "The manifest is not valid TOML, is missing a required table/field, "
        "uses an unknown profile, or names an unknown transport. The schema "
        "rejects unknown keys so typos fail loudly."
    ),
    fix=(
        "Check the manifest against `docs/deployment/production-servers.md`. "
        "Each `[voice.<profile>]` needs a known `transport` "
        "(`webrtc`/`websocket`/`twilio`/`telnyx`/`local`)."
    ),
    example='[voice.default]\ntransport = "webrtc"',
    related=["EASYCAT_E601", "EASYCAT_E603", "EASYCAT_E203", "EASYCAT_E202"],
)

EASYCAT_E603 = register(
    "EASYCAT_E603",
    "Manifest field {field!r} must be an env reference (bearer-env:NAME), not a literal secret.",
    cause=(
        "A `auth`/`token` field carried a literal-looking secret. Secrets "
        "must never be committed to `easycat.toml`; the loader requires the "
        "`bearer-env:NAME` grammar and resolves the value from the "
        "environment at load time so a token never appears in the manifest, "
        "logs, or `--json`/`/manifest` dumps."
    ),
    fix=(
        "Replace the literal with an env reference, e.g. "
        '`auth = "bearer-env:EASYCAT_SERVE_TOKEN"`, and export the secret '
        "in the environment (`export EASYCAT_SERVE_TOKEN=...`)."
    ),
    example='auth = "bearer-env:EASYCAT_SERVE_TOKEN"',
    related=["EASYCAT_E602"],
)

EASYCAT_E604 = register(
    "EASYCAT_E604",
    "Manifest env reference {reference!r} points at an unset variable {var!r}.",
    cause=(
        "An `auth`/`token` field used the `bearer-env:NAME` grammar, but the "
        "named environment variable is unset or empty at load time."
    ),
    fix=(
        "Export the variable before serving (`export {var}=...`), or use a "
        "`.env` file and verify with `easycat doctor --env-file .env`."
    ),
    example="export EASYCAT_SERVE_TOKEN=...",
    related=["EASYCAT_E603"],
)

EASYCAT_E605 = register(
    "EASYCAT_E605",
    "Manifest agent reference {reference!r} could not be resolved: {detail}",
    cause=(
        "A `[voice.<profile>] agent` used the `python:module:function` "
        "grammar, but the module or attribute could not be imported, or it "
        "was not callable. The resolver imports lazily so a typo or a missing "
        "extra surfaces here."
    ),
    fix=(
        "Check the dotted path resolves (`python -c 'import module'`), that "
        "the attribute exists and is callable, and that any provider extra "
        "the agent needs is installed."
    ),
    example='agent = "python:app:create_agent"',
    related=["EASYCAT_E602"],
)


__all__ = [
    "EASYCAT_E101",
    "EASYCAT_E102",
    "EASYCAT_E103",
    "EASYCAT_E104",
    "EASYCAT_E105",
    "EASYCAT_E201",
    "EASYCAT_E202",
    "EASYCAT_E203",
    "EASYCAT_E204",
    "EASYCAT_E205",
    "EASYCAT_E206",
    "EASYCAT_E207",
    "EASYCAT_E208",
    "EASYCAT_E209",
    "EASYCAT_E210",
    "EASYCAT_E301",
    "EASYCAT_E302",
    "EASYCAT_E303",
    "EASYCAT_E304",
    "EASYCAT_E305",
    "EASYCAT_E401",
    "EASYCAT_E402",
    "EASYCAT_E403",
    "EASYCAT_E404",
    "EASYCAT_E501",
    "EASYCAT_E601",
    "EASYCAT_E602",
    "EASYCAT_E603",
    "EASYCAT_E604",
    "EASYCAT_E605",
    "REGISTRY",
    "EasyCatError",
    "EasyConfigError",
    "ErrorEntry",
    "ErrorFactory",
    "SetupIssue",
    "all_codes",
    "get_entry",
    "register",
    "suggest_codes",
]
