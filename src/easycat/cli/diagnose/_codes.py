"""Error-code registry for ``easycat explain``.

This module is a read-only re-export of :mod:`easycat.errors`.  The
registry lives at library scope (not CLI-scope) because library code
raises ``EasyCatError`` subclasses and its own callers need the error
documentation, not just the CLI.

Meta-entries — ``exit-codes``, ``init-schema``, ``json-schema`` — are
also exposed here so the ``explain`` command can render them
alongside the per-error docs.
"""

from __future__ import annotations

from dataclasses import dataclass

from easycat.errors import REGISTRY, ErrorEntry

__all__ = [
    "REGISTRY",
    "ErrorEntry",
    "META_ENTRIES",
    "MetaEntry",
]


@dataclass
class MetaEntry:
    """A non-error topic that ``easycat explain`` can render.

    Used for the exit-code contract, the --config schema, and the JSON
    output schema — each of them has one canonical place to live, and
    this is it.
    """

    slug: str
    headline: str
    body: str


_EXIT_CODES_BODY = """\
Exit codes form a stable contract.  Scripts can branch on them without
parsing CLI output.

  0  - Success
  1  - Runtime error (agent crashed, provider failed, etc.)
  2  - Bad usage (unknown flag, missing argument, unknown template)
  3  - Missing credentials
  4  - Missing optional extra, or bad --config JSON
  5  - Bundle missing or corrupt
  6  - Regression detected (`replay --fail-on-regression`)
  101 - Target directory exists (`init` without `--force`)
  130 - SIGINT hard exit (second Ctrl-C)

Codes map one-to-one with EASYCAT_Exxx error categories.  See
`easycat explain --list` for the full catalog.
"""

_INIT_SCHEMA_BODY = """\
`easycat init --config` accepts a JSON payload with this shape
(schema_version 1):

    {
      "schema_version": 1,
      "template": "openai-agents" | "pydantic-ai" | "text-chat",
      "stt": "<provider>/<model>",            // optional
      "tts": "<provider>/<model>",            // optional
      "llm": "<provider>/<model>",            // optional, template-specific
      "transport": "local" | "webrtc" | "telephony",
      "agent_name": "string",                 // optional
      "agent_instructions": "string",         // optional
      "mcp_servers": ["filesystem", ...]      // optional, curated list
    }

Required keys: `schema_version`, `template`.  Unknown keys are
rejected on purpose so coding agents get loud feedback on typos.
Bump `schema_version` when the shape changes; old versions stay
documented via `easycat explain init-schema --version N` in future
releases.
"""

_JSON_SCHEMA_BODY = """\
Every command accepts `--json` and emits a versioned envelope:

    {
      "schema_version": 1,
      "command": "<name>",
      "status": "ok" | "error",
      ...
    }

On error, the envelope includes `code` (EASYCAT_Exxx), `message`,
`context`, and `exit_code`.  Stdout carries the envelope; stderr
carries logs and diagnostics so `2>/dev/null` remains safe.

`schema_version` bumps on breaking changes; old versions stay
documented under `easycat explain json-schema --version N` in future
releases.
"""


META_ENTRIES: dict[str, MetaEntry] = {
    "exit-codes": MetaEntry(
        slug="exit-codes",
        headline="CLI exit-code contract",
        body=_EXIT_CODES_BODY,
    ),
    "init-schema": MetaEntry(
        slug="init-schema",
        headline="`easycat init --config` JSON schema",
        body=_INIT_SCHEMA_BODY,
    ),
    "json-schema": MetaEntry(
        slug="json-schema",
        headline="CLI `--json` output envelope schema",
        body=_JSON_SCHEMA_BODY,
    ),
}
