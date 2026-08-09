"""Error-code registry for ``easycat explain``.

This module is a read-only re-export of :mod:`easycat.errors`.  The
registry lives at library scope (not CLI-scope) because library code
raises ``EasyCatError`` subclasses and its own callers need the error
documentation, not just the CLI.

Meta-entries — ``exit-codes``, ``init-schema``, ``json-schema`` — are
also exposed here so the ``explain`` command can render them
alongside the per-error docs, together with the concept topics
``events``, ``turn-taking``, ``barge-in``, and ``journal`` that
summarize a docs page and print its route, plus the symptom-first
``troubleshooting`` router that maps a symptom to the command, doc, and
topic that diagnose it.
"""

from __future__ import annotations

from dataclasses import dataclass

from easycat.errors import REGISTRY, ErrorEntry

__all__ = [
    "META_ENTRIES",
    "REGISTRY",
    "ErrorEntry",
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
      "template": "openai-agents" | "provider" | "provider-stt" |
                  "provider-tts" |
                  "pydantic-ai" | "pydantic-ai-workflow" |
                  "text-chat" | "twilio-phone" | "webrtc-browser",
      "stt": "<provider>/<model>",            // optional
      "tts": "<provider>/<model>",            // optional
      "llm": "string",                        // reserved; currently rejected
      "transport": "local" | "webrtc" | "twilio", // optional
      "agent_name": "string",                 // optional
      "agent_instructions": "string",         // optional
      "tools": ["tool-name", ...],            // reserved; currently rejected
      "mcp_servers": ["stdio://...", ...],    // optional MCP URIs
      "easycat_source": "path/to/easycat",    // optional local checkout path
      "easycat_git": "https://host/org/easycat.git", // optional portable source
      "easycat_git_rev": "commit-or-tag"       // optional Git revision
    }

Required keys: `schema_version`, `template`.  Unknown keys are
rejected on purpose so coding agents get loud feedback on typos.
`transport` must match the selected template when provided.
This release scaffolds voice-provider shortcuts for the voice
templates, local microphone, browser WebRTC, and Twilio phone transports
through separate templates, and MCP server URIs starting with
`stdio://`, `sse://`, `http://`, or `https://`. Plain MCP names such as
`"filesystem"` are rejected.
`easycat_source` / `--easycat-source` points the generated
`pyproject.toml` at a local editable checkout. `easycat_git` /
`--easycat-git` writes a portable Git-backed `[tool.uv.sources]` entry;
pin it with `easycat_git_rev` / `--easycat-git-rev` for reproducible CI.
Local and Git sources are mutually exclusive. When both source kinds are
omitted, editable/repo installs retain local-path auto-detection; published
installs render no block. Git URLs must not embed credentials.
Reserved keys `llm` and `tools` are accepted by schema_version 1 so
callers get a stable EASYCAT_E102 explanation; this release does not
wire them into templates yet. Add LLM or tool setup directly in the
generated `agent.py` for now.
Run `easycat init --list-templates --json` for the current
machine-readable template catalog.  The top-level `command_note`
explains installed vs repo-local creation and post-scaffold command
context. Each `catalog` row includes:
`name`, `mode`, `transport`, `framework`, `best_for`, `base_extras`,
`base_requirement`, `required_env`, `optional_env`, `files`, `description`,
`create_command` (installed CLI form), `repo_create_command`
(repo-local `uv run` form from the repository root), `next_step_commands`
(a `my-agent` preview sequence), `run_command`, `check_command`, and
`fix_command`.
Successful `easycat init NAME --json` also includes
`easycat_source`, `easycat_git`, and `easycat_git_rev` so automation can
verify the rendered dependency source, plus `next_step_commands`, an ordered
copy/sync/doctor/check/fix/docs/audience-docs/docs-json/json-schema/run
normal-path sequence from the human success footer, plus `fix_command` for
Ruff-fixable lint findings.
Bump `schema_version` when the accepted `--config` input shape changes;
keep older input schemas documented before accepting a newer version.
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

Successful commands may add command-specific fields. Common automation
entry points include:

  `entries`, `source_url`, `command_note`, `audience_filter`,
                            `available_audiences`,
                            `available_audience_filters`,
                            `audience_alias_note` - `easycat docs --json`
                            and `easycat docs --audience learners --json`;
                            each docs entry has `label`, `path`, `audience`,
                            `description`, `url`, and optional `commands`
                            in onboarding order, plus a `diataxis` label
                            (tutorial, how-to, reference, or explanation);
                            `audience`
                            labels the intended reader, and `command_note`
                            explains bare installed CLI hints, repo-local
                            `uv run` hints, and uppercase placeholders such
                            as PATH; `audience_alias_note` explains that
                            multi-word audience filters accept hyphens or
                            underscores and that the maintainers/operators
                            filters include compound labels such as provider
                            maintainers, release maintainers, and operators
                            and maintainers; `available_audience_filters`
                            lists the copyable filter tokens; `diataxis`
                            labels each entry as tutorial, how-to,
                            reference, or explanation
  `templates`, `catalog`, `command_note` -
                         `easycat init --list-templates --json`;
                         catalog entries include `name`, `mode`, `transport`,
                         `framework`, `best_for`, `description`,
                         `base_extras`, `base_requirement`, `required_env`,
                         `optional_env`, `files`, `create_command`,
                         `repo_create_command`, `next_step_commands`,
                         `run_command`, `check_command`, and `fix_command`;
                         `command_note` explains installed creation, repo-root
                         creation, and post-scaffold command context
  `path`, `template`, `pyproject_name`, `files`, `agent_lines`, `git`,
  `easycat_source`, `easycat_git`, `easycat_git_rev`,
  `run_command`, `check_command`, `fix_command`, `next_step_commands`,
  `command_note` -
                         `easycat init NAME --json`
  `environment`, `checks` - `easycat doctor --json`; each check row has
                         `name`, `status`, and `detail`, and may include
                         `code` and `fix` when the check fails
  `validation`, `report_path`, `exit_code` -
                         `easycat validate quick --json`,
                         `easycat validate contracts --json`,
                         `easycat validate release --json`, and
                         `easycat validate report PATH --json`; `validation`
                         contains the redacted validation report object
  `bundles`, `scanned` - `easycat bundles list --json`
  `path`, `session_id`, `turn_count`, `turns`, `issues`, `errors`,
  `error_type`, `failing_turn_id`, `tool_calls`, `records`, `duration_ms`,
  `annotations`, `provider_versions`, `journal_dropped_records`, `artifact_count`,
  `replay_entry_points`, `format_version` -
                         `easycat bundles show PATH --json` and
                         `easycat inspect PATH --json`; `turns` is the
                         per-turn latency waterfall — each entry has
                         `turn_id`, `wall_ms`, per-stage `spans`
                         (`stage`, `offset_ms`, `duration_ms`,
                         `record_count`), and `milestones` deltas (VAD
                         endpoint → STT final → agent first token →
                         TTS first byte); see docs/latency.md.
                         `issues` is the severity-ranked rollup
                         (`easycat inspect PATH --issues`); `error_type`
                         and `failing_turn_id` name the first failure;
                         `annotations` tallies reviewer verdicts from the
                         `<bundle>.annotations.json` sidecar
  `source_path`, `output_path`, `target`, `files`, `records`, `artifacts`,
  `format_version`, `summary`, `redaction` -
                         `easycat bundles export PATH --output DIR --json`
  `path`, `fidelity_requested`, `fidelity_effective`, `frames`, `stages`,
  `stage_replays`, `side_effecting`, `tool_policy`, `allowed_tool_calls`,
  `executed_tool_calls`, `blocked_tool_calls`, `stubbed_tool_calls`,
  `from_sequence`, `to_sequence`, `stage_filter`, `force`, `timing` -
                         `easycat replay PATH --json`

When an error is about a file or directory, commands include the
relevant path field when it helps automation recover:

  `report_path` - `easycat validate report .easycat/validation/latest.json --json`
  `path`        - `easycat bundles show PATH --json` and `easycat replay PATH --json`
  `output_path` - `easycat bundles export PATH --output DIR --json`

`easycat console` is interactive-only and exempt from this envelope: it
never accepts `--json`.  It always ends by printing the exported debug
bundle path; automation should replay that bundle instead with
`easycat replay PATH --json`.

Stdout carries the envelope; stderr carries logs and diagnostics so
`2>/dev/null` remains safe.  Automation should branch on `command`,
`status`, and `exit_code`, then inspect command-specific fields only for
the command it invoked.

`schema_version` bumps on breaking changes; keep older envelope schemas
documented before accepting a newer version.
"""


_EVENTS_BODY = """\
EasyCat has two event layers, and only one is meant for your code.

EasyCat-level events are emitted on the session EventBus: audio
(`AudioIn`/`AudioOut`), VAD (`VADStartSpeaking`/`VADStopSpeaking`), STT
(`STTPartial`/`STTFinal`), agent (`AgentDelta`/`AgentFinal`), TTS
(`TTSAudio`/`TTSMarkers`), turn lifecycle (`TurnStarted`, `TurnEnded`,
`BotStartedSpeaking`, `BotStoppedSpeaking`, `Interruption`), telephony
(`CallAnswered`, `CallEnded`, `CallFailed`), supervisor taps, and
`Error`. Subscribe with `session.subscribe_event(STTFinal, handler)`;
every event carries `session_id` / `turn_id` correlation fields.

Provider-scoped events (`STTEvent`, `TTSEvent`) are produced by STT/TTS
provider iterators and are internal: the Session maps them to the
EasyCat-level events above, so application code never consumes them
directly.

The EventBus drives behavior — it is not an observability sink. Use the
journal (`easycat explain journal`) for durable records.

Docs route: docs/reference/events.md (`easycat docs --audience
app-builders`, label "Events reference").
"""

_TURN_TAKING_BODY = """\
Turn-taking is a 5-state finite state machine in `turn_manager.py`:

  IDLE -> USER_SPEAKING -> USER_PAUSED -> PROCESSING -> BOT_SPEAKING

In VAD mode (default) the voice-activity detector opens a turn when the
user starts speaking, with pre-roll buffering so the first syllables are
not lost; a silence timeout (or the optional smart-turn ONNX endpoint
classifier) decides when the user is done. PUSH_TO_TALK mode hands that
control to the application. If the user speaks while the bot is playing
audio, the turn manager raises an interruption (barge-in): pending TTS
is cancelled cooperatively via CancelToken and the journal records what
the user actually heard.

Tune it with `EasyConfig(turn_taking=TurnManagerConfig(...))` and the direct
`smart_turn` / `smart_turn_sensitivity` fields.

Docs route: docs/architecture.md (`easycat docs --audience maintainers`,
label "Architecture"); hands-on chapters: docs/teaching/04-vad-preroll/,
docs/teaching/08-smart-turn/, docs/teaching/09-interruption/.
"""

_JOURNAL_BODY = """\
The execution journal is EasyCat's single source of truth for
observability. With `EasyConfig(debug="light")` or `debug="full"` every
session records events, spans, and metrics (and, in full mode, audio
artifacts) into an SQLite-backed journal under `.easycat/journals/`.

It is debug-first: `session.journal.read()` works during the session and
keeps working after a clean `session.stop()` through a preserved
read-only postmortem view. Export a replayable bundle with
`session.export_debug_bundle(path)` or automatically on every stop via
`EasyConfig(record_to=...)`, then inspect it with `easycat bundles show
PATH`, `easycat inspect PATH`, `easycat replay PATH`, or the debugger
UI.

Record catalog: docs/reference/journal-records.md; lifecycle and postmortem
access: docs/reference/session-lifecycle.md (`easycat docs --audience
app-builders`); operator tooling: docs/observability.md.
"""

_TROUBLESHOOTING_BODY = """\
Something wrong with a call? Route by symptom. Pick the line that
matches what you observed, run the command on a captured bundle or
journal PATH, then read the topic for the why.

didnt-hear-me — the bot ignored what the caller said.
  Run:   easycat inspect PATH      (filter for `stt_final` records)
  Read:  docs/latency.md
  Topic: easycat explain events

cut-me-off — the bot talked over the caller (barge-in misfired).
  Run:   easycat explain barge-in
  Read:  docs/teaching/09-interruption/
  Topic: easycat explain turn-taking

too-slow — the bot took too long to respond.
  Run:   easycat latency PATH
         easycat bundles show PATH   (per-turn latency waterfall)
  Read:  docs/latency.md
  Topic: easycat explain turn-taking

said-wrong — the bot answered, but the answer was wrong.
  Run:   easycat replay PATH --turn <id>
         easycat journal promote PATH TURN_ID --out regression.zip
                                        (pin a regression as a test)
  Read:  docs/testing-and-evals.md
  Topic: easycat explain journal

crashed — the session errored or the agent raised.
  Run:   easycat inspect PATH --issues
         easycat journal grep PATH --query . --regex --errors
         easycat explain <Exxx>      (decode the EASYCAT_Exxx code)
  Read:  docs/observability.md
  Topic: easycat explain exit-codes

Start with `easycat inspect PATH` to see errors, turns, and the latency
waterfall in one place, then drill in with the routed command above.
"""

_BARGE_IN_BODY = """\
Barge-in (interruption) lets the caller talk over the bot and have the
bot stop. When the user starts speaking while the bot is playing audio,
the turn manager raises an interruption: pending TTS is cancelled
cooperatively via CancelToken and the journal records what the user
actually heard before the cut.

The barge-in milestone is the cutoff latency
(`user_speech_start_to_bot_stopped_ms`): how long the bot kept talking
after the caller started. `easycat bundles show PATH` and `easycat
inspect PATH` surface it per turn alongside `interruption_count`; the
debugger UI flags slow cutoffs as issue cards.

How turn-taking drives it, the 5-state FSM, and the tuning knobs live in
the turn-taking topic — see `easycat explain turn-taking`. If barge-in
never fires or fires too eagerly, that topic covers VAD sensitivity and
the smart-turn endpoint classifier.

Docs route: docs/teaching/09-interruption/ (hands-on chapter); concept:
`easycat explain turn-taking`.
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
    "events": MetaEntry(
        slug="events",
        headline="How EasyCat session events flow",
        body=_EVENTS_BODY,
    ),
    "turn-taking": MetaEntry(
        slug="turn-taking",
        headline="How turn-taking and barge-in work",
        body=_TURN_TAKING_BODY,
    ),
    "barge-in": MetaEntry(
        slug="barge-in",
        headline="How barge-in (interruption) works",
        body=_BARGE_IN_BODY,
    ),
    "journal": MetaEntry(
        slug="journal",
        headline="The execution journal and debug bundles",
        body=_JOURNAL_BODY,
    ),
    "troubleshooting": MetaEntry(
        slug="troubleshooting",
        headline="Something wrong? Route by symptom",
        body=_TROUBLESHOOTING_BODY,
    ),
}
