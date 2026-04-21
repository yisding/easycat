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

Adding a code is a one-file change: call :func:`register` at module
load time and (optionally) bind the returned factory to a module-level
``EASYCAT_Exxx`` name.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Any


class EasyCatError(Exception):
    """Base exception for all EasyCat errors with a stable code.

    Carries a ``code`` (``EASYCAT_Exxx``) and a ``context`` dict that
    the CLI error handler renders with Rich. All factories in this
    module produce instances of this class.
    """

    def __init__(self, code: str, message: str, **context: Any) -> None:
        self.code = code
        self.message = message
        self.context = context
        super().__init__(f"{code}: {message}")


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
        "if needed: `uv add 'easycat[deepgram]'`."
    ),
    example='stt="deepgram/flux"',
    related=["EASYCAT_E203"],
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
        "Install Python 3.11 or newer. With uv: `uv python install 3.12 && uv sync --python 3.12`."
    ),
    example="uv python install 3.12",
    related=[],
)

EASYCAT_E202 = register(
    "EASYCAT_E202",
    "Missing required extra: {extra}",
    cause=(
        "The agent or template needs a Python package that is in one "
        "of EasyCat's optional extras, but that extra is not installed."
    ),
    fix="Install the extra: `uv add 'easycat[{extra}]'`.",
    example="uv add 'easycat[openai-agents]'",
    related=["EASYCAT_E203"],
)

EASYCAT_E203 = register(
    "EASYCAT_E203",
    "Missing API key: {var}",
    cause=(
        "The provider you selected needs an API key in an environment "
        "variable, but the variable is unset or empty."
    ),
    fix=(
        "Set the env var: `export {var}=...`. If the project uses a "
        "`.env` file, copy `.env.example` to `.env` and fill in keys "
        "there — `python-dotenv` or the scaffolded templates will "
        "load it automatically."
    ),
    example="export OPENAI_API_KEY=sk-...",
    related=["EASYCAT_E202"],
)

EASYCAT_E204 = register(
    "EASYCAT_E204",
    "Provider {provider!r} unreachable: {detail}",
    cause=(
        "`easycat doctor` sent a 200ms HEAD probe to the provider's "
        "API endpoint and it failed. The issue is either network, DNS, "
        "a bad API key, or a regional outage."
    ),
    fix=(
        "Check internet connectivity, verify the API key, and re-run. "
        "If the key is correct but the host still fails, check the "
        "provider's status page."
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
    fix="uv add 'easycat[smart-turn]'",
    example="uv add 'easycat[smart-turn]'",
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
        "`~/.cache/easycat/journals/` by default. That directory is "
        "either missing, read-only, or on a filesystem that does not "
        "support SQLite WAL mode."
    ),
    fix="mkdir -p ~/.cache/easycat/journals && chmod u+w ~/.cache/easycat/journals",
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
    fix="Free up disk space or point the cache elsewhere with XDG_CACHE_HOME.",
    example="",
    related=["EASYCAT_E207"],
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


__all__ = [
    "EasyCatError",
    "ErrorEntry",
    "ErrorFactory",
    "REGISTRY",
    "register",
    "get_entry",
    "all_codes",
    "suggest_codes",
    "EASYCAT_E101",
    "EASYCAT_E102",
    "EASYCAT_E103",
    "EASYCAT_E104",
    "EASYCAT_E201",
    "EASYCAT_E202",
    "EASYCAT_E203",
    "EASYCAT_E204",
    "EASYCAT_E205",
    "EASYCAT_E206",
    "EASYCAT_E207",
    "EASYCAT_E208",
    "EASYCAT_E501",
]
