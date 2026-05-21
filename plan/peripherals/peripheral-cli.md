# CLI for Scaffolding and Debugging — Peripheral

> **This is a peripheral initiative.** It is not essential to the
> debug-first thesis in `../roadmap/essential-debug-first-runtime.md`. It is the
> surface that turns the essential plan's journal from an internal
> design win into something a developer actually uses on their first
> hour with EasyCat and on their worst hour with EasyCat.

## Status (2026-05-21)

M1 (scaffolding) is effectively done:

- Typer entry point registered in `pyproject.toml`; `easycat --version`
  and the journey-ordered help menu work.
- `easycat init` — full `-t/--template`, `-c/--config`, `--list-templates`,
  `--force`, `--no-git`, `--json` surface with interactive prompts
  guarded by a TTY check and schema-v1 validator with fuzzy key
  suggestions.
- `easycat doctor` — all eight checks (Python version, extras, env
  vars, provider reachability, onnxruntime, microphone, journal
  writable, disk space) tagged with their `EASYCAT_Exxx` codes.
  `--fix` now performs safe auto-remediation for E207 (mkdir the
  journal directory); other failures stay manual on purpose.
- `easycat explain` — error code registry with `exit-codes`,
  `init-schema`, `json-schema` meta-entries and fuzzy suggestions on
  unknown codes.
- Output contract (`--json`, stdout/stderr split, exit-code mapping)
  and top-level `EasyCatError` handler.
- Three templates: `openai-agents`, `pydantic-ai`, `text-chat`.

M2 gaps:

- Templates `pydantic-ai-workflow`, `twilio-phone`, `webrtc-browser`
  not shipped.
- Template `agent.py` line budgets overshoot: `openai-agents` 24 lines
  (target ≤15), `pydantic-ai` 21 lines (target ≤12), `text-chat` 18
  lines (target ≤8). Templates currently wire a `current_time` tool
  instead of the plan's `calculator` + `filesystem` MCP; the tool is
  working, but the plan text should be updated to match the shipped
  content or vice-versa.

M3 (journal debugging) — partial:

- `easycat bundles list [--path <dir>] [--json]` and `easycat bundles
  show <path> [--json]` — shipped (`src/easycat/cli/debug/bundles.py`).
  `easycat inspect <path>` is also shipped as a friendly alias for
  `bundles show`.
  `show` surfaces session id, duration, turn count, tool-call count,
  error count, artifact count, provider versions, and replay entry
  points (rendered with the new `cp_<sequence>` vocabulary).
- `easycat bundles export --for=claude-code` — not started. Needs the
  redaction pass from `peripheral-redaction.md`.
- `easycat replay` — not started. The library-level `RunBundle` +
  replay fidelity classes exist (`debug/bundle.py`,
  `runtime/replay.py`), so this is pure CLI-shell work but it is the
  biggest remaining chunk.

The `uvx` zero-install guarantee, `[project.scripts]` entry, and
error-code registry-backed `explain` surface all meet the plan's
guardrails today.

>
> **Sibling peripheral docs:**
>
> - `peripheral-dx-onboarding.md` — library DX (line budgets, `run()`,
>   `async with`, string-keyed providers, config presets, error codes,
>   dev/prod log rendering). The scaffolded projects here use that
>   library DX; it must exist first.
> - `peripheral-eval-and-debugger-ui.md` — `easycat.testing` pytest
>   fixtures, Simulator + Judge, forked replay, interactive web
>   debugger UI. The CLI's `replay` command exposes the replay
>   fidelity classes owned over there.
> - `peripheral-redaction.md` — `RedactionPolicy` and export-time
>   redaction. `bundles export` runs through this.
> - `peripheral-observability-and-cost.md` — `CostRecord`, OTel
>   export, latency budgets. `bundles show` surfaces these fields.
> - `peripheral-provider-ecosystem.md` — Deepgram Flux, Smart Turn
>   v3.1, Kyutai Pocket TTS. `doctor` probes these; templates wire
>   some of them.
>
> **In scope (this file):** `easycat init` as the primary product,
> the template catalog (content, not just count), the non-interactive
> `--config` JSON schema for coding-agent scaffolding, `easycat
> doctor` as first-run verification, `easycat explain` for stable
> error codes, and three journal-driven debugging commands —
> `bundles list`, `bundles show`, `bundles export --for=claude-code`,
> `replay` — that turn a captured journal into an answer.
>
> **Out of scope:** library-wrapper commands (`run`, `dev`, `test`,
> `cost`, `login`) are deferred to a future extended CLI plan.
> See "Commands not in this plan" below for the reasoning. Testing
> fixtures — owned by `peripheral-eval-and-debugger-ui.md`. Redaction
> policies — owned by `peripheral-redaction.md`.

## Context

EasyCat started as a library-only project. Current code now has a
`[project.scripts]` entry point, `easycat.cli`, and first-run commands; this
context explains why the CLI became a peripheral priority rather than an
optional polish layer.

Two moments in an EasyCat user's life are disproportionately
important:

**Minute zero** — "I just heard about EasyCat and want to see if it
solves my problem." The user has `uv` installed and nothing else.
They need a working voice agent in front of them in under 60 seconds.
This is the scaffolding problem. In 2026 Pipecat ships `uv tool
install pipecat-ai-cli` + `pipecat init quickstart`, LiveKit ships
`lk agent init`, and most modern Python tools treat `uvx <tool>` as
the first-invocation path. Missing this bar means most users give up.

**Minute N, when something broke in production** — "A call went
sideways last night. I have the bundle. Help me find the bug." This
is the journal debugging problem. The essential plan made journals
crash-durable and exportable; this file makes them usable without
opening a Python REPL. The 2026 default debugging flow is "pipe the
trace into your coding agent" (LangSmith Fetch, Dec 2025), not "open
a dashboard."

This file is about those two moments. It is not about being a
general-purpose CLI product. We are not racing Pipecat on command
breadth or LiveKit on dev-loop features — those are covered by the
library (`easycat.run()`, `async with session`) and by the debugger
UI in `peripheral-eval-and-debugger-ui.md`. We are racing on two
specific promises:

1. **The scaffolded project is itself the demo.** `easycat init`
   produces working code that a developer can read end-to-end in 30
   seconds, modify immediately, and run without needing to understand
   the framework's internals. Every template is a real agent, not a
   placeholder.
2. **The journal earns its keep in a real debugging session.**
   `easycat bundles export --for=claude-code` packages a crashed
   session into a context pack Claude Code can use to find the bug.
   The bar is: a developer who has never seen EasyCat before can be
   handed a bundle and their coding agent and make progress.

## Design Principles

Four load-bearing principles. Every command in this file can be
traced back to one of these.

### 1. Zero-install first invocation

`uvx easycat init my-agent` on a clean laptop with only `uv`
installed must produce a working project. Non-negotiable — 2026
users expect it, and the cost of violating it is that most of them
give up before installing. Forces: `[project.scripts]` registration,
no heavy base deps, templates importable without optional extras.

### 2. Coding-agent-native

Every command accepts a non-interactive path. Claude Code, Cursor,
and Codex must drive EasyCat without a human at the keyboard.

- `easycat init --config '{...}'` with a stable, versioned JSON
  schema. No interactive prompts unless a TTY is attached and no
  `--config` is passed.
- `--json` mode on every command that prints human output. Stable
  schema with `schema_version`.
- Exit codes form a contract; scripts branch on them.
- Stdout for primary output, stderr for logs. `2>/dev/null` is safe.

In 2026 coding agents scaffold more projects than humans do. The
`--config` schema is the single most important surface in this file.

### 3. The scaffolded project is the demo

Templates are not "hello world" placeholders you throw away. Each
template is a real agent with a personality, a tool, and a README
that explains what to change first. Three consequences:

- Template `agent.py` ≤ 15 lines and readable end-to-end in 30
  seconds.
- Every template has a working tool/MCP wire-up — no blank slates.
- The scaffolded code uses the library exactly as experienced
  developers use it. No CLI-private magic, no `# TODO: replace this`
  comments. The upgrade path is "modify what you see."

### 4. The CLI is a reader of the library, not a parallel codepath

`easycat init` writes files and calls nothing at runtime. `easycat
bundles show` calls `RunBundle.load()`. `easycat replay` calls into
the essential plan's replay contract. If a command needs
functionality that does not exist in the library, it goes in the
library first.

This is how we avoid the drift that plagues most CLIs — divergence
between what scripts do and what the CLI does.

## Package Layout

```
src/easycat/cli/
    __init__.py           # exports `main`, the Typer entry point
    __main__.py           # `python -m easycat.cli` support
    _app.py               # Typer app construction
    _output.py            # Rich console, --json mode, exit codes
    _errors.py            # EASYCAT_Exxx → CLI exit code mapping
    scaffold/
        __init__.py
        init.py           # `easycat init`
        _schema.py        # `--config` JSON schema + validator
        templates/        # one directory per template
            openai-agents/
            pydantic-ai/
            pydantic-ai-workflow/
            twilio-phone/
            webrtc-browser/
            text-chat/
    diagnose/
        __init__.py
        doctor.py         # `easycat doctor`
        explain.py        # `easycat explain E012`
        _codes.py         # error-code registry (canonical source)
    debug/
        __init__.py
        bundles.py        # `easycat bundles list|show|export`
        replay.py         # `easycat replay`
```

One file per top-level command. No command file exceeds 250 lines —
past that the command has grown too many flags and needs splitting.

### Entry point

```toml
# pyproject.toml
[project.scripts]
easycat = "easycat.cli:main"
```

`easycat.cli.main` is a zero-argument function that constructs the
Typer app and calls it. Keeping `main` trivial lets us wire alternate
entry points later without refactoring.

### Dependency footprint

Two new base dependencies:

- `typer >= 0.16` — Rich-integrated `--help`, type-hint ergonomics
- `rich >= 13` — Typer pulls it in already; we use it directly

Everything else stays in extras. The CLI must work with only these
two added. Startup budget: `easycat --version` under 300ms on cold
import, measured in CI on every release. Lazy-import anything heavy
(template rendering imports only when `init` runs, journal reading
imports only when `bundles`/`replay` runs).

## Primary: `easycat init`

The product. Everything else supports this command.

### Synopsis

```
Usage: easycat init [OPTIONS] NAME

Options:
  -t, --template TEXT       Template to use [default: openai-agents]
  -c, --config TEXT         JSON config for non-interactive scaffolding
      --list-templates      Print available templates and exit
      --force               Overwrite existing directory
      --no-git              Skip `git init` in the new project
      --json                Emit machine-readable output
      --help                Show this message and exit
```

### Behavior

- If `NAME` exists and is non-empty, refuse with `EASYCAT_E101`
  unless `--force`.
- With no `--config` and a TTY attached, prompt for: template,
  default STT/TTS/LLM provider, agent name, agent instructions. Each
  prompt has a sensible default pre-filled from env vars (if
  `DEEPGRAM_API_KEY` is set, default STT is `deepgram/flux`).
- With `--config '{...}'` OR no TTY: non-interactive, required keys
  defaulted, unknown keys rejected (`EASYCAT_E102` with fuzzy-match
  suggestions) so coding agents get typo feedback.
- Writes `agent.py`, `.env.example`, `pyproject.toml` with
  `easycat[<extras>]` pinned, `README.md`, `.gitignore`.
- Runs `git init` by default; `--no-git` skips.
- Prints next-step commands: `cd <name> && uv sync && uvx easycat
  doctor`.

### Non-interactive `--config` JSON schema

The single most important surface in this file. Versioned, validated,
documented under `easycat explain init-schema`.

```json
{
  "schema_version": 1,
  "template": "openai-agents",
  "stt": "deepgram/flux",
  "tts": "openai",
  "llm": "openai/gpt-4.1-mini",
  "transport": "local",
  "agent_name": "Support",
  "agent_instructions": "Help the user with billing questions.",
  "tools": ["calculator"],
  "mcp_servers": ["filesystem"]
}
```

Rules:

- `schema_version` is required. Missing or unknown bumps reject
  loudly, never silently.
- `template` values come from the template directory names —
  unknown values use fuzzy-match suggestions.
- Provider strings (`stt`, `tts`, `llm`) reuse the string-keyed
  provider selection from `peripheral-dx-onboarding.md`.
- `tools` references a curated registry of tool stubs shipped with
  each template (each stub is a real working tool, not a TODO).
- `mcp_servers` references a curated registry of MCP servers (at
  minimum: `filesystem`, the official MCP filesystem server).
- Unknown top-level keys reject. This is deliberate — coding agents
  will send typos, and silent acceptance is worse than loud
  rejection.

Schema bumps are a library release event, documented in the
changelog and reachable via `easycat explain init-schema --version 2`.

### Golden path

```
$ uvx easycat init my-agent
? Template: (openai-agents) openai-agents
? Agent name: (Support) Support
? Agent instructions: Help the user with billing questions.
? STT provider: (openai) deepgram/flux
? TTS provider: (openai) openai
? LLM provider: (openai/gpt-4.1-mini) openai/gpt-4.1-mini
Creating my-agent/
  ✓ agent.py (12 lines)
  ✓ pyproject.toml
  ✓ .env.example
  ✓ README.md
  ✓ .gitignore
  ✓ git init

Next steps:
  cd my-agent
  cp .env.example .env  # then fill in your API keys
  uv sync
  uvx easycat doctor    # verify your setup
  uv run python agent.py
```

Non-interactive equivalent:

```
$ uvx easycat init my-agent --config '{
  "schema_version": 1,
  "template": "openai-agents",
  "stt": "deepgram/flux",
  "agent_name": "Support",
  "agent_instructions": "Help the user with billing questions."
}'
Creating my-agent/
  ✓ agent.py (12 lines)
  ...
```

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Ok |
| 2 | Bad flag or unknown template |
| 4 | Bad `--config` JSON or unknown key |
| 101 | Target exists (without `--force`) |

## Template Catalog

Templates are the most important content EasyCat ships. Each one is
a working agent with a personality, a tool, and a README that
teaches the next step.

### Content contract

Every template directory contains:

```
templates/<name>/
    agent.py              # ≤ 15 lines, runs with one API key
    pyproject.toml        # easycat[<extra>] + minimal deps
    .env.example          # exactly the keys the template needs
    README.md             # install → configure → run → next steps
    .gitignore
    [additional files as needed — e.g., static/ for webrtc]
```

CI enforces for every template:

1. `agent.py` within line budget.
2. `agent.py` parses and imports cleanly on a fresh install of
   `easycat[<extra>]`.
3. Startup success: `uvx easycat init demo --template <name> && cd
   demo && uv sync && uv run python agent.py` reaches `TurnStarted`
   within 60s against a stub transport.
4. `doctor` passes on the scaffolded project with valid test keys.
5. `README.md` has the four required sections: Install, Configure,
   Run, Next Steps.

### Templates

**`openai-agents`** (default)

OpenAI Agents SDK, local mic transport. The agent is a support bot
with one `calculator` tool and the `filesystem` MCP server wired up.
Shows: Agent definition, tool usage, MCP wire-up, `easycat.run()`.

`agent.py` (target ≤ 12 lines):

```python
from agents import Agent
from easycat import EasyCatConfig, run

run(EasyCatConfig(
    agent=Agent(
        name="Support",
        instructions="Help the user with billing questions.",
        tools=[...],
    ),
    stt="deepgram/flux",
    mcp_servers=["filesystem"],
))
```

**`pydantic-ai`**

PydanticAI single-agent, local mic. One tool wired in. Shows:
PydanticAI agent construction, structured output, tool wiring.
Target ≤ 12 lines.

**`pydantic-ai-workflow`**

PydanticAI workflow with two specialists (billing + technical) and
a router. Shows: multi-agent handoffs, the bridge's workflow pass-
through. Target ≤ 15 lines.

**`twilio-phone`**

Inbound PSTN bot via Twilio. `agent.py` plus a small `server.py` for
the FastAPI app; `agent.py` itself stays under budget. The bot reads
back the caller's number and takes a message. Shows: telephony
transport, FastAPI wiring. Ships with a README section on ngrok for
local testing.

**`webrtc-browser`**

WebRTC server + a single-file `static/index.html` client. The agent
greets the user and answers basic questions. Shows: WebRTC
transport, browser integration, single-file client-side JS. Target
`agent.py` ≤ 12 lines; HTML client is separate.

**`text-chat`**

Text-mode session for REPL-style testing of agent changes without
audio infrastructure. The single best template for iterating on
prompts. Target ≤ 8 lines.

### Templates we are NOT shipping

- **Voice-to-voice / realtime speech-to-speech.** Permanently out
  of scope. See "Chained Only" in `../roadmap/essential-debug-first-runtime.md`.
- **Offline-only template.** Deferred to a future milestone gated on
  Smart Turn v3.1 promotion (`peripheral-provider-ecosystem.md`)
  and Kyutai Pocket TTS integration. When it ships, it will be the
  `offline` template.
- **"Blank" template.** Every template has a personality. A blank
  starter teaches less than a filled-in one the user modifies.

### Adding a new template

Drop a directory under `cli/scaffold/templates/`. CI picks it up
automatically. No code changes to the CLI. This matters — we expect
templates to grow with the provider ecosystem, and that growth
should be a one-directory change.

## Supporting: `easycat doctor`

First-run verification after `init`, and the first thing to run when
something stops working. This is the connective tissue between
scaffolding and debugging.

```
Usage: easycat doctor [OPTIONS]

Options:
      --environment [dev|production]  Profile to check [default: dev]
      --provider TEXT                 Only check this provider
      --fix                           Offer auto-fixes where safe
      --json                          Emit machine-readable output
      --help                          Show this message and exit
```

Checks (each labeled with its `EASYCAT_Exxx` code on failure):

1. Python ≥ 3.11 (`E201`)
2. EasyCat version and extras installed (`E202`)
3. Env vars present: `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, etc.
   (`E203`)
4. Provider reachability — 200ms HEAD request per set key (`E204`).
   Deepgram Flux gets its own probe per
   `peripheral-provider-ecosystem.md`.
5. `onnxruntime` importable when `smart-turn` extra is installed
   (`E205`)
6. Default microphone device when transport is `local` (`E206`)
7. Journal writable at `~/.cache/easycat/journals/` (`E207`)
8. Disk space > 500MB free (`E208`)

Each row prints in a Rich table with color-coded status. Failures
link to `easycat explain Exxx`. `--fix` offers interactive
resolution for safe fixes (create missing dirs, install missing
extras — never modify env vars). Exit 0 if all pass, 1 if any fail.

## Supporting: `easycat explain`

Rust `cargo --explain` pattern. Every `EASYCAT_Exxx` code has a
canonical doc with cause, example, and fix.

```
Usage: easycat explain CODE [OPTIONS]

Options:
      --list                 List all error codes and exit
      --json                 Emit machine-readable output
      --help                 Show this message and exit
```

- `easycat explain E012` prints the canonical doc from the
  registry at `src/easycat/cli/diagnose/_codes.py`.
- Codes namespaced by range: `E1xx` scaffold, `E2xx` environment,
  `E3xx` runtime, `E4xx` bundle/replay, `E5xx` CLI usage.
- Each entry has: headline, cause, failing-code example, fix,
  related codes.
- `easycat explain exit-codes` documents the exit-code contract.
- `easycat explain init-schema` documents the `--config` schema.
- Unknown code prints fuzzy-match suggestions.

The registry is a single Python dict. Adding a code is a one-file
change. CI asserts every raised `EasyCatError` subclass has a
matching entry — a raised code without a `explain` doc fails CI.

## Secondary: Journal Debugging

The second reason this CLI exists. The essential plan made journals
crash-durable; the commands below turn a journal into an answer.

### `easycat bundles list`

Discover bundles written to the default cache dir or a given path.

```
Usage: easycat bundles list [OPTIONS]

Options:
      --path PATH             Directory to scan [default: ~/.cache/easycat/bundles]
      --since TEXT            e.g., "yesterday", "7d", "2026-04-01"
      --has-error             Only bundles with an error
      --json                  Emit machine-readable output
      --help                  Show this message and exit
```

Output is a Rich table: timestamp, session_id, turns, duration, cost
(if `CostRecord` present), error (if any), path. `--json` emits a
stable array.

The default path is where the essential plan's crash-durable journal
writes bundles. If a user says "my agent crashed last night,"
`easycat bundles list --has-error --since yesterday` is the first
command they run.

### `easycat bundles show`

Inspect one bundle without unpacking it in Python.

```
Usage: easycat bundles show [OPTIONS] BUNDLE

Options:
      --turn INT              Show only this turn
      --records               Include full journal records (verbose)
      --json                  Emit machine-readable output
      --help                  Show this message and exit
```

Default output: session config, turn timeline (one line per turn
with stage latencies), errors with frames, cost breakdown, budget
violations. `--records` dumps the full journal. This is a read-only
lens — users who want an interactive view use the debugger UI in
`peripheral-eval-and-debugger-ui.md`.

### `easycat bundles export`

The headline debugging command. Transforms a bundle into a context
pack a coding agent can consume.

```
Usage: easycat bundles export [OPTIONS] BUNDLE

Options:
      --for [claude-code|cursor|codex|raw]  Consumer format [default: claude-code]
      --output PATH             Output path [default: ./<bundle>-pack/]
      --redaction TEXT          development|production|regulated [default: production]
      --include-audio / --no-include-audio  [default: no]
      --help                    Show this message and exit
```

`--for claude-code` produces the flat-file context pack modeled on
LangSmith Fetch (Dec 2025): journal timeline as Markdown, failing
turn's artifacts as referenced blobs, suggested fix-code locations
inferred from traceback frames. The user then runs their coding
agent against the pack directory.

`--redaction` runs through `peripheral-redaction.md`. Default is
`production` — never leak unredacted data into an LLM context. Users
who want full fidelity for local-only debugging pass `--redaction
development` explicitly.

`--include-audio` is opt-in because audio blobs are large and most
coding-agent debugging works from transcripts + events.

This is the command we expect most debugging sessions to use. The
interactive debugger UI (`peripheral-eval-and-debugger-ui.md`) is
secondary — a minority of debugging is exploratory enough to want a
timeline view.

Exit codes: 0 ok, 5 bundle missing or corrupt, 1 export error.

### `easycat replay`

Replay a bundle against current runtime code. Wraps the three replay
fidelity classes from `../workstreams/workstream-4-replay-and-bundle.md`.

```
Usage: easycat replay [OPTIONS] BUNDLE

Options:
      --fidelity [artifact|simulated|live]  [default: simulated]
      --diff                    Print journal diff vs bundle baseline
      --fail-on-regression      Nonzero exit on latency/drift detection
      --help                    Show this message and exit
```

`--fidelity` selects among the essential plan's replay classes:

- `artifact` — replay recorded artifacts byte-for-byte (fastest,
  deterministic).
- `simulated` — replay against simulated provider responses (the
  default; matches pytest fixture behavior).
- `live` — re-execute against live providers (costs real money, used
  for "did the fix actually work" validation).

`--fork-at cp_87` (fork-replay) is deferred to a follow-up; it
depends on the forked_replay fidelity class owned by
`peripheral-eval-and-debugger-ui.md`. When it lands, it slots in as
a fourth `--fidelity` value.

`--fail-on-regression` is the CI integration point. Teams wire
`easycat replay fixtures/*.zip --fail-on-regression` into PR
pipelines to catch latency regressions from bundle fixtures.

Exit codes: 0 clean, 1 replay error, 5 bundle missing or corrupt, 6
regression detected.

## Commands NOT in This Plan

Explicit non-goals, with reasoning.

- **`easycat run`** — call `uv run python agent.py`. Adding a
  wrapper doesn't save meaningful keystrokes, and `run` without the
  broader set (logs, signal handling, flag pass-through) is strictly
  worse than a plain Python invocation. Defer; revisit if user
  research says it's missed.
- **`easycat dev`** — the dev loop (file watcher, bridge swap,
  debugger UI auto-launch) is genuinely valuable, but it's deep
  enough to deserve its own plan. The library surface for it
  (`run(..., debug="full")` + an auto-reload helper) already
  exists in `peripheral-dx-onboarding.md`. Defer to
  a future extended CLI plan.
- **`easycat test`** — `pytest` with the `easycat.testing` plugin
  pre-loaded is a one-line convenience, but `pyproject.toml` can
  already register the plugin in `[tool.pytest.ini_options]`.
  Scaffolded templates do exactly that. No wrapper needed.
- **`easycat cost`** — cost rollups are valuable but the per-session
  cost is already visible in `bundles show` and the dev waterfall
  (`peripheral-eval-and-debugger-ui.md`). A dedicated command is
  polish, not thesis. Defer.
- **`easycat login` / credential store** — API keys go in `.env`.
  Managing credentials is the OS's job.
- **`easycat deploy`** — deployment is documented in
  `peripheral-deployment.md`; the CLI is not a deploy tool.
- **Plugin system** — `uv tool install easycat-plugin-foo` style
  extension is out of scope. Small, vendored command surface stays
  small.
- **`easycat update`** — `uv tool upgrade easycat` already works.

If a deferred command's case becomes obvious from user research, it
lives in a future extended CLI plan — separate plan, separate
review.

## Output Contract

### Human mode (default)

Rich rendering. Colors, tables, spinners. Auto-detects `NO_COLOR`
and `CI=true` and degrades gracefully.

- Info lines start with `  ` (two spaces).
- Success lines start with `  ✓` (green).
- Warning lines start with `  !` (yellow).
- Error lines start with `  ✗` (red), always include the
  `EASYCAT_Exxx` code, and end with `Run \`easycat explain Exxx\``.

### JSON mode (`--json`)

Every command accepts `--json` and emits a stable, versioned schema:

```json
{
  "schema_version": 1,
  "command": "doctor",
  "status": "ok",
  "checks": [...]
}
```

`schema_version` bumps on breaking changes; old versions stay
documented via `easycat explain json-schema`. Non-zero exit codes
set `status: "error"` with the error code embedded.

Stdout is for primary output; stderr is for logs, progress, and
diagnostics. A coding agent can `2>/dev/null` without losing data.

## Exit Code Contract

Documented under `easycat explain exit-codes`:

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Runtime error |
| 2 | Bad usage (unknown flag, missing argument) |
| 3 | Missing credentials |
| 4 | Missing optional extra, bad `--config` JSON |
| 5 | Bundle missing or corrupt |
| 6 | Regression detected (`replay --fail-on-regression`) |
| 101 | Target directory exists (`init` without `--force`) |

Codes map one-to-one with `EASYCAT_Exxx` categories. Scripts branch
on exit code without parsing output.

## Help Architecture

Bare `easycat` prints a journey-ordered menu:

```
$ uvx easycat
EasyCat — voice bot framework

  Scaffold
    init        Scaffold a new project from a template
    doctor      Check environment and provider reachability
    explain     Look up an error code (like `cargo --explain`)

  Debug with the journal
    bundles     List, inspect, and export RunBundles
    replay      Replay a RunBundle against current code

Run `easycat <command> --help` for command-specific options.
Run `easycat explain <code>` to understand an error.
```

Every subcommand's `--help` is rendered by Typer + Rich. Two
conventions on top of Typer defaults:

1. **Example block at the bottom of each `--help`.** Add an epilog
   with 1-2 golden-path examples.
2. **Related-commands block.** `easycat doctor --help` ends with
   `Related: easycat init, easycat explain`.

## Error UX Integration

Every `EasyCatError` in the library carries:

- `code` — stable `EASYCAT_Exxx`
- `message` — first-person fix-suggesting text
- `context` — `turn_id`, `stage`, `elapsed_ms`, user-config file+line
- `notes` — PEP 678 `__notes__` with bridge and provider context

The CLI's top-level error handler:

1. Catches `EasyCatError` before Typer's traceback printing.
2. Renders with Rich, including fuzzy-match suggestions if the
   error carries one.
3. Prints `Run \`easycat explain <code>\`` as the last line.
4. Sets the matching exit code.
5. In `--json` mode, emits `{status: "error", code, message,
   context, exit_code}`.
6. With `EASYCAT_DEBUG=1` or `--verbose`, falls through to a full
   Rich traceback with third-party frames collapsed per
   `peripheral-dx-onboarding.md`.

The error-code registry in `_codes.py` is a single source of truth.
`easycat explain` reads from it; raising code reads from it; tests
assert every factory's rendered message matches its `explain` doc.

## CLI-Specific Testing

Two layers:

### Unit (`tests/cli/`)

- Typer's `CliRunner` drives each command; asserts stdout, stderr,
  exit code.
- Golden-file tests for `--help` and `--json` output. Snapshots
  checked into the repo, `pytest --update-snapshots` regenerates.
- Error-path tests: every `EASYCAT_Exxx` has a test that exercises
  the failure path and asserts the code surfaces in CLI output.
- Template lint: load every template's `agent.py`, assert it parses,
  passes ruff, does not exceed line budget.

### End-to-end (`tests/cli/e2e/`, marked `integration_local`)

- **Scaffold smoke matrix.** For each template: `init` into a tmpdir
  with stub `--config`, `uv sync`, import the scaffolded `agent.py`,
  run it against a fake transport, assert it reaches `TurnStarted`.
- **`bundles export --for=claude-code` round-trip.** Capture a
  RunBundle in memory, export, assert output pack shape matches the
  documented contract.
- **`replay` against checked-in fixture bundles**, `--fail-on-
  regression` asserting no latency drift.

CI runs both on every PR. Template matrix isolates failures per
template so one broken template doesn't block others.

## `uvx` Zero-Install Guarantee

`uvx easycat init my-agent` on a clean machine with only `uv`
installed must succeed. Guarantees:

1. No build-from-source deps in the base install.
2. Base wheels for Linux x86_64/aarch64, macOS arm64/x86_64,
   Windows x86_64.
3. Package size under 2MB (CLI + library, excluding extras).
4. `easycat --version` under 300ms on cold import.

First three are CI-enforced every release. The 300ms budget is
tracked because import-time compounds across every CLI invocation
and is what makes the tool feel snappy.

## Dependencies

| Item | Depends on |
|------|------------|
| Typer app skeleton, `--version`, `--help` | nothing |
| `easycat explain`, error-code registry | error codes from `peripheral-dx-onboarding.md` |
| `easycat init`, templates | library DX (`run()`, string keys) from `peripheral-dx-onboarding.md` |
| Template content (what `agent.py` looks like) | library DX (same) |
| `easycat doctor` | provider registries, `peripheral-provider-ecosystem.md` Flux probe |
| `easycat bundles list`, `show`, `export` | essential Phase 4 (RunBundle + crash-durable journal) |
| `easycat bundles export --redaction ...` | `peripheral-redaction.md` |
| `easycat replay` | essential Phase 4 (replay fidelity classes) |
| `easycat replay --fork-at` (future) | forked_replay (`peripheral-eval-and-debugger-ui.md`) |

## Suggested Sequencing

Three milestones. Each produces user-visible value and does not
depend on the next.

### M1 — Scaffolding works (ships with essential Phase 1–2)

Unblocks `uvx easycat init my-agent → uv sync → uv run python
agent.py` end-to-end in under 60 seconds.

- Typer app skeleton at `src/easycat/cli/`
- `[project.scripts]` registration
- `easycat --version`, `easycat --help`, journey menu
- `easycat explain` + error-code registry (with `exit-codes` and
  `init-schema` meta-entries)
- `easycat init` with three templates: `openai-agents`,
  `pydantic-ai`, `text-chat`
- `easycat init --config` non-interactive path with schema v1
  validator
- `easycat doctor` with checks 1–5 (env, extras, provider
  reachability)
- Unit tests for all of the above
- Template smoke tests in CI (lint + line budget + import)

### M2 — Every template + doctor complete

Finishes the scaffolding surface.

- Remaining templates: `pydantic-ai-workflow`, `twilio-phone`,
  `webrtc-browser`
- `easycat doctor` checks 6–8 (microphone, journal, disk)
- `easycat doctor --fix` for safe auto-fixes
- E2E scaffold matrix: init → sync → run per template against stub
  transport
- `easycat doctor --environment=production` profile (depends on
  `peripheral-deployment.md` detection hooks)

### M3 — Journal debugging (ships with essential Phase 4)

Connects scaffolded projects to production debugging.

- `easycat bundles list | show | export`
- `easycat bundles export --for=claude-code|cursor|codex|raw`
- `easycat bundles export --redaction` integration
  (`peripheral-redaction.md`)
- `easycat replay` with `artifact`/`simulated`/`live` fidelity
- `easycat replay --fail-on-regression` for CI

### Deferred

- `easycat replay --fork-at` (depends on forked_replay fidelity in
  `peripheral-eval-and-debugger-ui.md`)
- `offline` template (depends on Smart Turn v3.1 + Kyutai Pocket
  TTS; `peripheral-provider-ecosystem.md`)
- The library-wrapper commands moved to a future extended CLI plan

## Guardrails

Reject any change that violates these:

- A template exceeds its line budget.
- A template generates an `agent.py` with a `# TODO` or placeholder.
- `easycat init` requires a TTY.
- `easycat init --config` silently accepts unknown keys.
- `uvx easycat --version` exceeds 300ms on cold import.
- A new `EasyCatError` subclass lacks an entry in the explain
  registry.
- A command prints structured data to stdout but does not accept
  `--json`.
- `uvx easycat <any command> --help` fails to render on a fresh
  install with no extras.
- `easycat bundles export --for=claude-code` defaults to anything
  less strict than the `production` redaction policy.

## Competitive Context

- **Pipecat `pipecat-ai-cli`** (2026): `pipecat init quickstart`,
  interactive prompts, `--config` JSON. Sets the floor. Our
  differentiator is the bundle-export debugging flow, which Pipecat
  does not have.
- **LiveKit `lk agent`** (1.5, March 2026): `lk agent init`, hot
  reload in `dev`. We deliberately cede the dev-loop ground to the
  library (`peripheral-dx-onboarding.md`) and to a future
  a future extended CLI plan. Scaffolding quality and journal
  debugging are where we beat them.
- **LangSmith Fetch + Polly** (Dec 2025): "pipe traces into the
  coding agent." `easycat bundles export --for=claude-code` is
  EasyCat's version — but it ships open-source, local-first, no
  LangSmith account required. That's the pitch.
- **Rust `cargo --explain`**: the error-code-with-explanation pattern
  users already trust. `easycat explain Exxx` follows it exactly.
- **`uv` and `ruff`**: fast-startup CLIs set the perceived-
  responsiveness bar. We match with Typer + lazy imports.
