# Observability Export and Cost — Peripheral

> **This is a peripheral initiative.** It is not essential to the
> debug-first thesis in `essential-debug-first-runtime.md`. The essential
> plan's journal already answers "what happened in this turn and can I
> replay it" without any of this file. This file is about *projecting*
> journal records into production-monitoring backends and about
> attaching cost and latency facts to the same records so they show up
> everywhere (CLI, bundles, debugger UI, OTel backends, CI assertions).
>
> **Sibling peripheral docs:**
>
> - `peripheral-dx-onboarding.md` — line budgets, CLI, templates,
>   helpers, error diagnostics
> - `peripheral-redaction.md` — `RedactionPolicy` write filter, safe
>   snapshots, export-time redaction pass, ready-to-use policies
> - `peripheral-provider-ecosystem.md` — Deepgram Flux, Smart Turn
>   promotion, backchannel filter
> - `peripheral-eval-and-debugger-ui.md` — `easycat.testing`, Simulator
>   + Judge, forked replay, interactive debugger UI, dev waterfall
>
> **In scope (this file):** `CostRecord` attached to `TurnCompleted`,
> `PricingSource` protocol, per-turn/per-session/per-day cost rollups,
> `max_session_cost_usd` budget alerts, `JournalToOTelExporter` with
> `gen_ai.*` semantic conventions and Logfire reuse path, Latency Budget
> table and `LatencyBudget` config object, CI latency assertions,
> `WarmupStage` with per-substage timing records.

## Context

The essential plan's journal captures timing, input, output, and state
per stage, but it does not attach dollars, it does not emit OpenTelemetry
spans, and it does not enforce latency targets. None of those are
required to debug a failure locally, but all three are required to use
EasyCat in production:

- **Cost** is a first-class per-turn fact that belongs in the same record
  store as timing. Chained voice apps have three cost centers (STT
  seconds, LLM tokens, TTS characters), and each one needs to be
  visible per turn.
- **OTel** is how PydanticAI (via Logfire), LiveKit, Langfuse, Datadog,
  and Honeycomb already integrate. A 2026 voice framework that can't
  speak OTel is dead on arrival. But OTel is not a debugging system —
  it's a projection of the journal for monitoring dashboards.
- **Latency budgets** encode the 2026 human-conversation targets that
  EasyCat claims to hit. Without CI assertions and runtime tagging,
  regressions land silently.

All three are thin projections over the essential journal, plus a
warmup stage that makes cold-start latency measurable.

## Cost Observability

### `CostRecord` Shape

Attach a `CostRecord` to every `TurnCompleted` journal record. The
record models the three chained-pipeline cost centers (STT seconds,
LLM text tokens, TTS characters):

```python
@dataclass(frozen=True)
class CostRecord:
    stt_seconds: float
    stt_cost_usd: float
    llm_input_text_tokens: int
    llm_output_text_tokens: int
    llm_text_cost_usd: float
    tts_characters: int
    tts_cost_usd: float

    total_usd: float
    provider_breakdown: dict[str, float]  # per-provider line items
```

Voice-to-voice / realtime audio-token cost modeling is out of scope —
EasyCat is a chained voice runtime, see the "Chained Only" rationale
and Explicit Guardrails in `essential-debug-first-runtime.md`.

### Pricing Source

Provider pricing changes silently — don't hardcode it. `PricingSource`
protocol with a default JSON file shipped with releases, overridable by
users. Debug bundles capture the pricing source version alongside
provider version strings so replays compute costs at historical rates.

### Rollups at Three Scopes

- **Per turn**: inline in the dev waterfall (`$0.0042`)
- **Per session**: printed when session ends (`Session cost: $0.23, 18 turns`)
- **Per day**: `easycat cost --since yesterday` CLI and debugger UI

### Budget Alerts

`max_session_cost_usd=0.50` emits a warning journal record at 80% and
optionally kills the session at 100%. Kill-switch pattern from Langfuse,
Helicone, Langsmith. Voice apps burn money faster than chat apps
because audio tokens are expensive.

## OTel Export

OTel solves a different problem than debugging. It is designed for
production monitoring dashboards, alerting, and distributed tracing. The
debugging questions EasyCat needs to answer — "what happened in this
turn?", "which stage was slow?", "can I replay this?" — are all answered
by the journal alone.

But a 2026 voice framework that doesn't speak OTel is dead on arrival.
PydanticAI is OTel-native via Logfire. LiveKit emits OTel spans.
Langfuse, Logfire, Datadog, and Honeycomb all speak OTLP.

The reconciliation: **the journal is the debugging source of truth, OTel
is the export format**. Not competing systems. `JournalToOTelExporter`
is a single file that reads journal records and emits spans; it has no
state and no separate mental model.

### Design Principle

- The journal is the only system that matters for debugging.
- `debug=True` works without OTel installed, configured, or thought about.
- OTel never leaks into the debugging mental model.
- OTel export costs near zero to implement on top of a good journal, so
  there is no reason to defer it.

### Implementation

- Ship as optional dep: `easycat[otel]` pulls in `opentelemetry-sdk`.
- Single adapter `JournalToOTelExporter` projects journal records to
  OTel spans using the standardized `gen_ai.*` semantic conventions
  (stabilized March 2026) plus an `io.easycat.*` namespace for voice
  extensions (`io.easycat.stt.ttft_ms`, `io.easycat.tts.ttfb_ms`).
- Auto-detect: if `OTEL_EXPORTER_OTLP_ENDPOINT` is set at startup, the
  projector auto-enables and prints a single line confirming the
  endpoint.
- Logfire one-liner: `logfire.configure()` + EasyCat detects it and
  reuses the exporter. No duplicate span emission.
- **Phoenix-backed CI acceptance test**: run the exporter against a
  local Phoenix sidecar and assert that `gen_ai.*` attributes are
  present on LLM spans. Without this, "OTel export works" silently
  becomes "OTel export works with our custom attribute names" and
  users have to write a translation layer.
- ~200 lines once the journal exists. Avoid the "we'll add OTel later"
  tax that every OTel retrofit pays.

### What This Enables

- Langfuse, Logfire, Datadog, or any OTLP backend integration on day one.
- Pydantic Logfire compatibility for PydanticAI users with zero config.
- Voice-specific spans alongside standard distributed traces.
- One-env-var prod telemetry for users who are not going to read the
  journal docs.

### What This Avoids

- OTel SDK as a *required* dependency (still an optional extra).
- Two mental models for debugging ("is my data in the journal or in
  OTel?").
- Building on immature GenAI semantic conventions as a dependency — the
  projector maps to conventions but does not depend on them being
  stable.

## Latency Budgeting

Human conversation runs on a 200–300ms response window — hardwired
across cultures. Above 800ms, users perceive the other speaker as
having "stopped listening". The 2024 target of "P90 E2E 3.5s" is
obsolete.

### Target Budgets

Verified in journal records and asserted in CI via the eval module in
`peripheral-eval-and-debugger-ui.md`:

| Stage | P50 | P90 | Notes |
|---|---|---|---|
| STT TTFT | < 120ms | < 200ms | Streaming partial, `deepgram/flux` or `deepgram/nova-3` baseline |
| Endpointing | < 30ms | < 80ms | Smart Turn v3.1 (12ms CPU) or Flux native endpointing |
| LLM TTFT | < 250ms | < 400ms | Framework call → first token |
| TTS TTFB | < 60ms | < 120ms | Cartesia Sonic 3 (~90ms TTFA) default; `sonic-turbo` hits ~40ms |
| **E2E (chained)** | **< 1.0s** | **< 1.6s** | user stop → bot start |

Numbers come from 2026 benchmarks published by Inworld, Cartesia,
Speechmatics, Hamming AI, and Daily.co, normalized to the provider
defaults EasyCat ships with.

### `LatencyBudget` Config

A `LatencyBudget` config object lets users tighten or loosen individual
stage budgets. When a turn misses a budget, the waterfall highlights the
offending stage in red and the journal record is tagged with
`budget_exceeded=True` so CI assertions and production alerts fire on
the same signal.

### Cold-Start Caveat

These targets assume provider connections are warm. First-turn latency
on a freshly opened session routinely doubles because WebSocket
handshakes, model downloads, and ONNX runtime loading all bill the
user's first utterance. That's what the warmup stage below is for.

## Warmup Stage

Cold start is the biggest silent latency tax in voice frameworks
today. The first turn after session open routinely runs 1.5–3× slower
than steady state because WebSocket handshakes to Deepgram/ElevenLabs,
ONNX model loading (Smart Turn, Silero VAD), noise-reduction DSP
initialization, and the first LLM token all get billed to the user's
first utterance. This wrecks latency budgets on short sessions (demos,
call-center transfers, one-shot queries) where the first turn *is* the
whole conversation.

### `WarmupStage` Responsibilities

Runs immediately after session construction, before the transport is
armed to emit turns:

- Open STT and TTS provider WebSockets (or equivalents) and complete
  the handshake.
- Load and warm ONNX models (Smart Turn v3.1, Silero VAD) with a
  single dummy inference.
- Run a zero-byte TTS request against the TTS provider to prime the
  TLS session and token bucket.
- Precompile any lazy-imported modules the hot path needs.
- Emit a `WarmupCompleted` journal record with per-substage timing so
  cold-start regressions show up in CI the same way hot-path
  regressions do.

### Defaults and Escape Hatch

- `warmup=True` by default.
- `EasyCatConfig(warmup=False)` skips for batch workloads where the
  first turn does not need to be fast.
- `easycat dev` shows a single line of output while warmup runs so the
  user knows why the first `Listening...` prompt is delayed by a few
  hundred milliseconds.

### Budget Assertion

First-turn latency must fall within 20% of steady state. `easycat test`
(from `peripheral-eval-and-debugger-ui.md`) asserts this against
fixture runs so regressions in warmup coverage (e.g., a new provider
that forgets to pre-handshake) fail CI.

## Dependencies on the Essential Plan

| Item | Depends on |
|---|---|
| `CostRecord` attached to `TurnCompleted` | essential Phase 1 (journal records stable) |
| `PricingSource` protocol, budget alerts, cost rollups | essential Phase 1 |
| `JournalToOTelExporter` | essential Phase 1 (journal records stable) |
| Phoenix CI acceptance test | journal + `CostRecord` shape locked |
| Latency Budget targets, `budget_exceeded=True` tagging | essential Phase 3 (stage records) |
| CI latency assertions | `easycat.testing` (see eval file) + stage records |
| `WarmupStage` | essential Phase 3 (stage model) |
| `WarmupCompleted` journal record | Phase 1 schema, Phase 3 stage |

## Suggested Sequencing

1. **Immediately after essential Phase 1**: `CostRecord` attached to
   `TurnCompleted`, `PricingSource` protocol, `JournalToOTelExporter`
   with Phoenix CI. All three are additive to the journal and pay zero
   integration debt.
2. **During essential Phase 3**: Latency Budget targets wired to stage
   records, `budget_exceeded=True` tagging, `WarmupStage`. CI latency
   assertions land alongside the eval module.
3. **After essential Phase 3**: `warmup within 20% of steady state` CI
   guardrail enforced against fixture runs.

## Competitive Context

- **PydanticAI**: OTel-native via Logfire. Users expect EasyCat to
  reuse the same exporter with one line.
- **OpenTelemetry GenAI semantic conventions** (stabilized March 2026):
  Phoenix, Arize, Datadog, and Langfuse all consume standardized
  `gen_ai.*` attribute names. Diverging forces users to write a
  translation layer.
- **Langfuse / Helicone / LangSmith**: all ship budget kill-switches as
  standard. The pattern is settled.
- **Hamming AI / Cartesia / Inworld / Speechmatics / Daily.co**: 2026
  latency benchmarks that define the target budgets. Human conversation
  runs on a 200–300ms response window; sub-1.5s P50 E2E is the
  competitive bar for chained pipelines.
- **Phoenix**: local, free OTLP backend ideal for CI acceptance tests.
