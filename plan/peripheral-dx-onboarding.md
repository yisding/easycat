# DX and Onboarding — Peripheral

> **This is a peripheral initiative.** It is not essential to the
> debug-first thesis in `essential-debug-first-runtime.md`. It is,
> however, the most visible user-facing work in the overall redesign, and
> the one the outside world will judge EasyCat on first.
>
> **Sibling peripheral docs:**
>
> - `peripheral-cli.md` — first-class `easycat` CLI design (Typer app,
>   command surface, output contract, templates, error UX, `uvx`
>   zero-install guarantee). This file owns the library DX the CLI
>   wraps; that file owns the CLI product.
> - `peripheral-redaction.md` — `RedactionPolicy` write filter, safe
>   snapshots, export-time redaction pass, ready-to-use policies
> - `peripheral-provider-ecosystem.md` — Deepgram Flux, Smart Turn v3.1
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
> dev vs prod log rendering), `EasyCatConfig` flattening, quickstart
> guardrails.
>
> **Out of scope:** the `easycat` CLI command catalog, `--help`
> taxonomy, exit-code contract, `uvx` packaging — all owned by
> `peripheral-cli.md`.

## Context

Today's simplest examples are 47–62 lines (`examples/local_chat.py` 47,
`examples/pydantic_ai_voice.py` 62). LiveKit and Pipecat both shipped
one-command scaffolding in 2026. The single most important success signal
for onboarding is: `git clone` → working voice agent under 60 seconds with
one API key.

This file owns closing that gap. None of its contents is required to
deliver the debug-first thesis. All of it is required for EasyCat to hold
its own against Pipecat's `pipecat-ai-cli` and LiveKit's `lk agent init`
in 2026.

## Line-Count Budgets on Canonical Examples

Hard ceilings measured against real files in `examples/`, CI-enforced:

- `examples/local_chat.py` (OpenAI Agents, local mic): **≤ 7 lines** (from 47)
- `examples/pydantic_ai_voice.py` (PydanticAI, local mic): **≤ 8 lines** (from 62)
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
# examples/local_chat.py — 7 lines
from easycat import EasyCatConfig, run
from agents import Agent

run(EasyCatConfig(
    agent=Agent(name="Support", instructions="Help the user."),
))
```

**`async with session:`**

Context manager support matching `httpx.AsyncClient`, `asyncpg`, and
`anyio.TaskGroup`. For users who already have an asyncio loop:

```python
# 10 lines
from easycat import EasyCatConfig, create_session
from agents import Agent

async def main():
    session = create_session(EasyCatConfig(
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
run(EasyCatConfig(
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
ensures the library DX underneath it (`run()`, string keys, env
autodetect, error codes) exists so the CLI is a thin wrapper and not a
parallel codepath.

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
Only" rationale in `essential-debug-first-runtime.md`.

Every template's `agent.py` ≤ 15 lines, ships with one MCP server
wired up (the official `filesystem` server), runs with a single API
key. CI regression-tests line count and startup success. `uvx easycat
init my-agent && cd my-agent && uv sync && uv run python agent.py`
must succeed end-to-end under 60 seconds in CI.

## Config Factory Presets

- `EasyCatConfig.phone(...)`
- `EasyCatConfig.browser(...)`
- `EasyCatConfig.mic(...)`
- `EasyCatConfig.text(...)` — text-mode session, no audio provider
  wiring
- `EasyCatConfig.offline(...)`

Match the `easycat init` template set so users can graduate from
scaffolded code to explicit config without a rewrite.

## Offline Preset

The strongest possible ease-of-use pitch is "git clone → working voice
agent with zero API keys." 2026 makes this viable: Kyutai Pocket TTS
(100M params, CPU real-time, Jan 2026) and Whisper-small run locally on
any laptop; Smart Turn v3.1 runs on CPU.

```python
run(EasyCatConfig.offline(
    agent=Agent(name="Support", instructions="Help the user."),
))
```

Semantics:

- STT: Whisper-small via `faster-whisper`
- TTS: Kyutai Pocket TTS
- Turn detection: Smart Turn v3.1
- First-run downloads ~350MB to `~/.cache/easycat/models/` with a
  progress bar; cached forever after
- First-run message: "Downloading models for offline mode (~350MB). This
  only happens once."
- The agent framework's own model access remains the one required key —
  unless a local-LLM-compatible agent is passed, in which case zero keys.

The nuclear "I just want to see it work" option. Depends on the Smart
Turn v3.1 promotion tracked in `peripheral-provider-ecosystem.md`.

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

`EasyCatConfig` currently has 22 top-level fields, each pointing into a
nested config. Real complexity is in nested surfaces: `TelephonyConfig`,
`TurnManagerConfig`, `SmartTurnConfig`. Flatten the most common knobs to
top level, hide the rest behind sensible defaults. Every new config field
must have a default that keeps the quickstart working.

Add runtime/debug presets: `debug="light"`, `debug="full"`.

Advanced toggles remain available through config, not low-level internals:

- `debug_mode="light" | "full"`
- `export_debug_bundle=True`
- `redaction_policy=...`
- `mode="local" | "webrtc" | "telephony"`
- `runtime_mode="chained_pipeline" | "text_session"`
- `smart_turn=True` with `smart_turn_sensitivity=0.5`
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
| Line budgets, `run()`, `async with`, string keys, env autodetect | nothing |
| Error codes, dev/prod log rendering, `EASYCAT_LOG_LEVEL` | nothing |
| Config factory presets, `EasyCatConfig` flattening | nothing |
| Template content (what `agent.py` looks like) | `run()`, string keys (this file) |
| Offline preset | Smart Turn promotion (see `peripheral-provider-ecosystem.md`), string-keyed providers, config factory presets |

The CLI's own dependency table (init, doctor, explain, bundles,
replay) lives in `peripheral-cli.md`. Library-wrapper commands
(`run`, `dev`, `test`, `cost`) are deferred by that plan.

## Suggested Sequencing

1. **In parallel with essential Phase 1-2**: quickstart helpers
   (`run()`, `async with session`, string-keyed providers, env
   autodetect). These don't touch the journal or bridge and deliver
   visible line-count wins early. Also: error codes, log rendering,
   `EASYCAT_LOG_LEVEL`, config factory presets.
2. **Template content**: lands in lockstep with `peripheral-cli.md`
   M1 and M2, because each template's `agent.py` must import the
   library DX helpers from this file.
3. **Last**: offline preset (gated on Smart Turn promotion in the
   provider ecosystem file), final `EasyCatConfig` flattening pass.

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
