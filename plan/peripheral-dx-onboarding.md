# DX and Onboarding — Peripheral

> **This is a peripheral initiative.** It is not essential to the
> debug-first thesis in `essential-debug-first-runtime.md`. It is,
> however, the most visible user-facing work in the overall redesign, and
> the one the outside world will judge EasyCat on first.
>
> **Sibling peripheral docs:**
>
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
> selection, env var auto-detection, `easycat` CLI (`init`, `doctor`,
> `run`, `dev`, `explain`, `cost`, `test`, `bundles`, `bundle export`,
> `replay` command surface), template catalog, config factory presets,
> offline preset, error diagnostics (stable codes, fix-suggesting
> messages, `ExceptionGroup`, exception notes, traceback frame collapse,
> dev vs prod log rendering), `EasyCatConfig` flattening, quickstart
> guardrails.

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

Beat the bar Pipecat (`uv tool install pipecat-ai-cli`) and LiveKit
(`lk agent init`) set in 2026. The CLI is installed with the package,
wraps the same `create_session()` API, and is invokable via `uvx` with
**no prior install step**:

```bash
# Zero to running in under 60 seconds — one command, no prior install
uvx easycat init my-agent               # scaffold: one file, one .env, one README
cd my-agent && uv sync
uvx easycat doctor                      # verify env, providers, ONNX, mic
uvx easycat run agent.py                # runs the agent, handles shutdown
uvx easycat dev agent.py                # same + launches the debugger UI on :8765
uvx easycat dev --reload agent.py       # auto-restart on file change
uvx easycat replay bundle.zip           # local repro of a production failure
uvx easycat replay bundle.zip --fork-at cp_87  # fork from checkpoint cp_87
uvx easycat explain E012                # Rust-style error lookup
uvx easycat cost --since yesterday      # cost rollup across recent sessions
uvx easycat bundles list                # discover crash-recovered bundles
uvx easycat bundle export --for=claude-code bundle.zip  # trace pack for coding agents
uvx easycat test                        # run pytest with easycat.testing plugin loaded
```

Users who prefer a long-lived install get the same commands via `uv tool
install easycat` → bare `easycat ...`. The promise is that the *first*
invocation requires nothing but `uv`.

**Non-interactive scaffolding**: `easycat init` accepts a `--config` JSON
flag so Claude Code, Cursor, and Codex can scaffold EasyCat projects
without human prompts.

```bash
uvx easycat init my-agent --config '{"template":"openai-agents","provider":"deepgram-flux"}'
```

Three commands deserve special callouts:

- **`easycat doctor`** runs the first-run diagnostic flight every FastAPI,
  uv, and Pydantic user now expects. It verifies `OPENAI_API_KEY` /
  `DEEPGRAM_API_KEY` / etc. are present, hits provider endpoints with a
  200ms HEAD request, checks that `onnxruntime` is importable when Smart
  Turn is requested, and probes the default microphone device when
  transport is `local`. It prints a color-coded report with specific
  fix suggestions tied to `EASYCAT_Exxx` codes. This is the single most
  effective drop-off killer for voice-agent onboarding.

- **`easycat dev --reload`** runs the agent under a file watcher and
  reloads the agent module on change. LiveKit Agents 1.5 ships
  `watchfiles`-based reload in `lk agent dev`, so reload itself is not
  novel in 2026 — it is table stakes. What is distinctive is the *swap
  semantics*: EasyCat's bridge boundary lets the reload happen **without
  dropping the microphone, transport, debugger UI, or session journal**.
  On file change: cancel any in-flight turn through the bridge's
  cancellation contract, reimport the agent module, rebuild only the
  bridge, leave every other stage running, write a `CodeReloaded`
  checkpoint into the journal. Bridge boundary makes this ~100 lines.

- **`easycat bundle export --for=claude-code`** packages a RunBundle into
  a context pack that Claude Code, Cursor, or Codex can load directly —
  a few plain-text files with the journal timeline, the failing turn's
  artifacts, and suggested fix-code locations. Modeled on LangSmith Fetch
  (Dec 2025). The interactive web debugger in
  `peripheral-eval-and-debugger-ui.md` still exists for exploration, but
  `--for=claude-code` is the path most users will actually take.

## `easycat init` Templates

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

Every template ≤ 15 lines, ships with one MCP server wired up (the
official `filesystem` server), runs with a single API key. CI regression-
tests line count and startup success. `uvx easycat init my-agent && cd
my-agent && uv sync && uvx easycat run agent.py` must succeed end-to-end
under 60 seconds in CI.

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
| `easycat init`, `doctor`, `explain`, `run`, `test` | essential Phase 2 (bridge) for `init --config` correctness |
| `easycat dev` (launcher + browser open) | essential Phase 3 (stage refactor) |
| `easycat dev --reload` swap semantics | essential Phase 2 (bridge boundary) |
| `easycat bundles list`, `bundle export --for=claude-code` | essential Phase 4 (`RunBundle` + crash-durable journal) |
| `easycat replay --fork-at` | forked replay (see `peripheral-eval-and-debugger-ui.md`) |
| Offline preset | Smart Turn promotion (see `peripheral-provider-ecosystem.md`), string-keyed providers, config factory presets |

## Suggested Sequencing

1. **In parallel with essential Phase 1-2**: quickstart helpers (`run()`,
   `async with session`, string-keyed providers, env autodetect). These
   don't touch the journal or bridge and deliver visible line-count wins
   early. Also: error codes, log rendering, `EASYCAT_LOG_LEVEL`.
2. **After essential Phase 2**: `easycat` CLI (`init`, `doctor`,
   `explain`, `run`, `test`). Bridge is the dependency for `init
   --config` scaffolding to produce a working project.
3. **After essential Phase 3**: `easycat dev` with in-process swap
   reload.
4. **After essential Phase 4**: `easycat bundles list`, `bundle export
   --for=claude-code`.
5. **Last**: offline preset (gated on Smart Turn promotion in the
   provider ecosystem file), final `EasyCatConfig` flattening pass.

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
