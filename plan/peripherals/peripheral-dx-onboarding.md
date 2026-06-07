# DX and Onboarding — Peripheral

> **This is a peripheral initiative.** It is not essential to the
> debug-first thesis in `../roadmap/essential-debug-first-runtime.md`. It is,
> however, the most visible user-facing work in the overall redesign, and
> the one the outside world will judge EasyCat on first.

## Status (2026-06-07)

Shipped:

- `easycat.run(config)` with auto-attached runtime feedback
  (`src/easycat/helpers.py::run`).
- String-keyed provider selection (`stt="deepgram/flux"`,
  `tts="cartesia/sonic-3"`) with fuzzy suggestions on typos
  (`stt/factory.py`, `tts/factory.py`).
- Provider shortcut examples rely on `EasyConfig` / the `stt=` and `tts=`
  shortcut parsers to read stage-specific API keys instead of duplicating
  `require_env(...)` checks in the visible runtime code
  (`examples/*_voice.py`, `examples/combined_providers.py`).
- `OPENAI_API_KEY` auto-detection → OpenAI chain default
  (`config/easy.py`).
- PydanticAI slim examples rely on `EasyConfig.mic(...)` to validate the
  default OpenAI provider key instead of carrying a separate
  `require_env("OPENAI_API_KEY")` preflight
  (`examples/pydantic_ai*.py`, `examples/*_pydantic.py`).
- Stable `EASYCAT_Exxx` error codes with headline / cause / fix / example /
  related (`errors.py`).
- Third-party traceback frame collapse
  (`src/easycat/runtime/records.py::ErrorInfo.from_exception`).
- PEP 678 exception notes are captured in `ErrorInfo.notes`, so provider and
  framework diagnostics survive journal filters and debug bundle export
  (`runtime/records.py`).
- Runtime `Error` events annotate wrapped exceptions with PEP 678
  `stage=...`, `provider=...`, `code=...`, `session_id=...`, and
  `turn_id=...` notes before journal capture (`events.py`).
- Stage wrappers annotate re-raised provider exceptions with PEP 678
  `stage=...`, `provider=...`, `elapsed_ms=...`, `sequence=...`, and
  `record_key=cp_<sequence>` notes and record the same elapsed / input-record
  context on `stage_error` journal records (`stages/base.py`, `stages/*.py`).
- `ErrorInfo.from_exception(ExceptionGroup(...))` now preserves the grouped
  child error tree for journal, SQLite, bundle, and debugger consumers while
  retaining the flat notes summary (`runtime/records.py`, `runtime/journal.py`).
- Streaming agent turns now emit journal-visible `Error` events for TTS
  synthesis failures, and emit a pipeline `ExceptionGroup` when agent and TTS
  branches both fail in the same turn (`session/_turn_runner.py`).
- `debug="light" | "full"`, `export_debug_bundle()` (`config/easy.py`,
  `src/easycat/session/_session.py::export_debug_bundle`).
- `async with session:` context-manager support
  (`src/easycat/session/_session.py::__aenter__` /
  `src/easycat/session/_session.py::__aexit__`).
- `EasyConfig.mic() / .browser() / .phone()` factory presets
  (`config/easy.py`). Text-mode sessions go through `create_text_session()`
  instead of a `.text()` classmethod because the config itself
  requires STT/TTS providers that a text session skips.
- `EASYCAT_LOG_LEVEL` env var honoured by `run()` (`helpers.py`).
- `EASYCAT_ENV=prod|production` selects the single-line JSON console logger by
  default while `dev`/unset keeps the human renderer; explicit
  `EASYCAT_LOG_FORMAT=json|text|human` still wins and rejects unknown values
  instead of silently falling back (`_logging.py`, `docs/observability.md`).
- `EasyConfig(record_to=...)` auto-captures a debug bundle on clean
  stop/shutdown when debug journaling is enabled (`config/_factory.py`).
- `smart_turn=True` and `smart_turn_sensitivity=0..1` now normalize to
  `SmartTurnConfig(enabled=True, threshold=1-sensitivity)` so common endpoint
  tuning does not require importing the lower-level config class.
- Config flattening pass meets the target: currently 22 top-level `EasyConfig` fields
  against the target ≤22.

Still remaining:

- Example visible-code budgets are CI-enforced for the canonical examples:
  `openai_agents_voice.py` ≤7, `pydantic_ai_voice.py` ≤8, and
  `ws_server.py` ≤15, excluding setup docstrings and import guards.
  Provider shortcut and PydanticAI slim examples have shed duplicate key
  preflights; broader raw line-count shrinkage remains open, especially in
  server and protocol-heavy examples.
- `EasyConfig.offline()` preset (depends on Kyutai Pocket TTS +
  Whisper-small + Smart Turn v3.2 wiring).
- Pipeline-wide `ExceptionGroup` propagation outside the streaming agent/TTS
  turn path and remaining exception paths outside the stage wrappers.
- Full structlog dev/prod renderer split remains; today's stdlib logger has
  the `EASYCAT_ENV=prod` JSON-renderer behavior but not structlog processors.
- Advanced knobs promised by the plan that aren't yet config fields:
  `warmup=`, `max_session_cost_usd=`, `latency_budget=`.

The high-leverage DX wins are shipped; the remaining work is either
ecosystem-gated (offline preset on Kyutai) or mechanical cleanup
(structlog, field flattening).

>
> **Sibling peripheral docs:**
>
> - `peripheral-cli.md` — first-class `easycat` CLI design (Typer app,
>   command surface, output contract, templates, error UX, `uvx`
>   zero-install guarantee). This file owns the library DX the CLI
>   wraps; that file owns the CLI product.
> - `peripheral-redaction.md` — `RedactionPolicy` write filter, safe
>   snapshots, export-time redaction pass, ready-to-use policies
> - `peripheral-provider-ecosystem.md` — Deepgram Flux, Smart Turn v3.2
>   promotion, backchannel filter
> - `peripheral-observability-and-cost.md` — OTel export, cost modeling
>   with pricing source, latency budgets, warmup stage
> - `peripheral-eval-and-debugger-ui.md` — `easycat.testing`, Simulator +
>   Judge, forked replay, interactive web debugger UI, dev waterfall
>
> **In scope (this file):** line-count budgets on canonical examples,
> `easycat.run()` and `async with session` helpers, string-keyed provider
> selection, env var auto-detection, template content (the CLI surface
> that uses them lives in `peripheral-cli.md`), config factory presets,
> offline preset, error diagnostics (stable codes, fix-suggesting
> messages, `ExceptionGroup`, exception notes, traceback frame collapse,
> dev vs prod log rendering), `EasyConfig` flattening, quickstart
> guardrails.
>
> **Out of scope:** the `easycat` CLI command catalog, `--help`
> taxonomy, exit-code contract, `uvx` packaging — all owned by
> `peripheral-cli.md`.

## Context

The canonical local examples now fit the visible-code budget while keeping
setup docstrings and import guards in the file. LiveKit and Pipecat both
shipped one-command scaffolding in 2026. The single most important success
signal for onboarding is: `git clone` → working voice agent under 60 seconds
with one API key.

This file owns closing that gap. None of its contents is required to
deliver the debug-first thesis. All of it is required for EasyCat to hold
its own against Pipecat's `pipecat-ai-cli` and LiveKit's `lk agent init`
in 2026.

## Line-Count Budgets on Canonical Examples

Hard ceilings measured against visible runtime code in `examples/`,
CI-enforced:

- `examples/openai_agents_voice.py` (OpenAI Agents, local mic): **≤ 7 lines**
- `examples/pydantic_ai_voice.py` (PydanticAI, local mic): **≤ 8 lines**
- `examples/ws_server.py` (WebSocket server): **≤ 15 lines**

Enforcement:

- CI asserts each example's line count against its budget.
- Every change to the runtime must shrink, not preserve, the canonical
  example budget.
- "Add a new knob" PRs that do not include a corresponding default are
  blocked until one exists.

Current ceremony to remove:

- explicit env var checking (`require_env("OPENAI_API_KEY")`) → auto-detect
- explicit adapter construction (`build_openai_agents_adapter`,
  `PydanticAIAdapter`) → duck-type and auto-adapt any `Agent`-shaped object
- explicit transport config (`LocalTransportConfig()`) → sensible default
  transport based on runtime environment
- explicit event logging setup (`default_event_logging()`) → journal is on
  by default
- explicit runtime feedback attachment (`attach_runtime_feedback(session)`)
  → auto-attached when `sys.stderr.isatty()` and not in a test environment
- explicit shutdown signal handling (`wait_for_shutdown_signal(session)`)
  → handled by `run()` or `async with session`
- explicit `asyncio.run(main())` wrapper → handled by `run()`

## Quickstart Helpers

**`easycat.run(config)`**

20-line wrapper that replaces `asyncio.run(main())` + `await
session.start()` + shutdown handling. Thin enough that advanced users can
still reach the session object via `create_session()`.

Target example:

```python
# examples/openai_agents_voice.py — 7 visible runtime lines
from agents import Agent
from easycat import EasyConfig, run

run(EasyConfig.mic(
    agent=Agent(name="Support", instructions="Help the user."),
))
```

**`async with session:`**

Context manager support matching `httpx.AsyncClient`, `asyncpg`, and
`anyio.TaskGroup`. For users who already have an asyncio loop:

```python
# 10 lines
from agents import Agent
from easycat import EasyConfig, create_session

async def main():
    session = create_session(EasyConfig.mic(
        agent=Agent(name="Support", instructions="Help the user."),
    ))
    async with session:
        await session.wait_closed()
```

Neither path should require the user to think about signal handling or
shutdown order.

## String-Keyed Provider Selection

Match the DX LiveKit and Pipecat established in 2026 — providers
addressable by string, not by adapter construction. Swapping STT or TTS
becomes a one-word change.

```python
run(EasyConfig.mic(
    agent=Agent(name="Support", instructions="Help the user."),
    stt="deepgram/flux",
    tts="cartesia/sonic-3",
    llm="openai/gpt-4.1-mini",
))
```

Semantics:

- Parser over existing `stt/factory.py` and `tts/factory.py` registries.
- `"deepgram/flux"` splits into `provider="deepgram"` and `model="flux"`,
  fed through existing config dataclasses with sensible defaults.
- Typed config path (`DeepgramSTTConfig(model="flux", ...)`) still works
  and takes precedence.
- Missing API keys produce `EASYCAT_Exxx` with a shell snippet fix.
- Invalid provider strings use fuzzy matching ("did you mean 'deepgram'?").
- Pure DX layer over existing registries. No new provider infrastructure.

## Env Var Auto-Detection

- If `DEEPGRAM_API_KEY` is set and `stt=` is omitted, pick `deepgram/flux`.
- If only `OPENAI_API_KEY` is set, pick the OpenAI chain.
- Simplest working config has zero provider strings — just an agent and
  an env var.

## `easycat` CLI

The CLI design — command surface, Typer app structure, output contract,
error UX, template discovery — lives in `peripheral-cli.md`. This file
ensures the library DX underneath it (`run()`, string keys, error
codes) exists so the CLI is a thin wrapper and not a parallel
codepath.

The zero-install promise (`uvx easycat init my-agent` working on a
clean machine) is owned by `peripheral-cli.md`. This file's
contribution is keeping the library wheel small and free of
build-from-source deps.

## `easycat init` Template Content

The CLI-side template catalog (discovery, scaffolding, non-interactive
`--config` schema) lives in `peripheral-cli.md`. This section owns the
*content* each template generates — specifically, that every generated
`agent.py` is short enough to serve as the visible proof of the
library DX work in this file.

Template set:

- `openai-agents` (default)
- `pydantic-ai`
- `pydantic-ai-workflow`
- `twilio-phone`
- `webrtc-browser`
- `text-chat` (text-mode session for REPL-style testing of agent
  changes without audio infrastructure)

Voice-to-voice / realtime speech-to-speech templates are explicitly
out of scope — EasyCat is a chained voice runtime, see the "Chained
Only" rationale in `../roadmap/essential-debug-first-runtime.md`.

Template `agent.py` line budgets match the shipped scaffold caps:
`openai-agents` ≤16, `pydantic-ai` ≤17, `pydantic-ai-workflow` ≤20,
`text-chat` ≤17, `twilio-phone` ≤15, and `webrtc-browser` ≤14. Each
template ships with a concrete working tool or workflow instead of a
blank TODO; MCP server wiring is generated when requested rather than
forced into every starter. CI regression-tests line count and startup
success. The direct-agent generated-project path is:

```bash
uvx easycat init my-agent
cd my-agent
cp .env.example .env
uv sync
uv run easycat doctor --env-file .env
uv run --env-file .env python agent.py
```

`twilio-phone` runs through:

```bash
uv run --env-file .env uvicorn server:create_app --factory --host 0.0.0.0 --port 8000
```

## Config Factory Presets

- `EasyConfig.mic(...)` — shipped local microphone preset.
- `EasyConfig.browser(...)` — shipped WebRTC/browser preset.
- `EasyConfig.phone(...)` — shipped Twilio/PSTN preset.
- `create_text_session(TextSessionConfig(...))` — shipped text-mode
  path with no audio provider wiring.
- `EasyConfig.offline(...)` — still planned.

Match the `easycat init` template set so users can graduate from
scaffolded code to explicit config without a rewrite.

## Offline Preset

The strongest possible ease-of-use pitch is "git clone → working voice
agent with zero API keys." 2026 makes this viable: Kyutai Pocket TTS
(100M params, CPU real-time, Jan 2026) and Whisper-small run locally on
any laptop; the bundled Smart Turn v3.2 model runs on CPU.

```python
run(EasyConfig.offline(
    agent=Agent(name="Support", instructions="Help the user."),
))
```

Semantics:

- STT: Whisper-small via `faster-whisper`
- TTS: Kyutai Pocket TTS
- Turn detection: Smart Turn v3.2
- First-run downloads ~350MB to `~/.cache/easycat/models/` with a
  progress bar; cached forever after
- First-run message: "Downloading models for offline mode (~350MB). This
  only happens once."
- The agent framework's own model access remains the one required key —
  unless a local-LLM-compatible agent is passed, in which case zero keys.

The nuclear "I just want to see it work" option. Depends on the Smart
Turn v3.2 promotion tracked in `peripheral-provider-ecosystem.md`.

## Error Diagnostics

Current error story: exceptions get `repr()`'d into an event payload and
logged via `logger.exception()`. Not enough. Adopt the modern stack:

**Stable error codes**

Every EasyCat error gets a stable ID (`EASYCAT_E042`) with an `easycat
explain E042` CLI that dumps the full doc. Rust `cargo --explain` pattern.

**First-person fix-suggesting messages**

```
EASYCAT_E012: I couldn't find a TTS provider named 'elvenlabs'.
  Did you mean 'elevenlabs'?
  Configured in: my_agent.py:14
  Available providers: elevenlabs, openai, deepgram, cartesia
  Run `easycat explain E012` for details.
```

Tone follows Elm and Rust compiler errors: first-person, point to the
exact spot, suggest a fix, link to deeper docs. Fuzzy matching on typos.

**`ExceptionGroup` for pipeline failures**

EasyCat uses asyncio TaskGroups for parallel pipeline work. When STT and
TTS both fail in a single turn, current code loses one error. Use PEP 654
`ExceptionGroup` (and `except*`) so every pipeline failure surfaces as a
grouped tree.

**PEP 678 exception notes**

Annotate every pipeline exception with `__notes__` carrying `turn_id`,
`stage`, `elapsed_ms`, `sequence`, and the journal record key that
captured the failing input. Python 3.11+ renders notes inline in
tracebacks for free.

**Collapse third-party frames**

Tracebacks default to hiding frames inside `openai`, `pydantic_ai`,
`asyncio`, `anyio`, and other third-party packages. Show only `easycat/*`
and user code. `EASYCAT_DEBUG=1` or `--verbose` expands the full stack.
Next.js overlay pattern.

**Dev vs prod log rendering**

Single logger, branch on `sys.stderr.isatty()` or `EASYCAT_ENV`:

- Dev: `structlog.dev.ConsoleRenderer` with colors, pretty tracebacks,
  inline locals, emoji status lines
- Prod: `structlog.processors.JSONRenderer` for structured log pipelines

Same event names, same keys in both modes — only the renderer differs.

**Runtime log-level env var**

`EASYCAT_LOG_LEVEL=debug` tweaks verbosity without code changes, matching
the `LIVEKIT_LOG_LEVEL` / `UVICORN_LOG_LEVEL` convention. Lives alongside
`EASYCAT_ENV=dev|prod`.

## Config Audit and Flattening

`EasyConfig` currently has 22 dataclass fields, including inherited
agent/runtime fields. The flattening slices grouped audio input knobs under
`AudioProcessingConfig`, debug/journal knobs under `ObservabilityConfig`, and
conversation/telephony policy knobs under `SessionPolicyConfig` while keeping
legacy top-level aliases working. Real complexity remains in nested surfaces:
`TelephonyConfig`, `TurnManagerConfig`, and `SmartTurnConfig`. Group the
low-frequency knobs behind sensible defaults, keep common knobs obvious, and
avoid adding new required fields. Every new config field must have a default
that keeps the quickstart working.

Runtime/debug presets are shipped as `debug="light"` and `debug="full"`.
Always-on recording is shipped as `record_to=...` when debug journaling is
enabled.

Candidate advanced toggles should remain available through config, not
low-level internals:

- `debug="light" | "full"`
- `record_to=...`
- `redaction_policy=...`
- `mode="local" | "webrtc" | "telephony"`
- `runtime_mode="chained_pipeline" | "text_session"`
- `smart_turn=True` with `smart_turn_sensitivity=0.5` (shipped)
- `backchannel_filter=True`
- `latency_budget=LatencyBudget(...)`
- `warmup=True`
- `mcp_servers=[...]`
- `max_session_cost_usd=0.50`

## Quickstart Guardrails

Reject any redesign change that violates these:

- The simplest OpenAI Agents or PydanticAI example exceeds its line budget.
- A new runtime feature requires a new required config field.
- Users must wire stages directly to get started.
- Debugging requires custom subscription code or a separate example app.
- Users must learn new EasyCat-native agent concepts before shipping.
- `debug=True` does not produce immediately useful output.
- Swapping STT, TTS, or LLM providers requires more than a single string
  change.
- The scaffolded `easycat init` project does not run with a single API key.

## Dependencies on the Essential Plan

| Item | Depends on |
|---|---|
| Line budgets, `run()`, `async with`, string keys | nothing |
| Error codes, dev/prod log rendering, `EASYCAT_LOG_LEVEL` | nothing |
| Config factory presets, `EasyConfig` flattening | nothing |
| Template content (what `agent.py` looks like) | `run()`, string keys (this file) |
| Offline preset | Smart Turn promotion (see `peripheral-provider-ecosystem.md`), string-keyed providers, config factory presets |

The CLI's own dependency table (init, doctor, explain, bundles,
replay) lives in `peripheral-cli.md`. Library-wrapper commands
(`run`, `dev`, `test`, `cost`) are deferred by that plan.

## Suggested Sequencing

1. **In parallel with essential Phase 1-2**: quickstart helpers
   (`run()`, `async with session`, string-keyed providers). These
   don't touch the journal or bridge and deliver visible line-count
   wins early. Also: error codes, log rendering, `EASYCAT_LOG_LEVEL`,
   config factory presets.
2. **Template content**: lands in lockstep with `peripheral-cli.md`
   M1 and M2, because each template's `agent.py` must import the
   library DX helpers from this file.
3. **Last**: offline preset (gated on Smart Turn promotion in the
   provider ecosystem file), final `EasyConfig` flattening pass.

The CLI-facing sequencing (M1–M3 milestones for `init`, `doctor`,
`explain`, `bundles`, `replay`) lives in `peripheral-cli.md`.

## Competitive Context

- **Pipecat**: `uv tool install pipecat-ai-cli` + `pipecat init
  quickstart` is now the official onboarding path with interactive
  prompts and `--config` JSON for non-interactive scaffolding.
- **LiveKit Agents 1.5** (March 2026): `watchfiles`-based hot reload in
  `lk agent dev`, `LIVEKIT_LOG_LEVEL` env var for runtime verbosity.
- **LangSmith Fetch CLI + Polly** (Dec 2025): defined "pipe traces into
  the user's coding agent" pattern. RunBundle-like data goes straight
  into Claude Code or Cursor instead of a separate web dashboard.
- **Kyutai Pocket TTS** (Jan 2026): 100M params, CPU real-time, Apache
  2.0 — makes the zero-key offline preset viable.
- **vLLora** (vllora.dev): pipeline-stage debugging for LiveKit agents.
  Validates the coding-agent-first debugging flow as a real market need.
