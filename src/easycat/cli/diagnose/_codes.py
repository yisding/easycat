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
  5  - Bundle missing, corrupt, or too new for this EasyCat
  6  - Replay failed or side effects were blocked
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
      "template": "openai-agents" | "pydantic-ai" |
                  "pydantic-ai-workflow" | "text-chat" |
                  "twilio-phone" | "webrtc-browser",
      "stt": "<provider>/<model>",            // optional
      "tts": "<provider>/<model>",            // optional
      "llm": "string",                        // reserved; currently rejected
      "transport": "local" | "webrtc" | "twilio", // optional
      "agent_name": "string",                 // optional
      "agent_instructions": "string",         // optional
      "tools": ["tool-name", ...],            // reserved; currently rejected
      "mcp_servers": ["stdio://...", ...]     // optional MCP URIs
    }

Required keys: `schema_version`, `template`.  Unknown keys are
rejected on purpose so coding agents get loud feedback on typos.
`transport` must match the selected template when provided.
This release scaffolds voice-provider shortcuts for the voice
templates, local microphone, browser WebRTC, and Twilio phone transports
through separate templates, and MCP server URIs starting with
`stdio://`, `sse://`, `http://`, or `https://`. Plain MCP names such as
`"filesystem"` are rejected.
Reserved keys `llm` and `tools` are accepted by schema_version 1 so
callers get a stable EASYCAT_E102 explanation; this release does not
wire them into templates yet. Add LLM or tool setup directly in the
generated `agent.py` for now.
Run `easycat init --list-templates --json` for the current
machine-readable template catalog.
Bump `schema_version` when the shape changes; keep older schemas
documented before accepting a newer version.
"""

_JSON_SCHEMA_BODY = """\
Commands that expose `--json` emit a versioned envelope:

    {
      "schema_version": 1,
      "command": "<name>",
      "status": "ok" | "error",
      ...
    }

On EasyCat errors, the envelope includes `code` (EASYCAT_Exxx),
`message`, `fix`, `context`, and `exit_code`.  Other command-specific
errors still include `message` and `exit_code` without inventing a fake
EASYCAT_Exxx code.

When an error is about a file or directory, commands include the
relevant path field when it helps automation recover:

  `report_path` - `easycat validate report PATH --json`
  `path`        - `easycat bundles show PATH --json` and `easycat replay PATH --json`
  `output_path` - `easycat bundles export PATH --output DIR --json`

Stdout carries the envelope; stderr carries logs and diagnostics so
`2>/dev/null` remains safe.  Automation should branch on `command`,
`status`, and `exit_code`, then inspect command-specific fields only for
the command it invoked.

`schema_version` bumps on breaking changes; keep older envelope schemas
documented before accepting a newer version.
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
