# Evaluation and Debugger UI — Peripheral

> **This is a peripheral initiative.** It is not essential to the
> debug-first thesis in `essential-debug-first-runtime.md`. The essential
> plan ships journal-backed debugging, bundle export, and three replay
> fidelity classes (artifact / simulated / live). This file is about
> everything that builds on top of those primitives: automated evals,
> time-travel forking, and interactive debugging surfaces.

## Status (2026-04)

Shipped:

- Interactive debugger UI — journal-driven aiohttp server on port 8765
  with live pipeline graph, record inspector, per-stage latency
  waterfall, cost panel, synchronized audio playback, transcript view,
  and Replay from UI (`src/easycat/debugger/server.py`,
  `src/easycat/debugger/static/index.html`). Browser auto-launches on
  server start.
- Bundle-as-fixture loading for pytest (`load_bundle()` in
  `src/easycat/debug/testing.py`).
- `CommittableCheckpoint` metadata (`debug/bundle.py`) — the data
  structure for forked replay exists even though the fourth fidelity
  class does not yet consume it.
- Three replay fidelity classes: `ARTIFACT`, `SIMULATED`, `LIVE`
  (`runtime/replay.py`).
- Low-level testing assertion helpers on journal records:
  `assert_exact_match`, `assert_regex`, `assert_turn_completed`,
  `assert_no_error` (`src/easycat/debug/testing.py`).
- Pytest plugin registration under `[project.entry-points."pytest11"]`
  exposing the bundle fixture out of the box.
- Auto-launch of the debugger UI when `debug="full"` is set
  (`config.py` glue into `debugger.server.serve_in_background()`).
- `EasyCatConfig(record_to=".easycat/recordings/")` — session
  auto-captures a timestamped bundle on stop/shutdown (`config.py`
  `_install_record_to_hook`).
- `checkpoint_id` (`cp_<sequence>`) vocabulary helpers —
  `easycat.debug.bundle.checkpoint_id`, `parse_checkpoint_id`,
  `CommittableCheckpoint.checkpoint_id`, and
  `RunBundle.lookup_by_checkpoint_id`. The debugger UI and
  `easycat bundles show` both render the new id.

Not started:

- Persona-driven Simulator + Judge (Vapi Evals / Hamming / Coval
  pattern). LLM-as-judge helper (`assert_llm_judge`) is not wired.
- `forked_replay` fourth fidelity class and the matching "Fork from
  here" UI button. Depends on the bridge execution cursor work in the
  essential plan.
- Terminal ASCII dev waterfall (`[vad 12ms][stt 340ms…]`) with
  Rich `Live` in-flight rendering and budget markers.
- Auto-reload via `easycat.run(..., reload=True)` and the
  `CodeReloaded` checkpoint divider it would write.

The interactive debugger is the headline piece that landed early; the
testing surface is the obvious next priority because it unblocks the
"ship a fix with a regression test in one PR" flow the bundle-as-
fixture design was built for.

>
> **Sibling peripheral docs:**
>
> - `peripheral-dx-onboarding.md` — library DX: line budgets, `run()`,
>   `async with`, string-keyed providers, template content, error
>   diagnostics
> - `peripheral-cli.md` — `easycat` CLI (scaffolding + journal
>   debugging). Exposes `bundles export` and `replay` as entry points
>   into features in this file. `easycat test` (pytest wrapper) and
>   `easycat dev` (dev loop) are deferred by that plan; the pytest
>   plugin and debugger UI below still ship and are driven directly.
> - `peripheral-redaction.md` — `RedactionPolicy` write filter, safe
>   snapshots, export-time redaction pass, ready-to-use policies
> - `peripheral-provider-ecosystem.md` — Deepgram Flux, Smart Turn
>   promotion, backchannel filter
> - `peripheral-observability-and-cost.md` — OTel export, cost modeling,
>   latency budgets, warmup stage
>
> **In scope (this file):** `easycat.testing` module (journal-based
> pytest fixtures, bundle-as-fixture loading, three validation methods,
> behavioral assertions), persona-driven Simulator, Judge, simulation-
> first mode, `forked_replay` fidelity class, LangGraph checkpoint
> vocabulary for user-facing replay, interactive web debugger UI, dev
> waterfall terminal output, "fork from here" UI button.

## Context

Once the essential plan lands, EasyCat has a journal that captures
every stage boundary, a bundle format that can export crashed sessions,
and three replay classes. That is enough to answer "what happened and
can I replay it" locally. It is not enough to:

- **Catch voice regressions automatically.** Voice regressions are
  spectral, not binary — a prompt tweak can move booking completion by
  double digits without any assertion failing (Hamming AI's data across
  4M+ calls, Coval's simulation-first position for voice agents).
  Traditional pass/fail tests miss this entirely.
- **Run "what if I changed this prompt mid-turn" debugging.** The base
  replay classes rewind from a captured input; they do not fork and
  continue live from an arbitrary point in history.
- **Give developers an interactive view of a session as it happens.**
  The CLI surface in `peripheral-cli.md` and the `--for=claude-code`
  bundle export cover the "I'm already in a coding agent" flow. Some
  debugging is genuinely exploratory and wants a timeline
  visualization.

This file owns all three.

## `easycat.testing` Module

Ship `easycat.testing` as part of core (LiveKit 1.0 pattern), not a
separate package. Voice regressions need a dedicated testing surface
with voice-specific primitives.

### Core Pieces

- **Journal-based assertions and fixtures for pytest** — load a
  `RunBundle` as a fixture and assert against its records directly.
- **Bundle-as-fixture loading** — production failures promoted directly
  into regression tests, no adaptation layer. Nobody else ships this
  yet. A production failure captured as a `RunBundle` can land as a
  pytest fixture in the same PR that fixes it.
- **Three validation methods** matching Vapi Evals and the Hamming /
  Coval consensus:
  - `assert_exact_match` (deterministic)
  - `assert_regex` (flexible)
  - `assert_llm_judge` (semantic)
  All three read from journal records so test authors never touch audio.
- **Behavioral assertions** over transitions, tool usage, interruptions,
  and latency.
- **Per-stage latency budget assertions** tied to the Latency Budget
  table in `peripheral-observability-and-cost.md`: STT TTFT, LLM TTFT,
  TTS TTFB, E2E P50/P90.
- **LLM-as-judge assertion helpers**: `assert_turn_completed`,
  `assert_no_semantic_regression`, `assert_intent_matched`. The Hamming
  two-step pipeline achieves 95–96% agreement with human evaluators.

### Persona-Driven Synthetic Caller

Ship two cooperating LLM roles, not one (Voicetest / Vapi Evals
pattern):

- **Simulator** plays a configurable user persona (`patient caller`,
  `impatient interrupter`, `heavy accent`, `angry complainant`) and
  drives a multi-turn conversation toward a goal, deciding autonomously
  when the goal is met or unreachable.
- **Judge** scores the resulting transcript against success criteria
  independently of the Simulator.

The separation matters: one model cannot reliably play both the user
and the evaluator without persona bleed. All three inputs (persona,
goal, success criteria) are plain text in a fixture file so
non-engineers can contribute test cases. Scripted line-by-line user
turns are still supported as a degenerate one-persona case for
deterministic regression.

### Simulation-First Mode

Replay a bundle fixture against current runtime code to detect latency
regressions, prompt drift, and interruption behavior changes without
running live providers (Coval pattern). Ties together the essential
plan's `artifact_replay` fidelity class with the latency budget
assertions in `peripheral-observability-and-cost.md`.

### `easycat.testing` pytest plugin

The pytest plugin is the primary surface. Users register it in their
`pyproject.toml` under `[tool.pytest.ini_options]` (scaffolded
templates do this for the user) and run `pytest` directly. A
dedicated `easycat test` CLI wrapper is deferred by
`peripheral-cli.md` — the plugin carries its own weight without one.

## Forked Replay / Time-Travel

### `forked_replay` Fidelity Class

Adds a fourth replay class on top of the three in the essential plan.
Replay deterministically up to a chosen `checkpoint_id`, then switch to
live execution against (optionally) modified code. The "what if I
changed this prompt mid-turn" debugging mode validated by the
`agent-replay` project, LangGraph time-travel, and Block's Goose.

Fidelity:

- Deterministic before the fork point.
- Non-deterministic after the fork point.
- Fork point must be a **committable checkpoint**, not an arbitrary
  journal record. Forking mid-LLM-stream would leave the bridge in an
  inconsistent state. The CLI and API refuse to fork there and point
  the user at the nearest committable checkpoint.

### LangGraph Checkpoint Vocabulary

Adopt LangGraph's user-facing vocabulary for replay. Internally the
journal still uses monotonic `sequence` numbers, but externally users
see `checkpoint_id` (e.g., `cp_87`) — the same concept shape as
`get_state_history()` / `update_state()` that every LangGraph user
already knows. Free naming win; diverging forces users to learn a
second vocabulary for the same primitive.

A checkpoint is a committable boundary in the bridge execution cursor
(from the essential plan), not every journal record.

### CLI and UI Surfaces

- `easycat replay bundle.zip --fork-at cp_87` from the terminal (CLI
  command surface lives in `peripheral-cli.md`; this file owns the
  fork semantics).
- "Fork from here" button in the interactive debugger UI below.

### What Counts as a Fork Boundary

The fork-replay plan (implementation proposal) must specify what counts
as a committable fork boundary in each bridge:

- **OpenAI Agents**: committed handoffs, between-turn boundaries, the
  start/end of a tool-call unit. Not mid-stream during a response.
- **PydanticAI**: `iter()` node boundaries where message history is
  consistent, between tool calls, at workflow specialist transitions.
  Not mid-model-request.

Forking inside an uncommittable region returns a clear error pointing
at the nearest safe checkpoint.

## Interactive Debugger UI

EasyCat has a structural advantage over every existing voice debugger:
the journal is one store, so the debugger is just a reader, not a
separate telemetry pipeline. The 2026 reality is that the CLI surface
is used far more than the GUI:

1. **`easycat bundles export --for=claude-code`** is the primary
   debugging flow. Most users in 2026 debug by piping trace data into
   their coding agent. CLI command lives in `peripheral-cli.md`.
2. **Interactive web debugger** is secondary, for exploratory
   debugging. Ships here.

The split is deliberate: a Textual / web dashboard is the wrong bet to
make the *centerpiece* of peripheral work when LangSmith Fetch, Claude
Code, and Cursor have moved debugging into the coding agent. Ship the
interactive debugger because some debugging is genuinely exploratory
and a timeline view is the right tool, but size the investment
accordingly.

### Core Features

Grow out of the current `examples/webrtc_observability_server.py`
(309 lines, disconnected from core), but driven by the journal rather
than ad hoc event subscriptions.

- **Live pipeline graph** — stages as nodes, records flashing through
  them in real time (Pipecat Whisker style, but journal-driven).
- **Record inspector** — click any journal record to see its full
  payload, artifact refs, timing, and upstream/downstream
  correlations.
- **Filter by stage, operation, turn, or error code** — same query
  surface as the pytest plugin.
- **Synchronized audio playback with transcript view** — the feature
  LiveKit Cloud has but no self-hosted framework offers.
- **Per-stage latency waterfall with budget markers** — red where a
  turn blew its Latency Budget from
  `peripheral-observability-and-cost.md`.
- **Cost panel** — per-turn, per-session, per-day rollup from
  `CostRecord`.
- **Save/load sessions as RunBundles** — same format as production
  export.
- **Replay from the UI** with explicit fidelity labels (artifact /
  simulated / live re-execution / forked).
- **"Fork from here" button** — pick any committable journal record and
  reopen a live session at that point with the current agent code. Same
  underlying mechanism as `easycat replay --fork-at`, exposed where
  developers are already looking.

### Server Model

- Single-process server with the debugger UI on a side port (default
  8765).
- No external media server required for development (Pipecat and
  LiveKit both require media infra — real differentiator).
- WebSocket-first, with optional WebRTC upgrade path for production.
- Opens the browser to the debugger automatically on first run.
- `debug="full"` in config auto-launches the debugger UI. No `easycat
  dev` wrapper is required; any agent invoked via `easycat.run(...,
  debug="full")` gets the UI for free.

## Dev Waterfall Output

`debug=True` produces a per-turn ASCII waterfall inline in the
terminal. This is a second rendering of the same journal records the
interactive UI reads — it is included in this file because it is the
lightweight sibling of the web debugger, and the two should ship as
coordinated views.

```
turn #7 (turn_id=8f3a)
  [vad  12ms][stt 340ms████][llm-ttft 180ms██][llm-stream 240ms███][tts-ttfb 95ms█][play 1.2s]
  user: "what's the weather in paris"
  bot:  "It's 14 degrees and cloudy in Paris right now."
  total 2.07s  |  $0.0042 (142 in / 88 out, stt 1.4s, tts 52ch)
```

Critical details:

- **Named stage bars**: `vad → stt → llm-ttft → llm-stream → tts-ttfb → play`.
  Each is a journal record with known timing.
- **Budget marker**: if a configured `LatencyBudget` exists, draw a
  vertical marker in the waterfall where the budget line falls. The
  developer instantly sees which turn blew the budget.
- **Critical path highlighting**: color the span that dominates the
  turn's wall clock (hot path).
- **Cost triple inline**: STT seconds / LLM tokens / TTS characters →
  USD. Voice apps have three cost centers, not one.
- **Live rendering**: Logfire-style in-flight spans that tick up in
  real time using Rich `Live`. The developer sees the LLM stage
  advance as the model streams.

The waterfall is derived entirely from the journal — same records as
the web debugger, rendered for the terminal.

## Dev Loop Features

A dedicated `easycat dev` command is deferred by `peripheral-cli.md`.
The two library-level features below surface debugger-UI behavior
from this file, both driven by `EasyCatConfig` rather than a
dedicated CLI command:

- **Auto-reload** — a library helper (`easycat.run(..., reload=True)`
  or an `async with session.autoreload():` block) watches for file
  changes and swaps the agent module in-process via the bridge
  boundary. The debugger timeline writes a `CodeReloaded` checkpoint
  that shows up as a visual divider in the UI.
- **Session recording** — `EasyCatConfig(record_to=".easycat/recordings/")`
  automatically captures every session as a timestamped bundle,
  retained for seven days. Makes "wait, what just happened?"
  debugging one command away — no more losing a surprising behavior
  because the session ended before the developer thought to export.

## Dependencies on the Essential Plan

| Item | Depends on |
|---|---|
| `easycat.testing` pytest fixtures, assertions | essential Phase 4 (`RunBundle` + replay classes) |
| Bundle-as-fixture loading | essential Phase 4 |
| Three validation methods, LLM-as-judge helpers | essential Phase 1 (journal records), Phase 4 (bundle loading) |
| Simulator + Judge | essential Phase 2 (bridge) to run the agent under test |
| Simulation-first mode | essential Phase 4 (`artifact_replay`) |
| `forked_replay` fidelity class | essential Phase 4 (replay contract) |
| `checkpoint_id` vocabulary | bridge execution cursor (Phase 2), journal sequence (Phase 1) |
| Interactive debugger UI | essential Phase 3 (stages) + Phase 4 (bundle/replay) |
| Dev waterfall output | essential Phase 1 (journal records) + `CostRecord` + `LatencyBudget` from observability file |
| "Fork from here" button | `forked_replay` + UI |
| Session recording (`record_to=...`) | Phase 4 (`RunBundle`) |
| `CodeReloaded` checkpoint in UI timeline | auto-reload swap semantics from `peripheral-dx-onboarding.md` |

## Suggested Sequencing

1. **After essential Phase 1**: dev waterfall output ships as the
   lightweight rendering of journal records. Requires `CostRecord` and
   `LatencyBudget` from `peripheral-observability-and-cost.md` to be in
   flight, but degrades gracefully if they aren't yet.
2. **After essential Phase 3**: interactive debugger UI starts taking
   shape alongside the stage refactor — stages become graph nodes.
3. **After essential Phase 4**: `easycat.testing` module, Simulator +
   Judge, simulation-first mode, `forked_replay` fidelity class. All
   four depend on the replay contract being stable.
4. **Last**: "fork from here" button in the debugger UI, `record_to=`
   session auto-capture, `CodeReloaded` timeline divider integration
   with the library-level auto-reload swap semantics from the DX
   file.

## Competitive Context

- **LangSmith Fetch CLI + Polly** (Dec 2025): defined the "pipe traces
  into the coding agent, not a dashboard" pattern. This is why the
  interactive debugger is positioned as *secondary* to
  `--for=claude-code` in the DX file.
- **LangGraph time-travel** (2025/2026): `get_state_history()`,
  `update_state()`, checkpoint IDs are the settled API shape for replay
  and fork across the agent ecosystem. EasyCat's `RunBundle` replay
  should speak "checkpoint" rather than invent "journal sequence" as a
  user-facing concept.
- **Block's Goose**: exploring time-travel debugging with session
  replay/rewind for AI agent workflows (GitHub issue, Jan 2026),
  confirming industry interest in the replay-from-boundary concept.
- **agent-replay project** and **`AgentStreamRecorder` patterns**:
  "record mode" captures a full run, "replay mode" feeds captured
  artifacts back through live code, "fork mode" replays up to a sequence
  number and then resumes live against new code. EasyCat's replay
  models all three.
- **LiveKit Cloud Agent Observability**: synchronized audio +
  transcript + traces + per-stage latency in one timeline. EasyCat's
  equivalent is self-hosted and free.
- **Pipecat Whisker**: live pipeline graph, frame flashing, record
  inspection, session save/load. The inspiration for the interactive
  debugger's pipeline view.
- **Voicetest, Vapi Evals, Hamming AI, Coval**: persona-driven
  simulation + LLM-as-judge as the 2026 standard for voice regression
  testing. Hamming's two-step pipeline achieves 95–96% agreement with
  human evaluators across 4M+ calls.
- **vLLora** (vllora.dev): pipeline-stage debugging for LiveKit
  agents — validates the ExecutionJournal + debug bundle approach as a
  real market need.
