# Debug-First Runtime Redesign — Essential Plan

> **This is the load-bearing plan.** Everything in this file is required
> to deliver the debug-first thesis. If an item is not here, it is not
> essential to that thesis — it lives in one of the four peripheral
> follow-up files, which capture valuable but separable work.
>
> **In scope (essential):** execution journal, artifact store, external
> agent bridge, Session decomposition, stage model, replay, debug
> bundle export, text-mode session, MCP pass-through (as a bridge
> correctness test).
>
> **Peripheral follow-up files** (each is a sibling initiative, not a
> dependency of this plan):
>
> - `peripheral-deployment.md` — per-platform runbooks for the
>   Deployment Targets tiers below (Fly Machines, EC2/Fargate,
>   Modal, Cloud Run, Railway/Render/DO), SQLite tuning reference,
>   Litestream vs LiteFS guidance, libSQL/Turso config, failure
>   modes for Tier 3 platforms, and the concrete decision tree.
> - `peripheral-dx-onboarding.md` — library DX: line budgets,
>   `easycat.run()` / `async with session`, string-keyed providers,
>   template content, config factory presets, offline preset, error
>   diagnostics, `EasyCatConfig` flattening.
> - `peripheral-cli.md` — `easycat` CLI focused on scaffolding
>   (`init` + template catalog + `--config` schema) and journal
>   debugging (`bundles list|show|export`, `replay`). Supporting
>   commands: `doctor`, `explain`. Typer app, `uvx` zero-install
>   guarantee, output contract, error UX. Library-wrapper commands
>   (`run`, `dev`, `test`, `cost`) are deferred.
> - `peripheral-provider-ecosystem.md` — Deepgram Flux STT adapter,
>   Smart Turn v3.1 promotion (Pipecat wrapper), backchannel filter.
> - `peripheral-redaction.md` — `RedactionPolicy` write filter,
>   `SafeConfigSnapshot` / `SafeEnvironmentSnapshot`, export-time
>   redaction pass, ready-to-use `development` / `production` /
>   `regulated` policies, bundle banner upgrade.
> - `peripheral-observability-and-cost.md` — `CostRecord` with pricing
>   source, budget alerts, `JournalToOTelExporter` with `gen_ai.*`
>   semconv, Latency Budget targets, `WarmupStage`.
> - `peripheral-eval-and-debugger-ui.md` — `easycat.testing`,
>   persona-driven Simulator + Judge, simulation-first mode,
>   `forked_replay` fidelity class, LangGraph checkpoint vocabulary,
>   interactive web debugger UI, dev waterfall terminal output.

## Summary

Redesign EasyCat's runtime around a single execution journal so every voice
agent failure can be answered with the same five questions:

1. What happened?
2. Where did it happen?
3. What did that stage receive?
4. What did it produce?
5. Can I replay only that part?

Today those answers are split across `EventTraceLogger`, `Tracer`/`Span`, and
`InMemoryMetrics`, each with different payload shapes and correlation rules.
`Session` is a 1,500-line monolith that embeds orchestration, observability,
and interruption logic. Adapter-specific history handling hides framework
execution state (handoffs, tool calls, node transitions) from observability.
A production debug flow has to reverse-engineer the pipeline.

This plan replaces those three systems with one journal, decomposes Session
into stage + context + controller types, and makes the adapter layer an
explicit bridge that exposes framework execution state as structured records.

## Constraints

- EasyCat can change its internal API shape.
- Backwards compatibility with the current public config/import surface is
  not a goal of this redesign. The plan may change `EasyCatConfig`,
  top-level exports, and agent/debug entry points when that reduces
  complexity, provided each breaking change ships with migration notes,
  before/after examples, and release-note coverage.
- EasyCat must continue to wrap OpenAI Agents SDK and PydanticAI cleanly
  without owning agent semantics.
- The runtime is a **chained voice pipeline**: STT → agent → TTS with
  discrete turn boundaries. Voice-to-voice / speech-to-speech realtime
  APIs (OpenAI Realtime, Gemini Live, etc.) are explicitly out of
  scope — see the "Chained Only" rationale below and the
  Explicit Guardrails section at the end of this file.
- Debuggability is on by default in a lightweight mode; full capture is
  opt-in.
- **Latency budget.** Debug instrumentation must not violate the P50
  <1.0s / P90 <1.6s turn-latency targets in the "Chained Only" table
  below. The Latency Budget section spells out the specific
  per-boundary overhead ceilings the journal, bridges, stages, and
  replay paths must respect.

## Latency Budget

The debug-first runtime runs inside a real-time audio pipeline. Every
journal write, state snapshot, artifact store, and bridge call sits on
the critical path of a voice turn, and a turn that misses its latency
budget is a bug regardless of how well the journal captured it.

The essential plan commits to these ceilings. They are enforced by the
perf regression gate defined in Workstream 1 (T1.0.5 baseline) and
Workstream 3 (T3.12 post-port gate).

**End-to-end turn latency (user-perceived):**

- P50 turn latency: **≤ 1.0s** (user-end-of-speech → first TTS byte)
- P90 turn latency: **≤ 1.6s**

Both inherit the conversational targets from the "Chained Only" table
above; the debug-first work must not regress them.

**The hot path is write(), not fsync().** The SQLite adapter runs in
WAL mode with `PRAGMA synchronous=NORMAL`, `PRAGMA
wal_autocheckpoint=0`, and batched per-turn commits. Under this
configuration, committing a transaction is a `write()` syscall into
the kernel page cache — **not** an `fsync()` to the underlying block
device. That means per-record latency is bounded by memcpy + B-tree
insert cost, and is **independent of the underlying storage
substrate**: a journal append costs the same on NVMe, EBS gp3, or a
network-attached volume, because none of them are touched on the
commit path. `fsync()` happens only once, at session close, when
latency is no longer a concern (see "Journal backend implications"
below for the checkpoint-on-close strategy).

**Per-boundary instrumentation overhead (debug-on-by-default mode):**

- Journal `append` (in-memory ring buffer, `debug="light"`):
  **≤ 50µs P50, ≤ 200µs P99**. This is the hot-path default for
  dev and test runs.
- Journal `append` (SQLite WAL, `debug="full"`): **≤ 100µs P50,
  ≤ 500µs P99** per record. Measured end-to-end from the append
  call including the turn-boundary commit (WAL `write()`, no
  fsync). Because fsync is off the hot path entirely, this ceiling
  is storage-independent — an EBS-backed volume meets the same
  budget as local NVMe.
- Stage-boundary snapshot (`state_before` + `state_after`,
  serialized through `apply_write_filter`): **≤ 1ms P99** per
  boundary for snapshots under 4KB inline. Snapshots above that
  size route through the artifact store and do not count against
  this budget.
- Artifact store write (content-addressable, SHA-256 hashed once at
  write time): **≤ 2ms P99** for payloads under 64KB; larger
  payloads are allowed to exceed this but must not block a turn.
- Bridge `AgentRecorder` call (a single `record_*` invocation):
  **≤ 100µs P99**. Bridges must not perform synchronous framework
  calls inside recorder invocations.

**Cumulative per-turn ceiling.** Across a full turn the sum of all
instrumentation overhead (journal writes + snapshots + artifact
writes + recorder calls) must stay under **50ms P99**. A turn that
spends more than 50ms on debug instrumentation counts as a latency
regression and fails AC3.15.

**Degraded-mode liveness guarantee.** The session enters degraded
mode (WS1 T1.9), surfaces the degraded flag on `JournalView`, and
flips subsequent writes to best-effort when a single hot-path
journal write takes longer than **2ms** (ten times the P99 ceiling
— a clear signal that something is wrong with the process, not the
storage). Voice turns never block on journal writes. This is the
invariant that makes the debug-first guarantee compatible with
real-time audio: correctness without a liveness hazard.

**Measurement.** The WS1 T1.0.5 perf baseline captures end-to-end
turn latency under a known workload (50 partial transcripts/sec for
10s). WS3 T3.12 re-measures after stage ports. WS4 adds replay
overhead measurement. Every workstream that touches the stage
critical path carries a perf regression AC against these ceilings.

Numbers above are initial targets based on the "Chained Only"
latency table and realistic real-time audio processing overhead on
an EC2-class CPU. They will be refined during the WS1 plan once the
baseline harness lands; refinements tighter than the targets here
require a plan amendment.

## Why Debug-First Is the Bet

Self-hosted, framework-agnostic voice debugging does not exist today.
LiveKit's Agent Observability requires LiveKit Cloud. Pipecat ships nothing
comparable. Vocode, Bland, Vapi, and Retell all optimize for time-to-first-
call at the cost of debugging depth. The debug-first runtime is EasyCat's
single biggest differentiator opportunity, and it is a pure software bet: no
provider partnerships, no hosted backend, no proprietary model.

Everything that is *not* debug-first — CLI ergonomics, provider additions,
eval harness, onboarding budgets, OTel export — is valuable but
separable. Those live in the peripheral follow-up files so this plan
can stay focused.

## Deliberately Deferred (Phase 2)

These are capabilities a mature EasyCat will need but that are out of
scope for the debug-first plan. Deferring them is deliberate: each
depends on the journal/bridge/stage substrate landing first, and each
would dilute the debug-first thesis if sequenced earlier. They are
not forgotten — they are Phase 2.

- **Multi-tenancy.** The essential plan assumes one `run_id`/
  `session_id` per process-visible Session. There is no
  `tenant_id` on `RunContext`, no per-tenant journal isolation,
  no per-tenant quota. When multi-tenancy lands, it will add a
  `tenant_id` field to `RunContext`, scope journal reads/writes
  by it, and plug into the existing `apply_write_filter` hook for
  per-tenant redaction policy.
- **Pre-voice authentication.** EasyCat assumes the caller has
  already been authenticated by whatever accepts the inbound
  WebSocket/SIP/HTTP connection (the telephony provider, the web
  server, the reverse proxy). Caller-ID, session tokens, JWTs,
  and tenant bindings are upstream concerns. The journal can
  record the result of auth as allowlisted metadata, but
  performing auth is not in scope.
- **Rate limiting and quota.** Per-session cost budgets are
  addressed in `peripheral-observability-and-cost.md`. Global
  rate limiters (calls/minute, concurrent sessions per API key,
  regional caps) are not.
- **CI/CD harness for bundles.** WS4 ships `load_bundle()` as a
  pytest helper. Wrapping it as a reusable CI harness (GitHub
  Actions workflow, pytest plugin for bundle-as-fixture
  parameterization, cost-delta assertions on PRs) lives in
  `peripheral-eval-and-debugger-ui.md` and `peripheral-dx-
  onboarding.md`, and is not required for the debug-first
  thesis.
- **Enterprise release process.** Semver guarantees, breaking-
  change deprecation windows, SDK release automation, and long-
  term support policies are a separate initiative. The essential
  plan ships under a `0.x` alpha tag with per-workstream
  deprecation notes (see Migration Strategy below).

If any of these becomes a blocker for an early adopter, raise an
issue; they can be pulled forward with their own essential plan.
None of them require redesigning the debug-first substrate.

## Deployment Targets

EasyCat must be easy to deploy — on EC2-class long-lived VMs first,
and on pay-as-you-go serverless platforms where they can meet the
latency and WebSocket requirements of a chained voice pipeline.
Deployment is not a peripheral concern: the journal backend
selection, the crash-durability contract, the degraded-mode
liveness guarantee, and the latency budget all depend on what the
hosting environment can offer. Full per-platform runbooks, cold-
start tuning, and the SQLite-vs-alternative decision tree live in
`peripheral-deployment.md`; this section pins the constraints and
the tier assignments.

**Non-negotiable constraints.** Any deployment target EasyCat
officially supports must:

- sustain a long-lived WebSocket (or equivalent bidirectional
  stream) for the full duration of a voice call — no HTTP
  request-timeout ceilings shorter than the longest expected
  call
- preserve session affinity: once a call lands on an instance, it
  stays there until the call ends
- meet the Latency Budget above (P50 <1.0s, P90 <1.6s turn
  latency; ≤50ms P99 cumulative instrumentation overhead)
- run native-Linux Python with `numpy`, `onnxruntime`,
  `webrtcvad`, `librosa`, PortAudio bindings, and other audio-
  adjacent native wheels (Pyodide / WASI-only runtimes are
  excluded by construction)
- either provide durable disk for the SQLite journal *or* a
  first-class alternative the plan targets (WAL shipping to
  object storage via Litestream, embedded libSQL/Turso replicas,
  or a managed relational DB)

### Tier 1 — Recommended defaults

These are the deployments EasyCat documents first and its CI
covers end-to-end. Each supports every non-negotiable constraint
above without workarounds.

- **Fly.io Machines** (default). Purpose-built for this shape.
  Native unbounded WebSockets, region-pinned Machines, sub-
  second `auto_stop_machines="suspend"` / resume from memory
  snapshot, persistent Volumes for the SQLite journal, first-
  class Litestream and LiteFS support, documented
  spawn-per-session pattern via the Machines API
  (`api.machines.dev/v1`). Matches the Pipecat reference
  architecture. Recommended sizing: 2 vCPU shared / 1 GB for the
  simple chained pipeline; 4 vCPU / 4 GB when Silero VAD +
  SmartTurn ONNX + Krisp are all enabled.
- **AWS EC2 / ECS Fargate with EBS + Litestream** behind an NLB.
  Boring, always works, no platform-specific quirks. Matches the
  LiveKit Agents self-host guidance: **4 cores / 8 GB per agent
  server handles 10–25 concurrent sessions**; use compute-
  optimized instance families (`c6i`, `c7i`), **not** burstable
  (`t3`, `t4g`) — CPU credit starvation causes Smart Turn
  inference timeouts and interruption drift. EBS for the SQLite
  journal on EC2; EBS (not EFS) for Fargate with a mounted
  volume. Never put a write-heavy SQLite DB on EFS — NFS
  semantics kill WAL fsync throughput.
- **Modal with `min_containers≥1`.** Pay-as-you-go with prewarmed
  model weights. Use `@modal.asgi_app()` on an `@app.cls` class
  and bind FastAPI `WebSocket` endpoints so model instances stay
  warm across sessions. Required settings: `min_containers=1`
  (or `buffer_containers` for burst headroom), `timeout=3600`
  (default 600 is too short for calls),
  `scaledown_window=600`, and a `@modal.enter()` hook that opens
  the SQLite file during container warmup so the first turn
  doesn't pay the ~50ms cold file-open cost. Journal goes to
  Turso (embedded libSQL replica syncing to an edge primary),
  not Modal Volumes — Modal Volumes are tuned for model weights,
  not hot transactional fsync loops.

### Tier 2 — Supported with caveats

These work but require the user to understand a platform-specific
constraint. The deployment peripheral documents each constraint
explicitly so users do not hit it in production.

- **Google Cloud Run** — only for deployments where the longest
  expected call is under the platform's **60-minute request
  timeout**. Required flags: `--timeout=3600
  --min-instances=1 --session-affinity
  --execution-environment=gen2 --cpu-boost
  --no-cpu-throttling`. Journal goes to Turso or Cloud SQL,
  never local FS (Cloud Run's FS is tmpfs).
- **Railway / Render / DigitalOcean App Platform.** Native
  WebSockets, long-lived connections, but instances are replaced
  during deploys and maintenance — clients must implement
  reconnect logic. No scale-to-zero on the relevant paid tiers.
  Fine for demos, internal tools, and low-traffic prod. Journal
  goes on attached block storage with Litestream→S3 on Railway;
  Render and DO users should prefer Turso or managed Postgres.

### Tier 3 — Not recommended (and why)

Documented explicitly so users don't waste time on them. Each
fails one of the non-negotiable constraints above.

- **AWS Lambda (API Gateway WebSocket, Function URL streaming,
  or Lambda Web Adapter).** The request-response execution model
  is incompatible with in-memory `TurnContext`/`VoiceDeliveryLedger`
  state; API Gateway WebSocket has a **2-hour max connection / 10-
  minute idle timeout** and charges per message; Lambda Web
  Adapter only supports one-way RESPONSE_STREAM (server→client
  SSE), not bidirectional WebSockets.
- **Azure Container Apps.** Hard **240-second HTTP request
  timeout** on the consumption plan (confirmed in
  `learn.microsoft.com/en-us/azure/container-apps/ingress-overview`,
  updated 2026-03-25). WebSockets inherit the timeout. Any voice
  call longer than four minutes is killed. Disqualifier.
- **Cloudflare Workers Python (Pyodide).** Architecturally ideal
  (native hibernatable WebSockets, Durable Objects with embedded
  SQLite, global edge), but the Python runtime is Pyodide/WASM
  and cannot load `onnxruntime`, `webrtcvad`, `librosa`, or
  PortAudio bindings. Revisit in 6–12 months if the Pyodide
  package set grows. Until then, EasyCat's VAD, Smart Turn, and
  noise-reduction stages cannot run on Workers.
- **Vercel.** No Python WebSocket server in any runtime; 15-
  minute function cap via the underlying Lambda invocation.
- **Cloud Run Jobs.** Batch execution model, not an HTTP
  service. Wrong shape entirely.

### Journal backend implications

The tier assignments above imply that WS1 ships **four journal
backends from day one**, all implementing the `ExecutionJournal`
protocol. Two are non-durable (in-memory ring buffer for
`debug="light"`, plain `SqliteJournal` for local-only
`debug="full"`); two are durable with replication:

1. **`sqlite+litestream`** — local SQLite at
   `.easycat/journals/<session_id>.sqlite` with WAL mode,
   `PRAGMA synchronous=NORMAL`, `wal_autocheckpoint=0`,
   checkpoint-on-close, and Litestream shipping WAL segments to
   S3-compatible object storage on a sub-second RPO. This is the
   default for Tier 1 Fly/EC2/Fargate and Tier 2 Railway with a
   volume.
2. **`libsql`** (Turso embedded replica) — in-process libSQL
   replica with local commits on append and background sync to a
   remote primary. Reads are local µs; remote durability syncs
   asynchronously every N seconds. This is
   the default for Tier 1 Modal, Tier 2 Cloud Run, and any
   ephemeral-FS host where `sqlite+litestream` would lose WAL
   segments on container exit.

Both adapters plug into the same WS1 `ExecutionJournal` interface;
users choose via `EasyCatConfig.journal_backend` without changing
any other code. `peripheral-deployment.md` covers the tuning
details (Litestream `db`/`replica` config, libSQL `sync_interval`,
startup-hook file-open warmup).

#### Checkpoint strategy

The SQLite adapter runs with `PRAGMA wal_autocheckpoint=0` — i.e.,
inline autocheckpoint is **disabled**. This prevents the default
SQLite behavior of running a PASSIVE checkpoint on a random writer
when the WAL grows past ~4MB, which on high-tail-latency storage
(EBS, network-attached volumes) would cause sporadic multi-ms
stalls on the hot path. With autocheckpoint disabled, no `fsync()`
occurs during the session's lifetime. The WAL grows for the
duration of the call and is checkpointed once at clean session
close, when latency is no longer a concern.

**WAL growth is bounded by session length.** A 10-minute session at
50 partial transcripts/sec plus stage snapshots ≈ 30MB WAL. A
30-minute session ≈ 90MB. These are well within the capacity of any
production deployment. For multi-hour calls where WAL growth could
reach hundreds of MB, a future optimization can add periodic
checkpointing — but that is not a launch requirement.

**Application crashes do not depend on the checkpoint.** The
checkpoint only affects the kernel-crash / power-loss durability
window (see the `ExecutionJournal` responsibilities section).
Python-level crashes — unhandled exceptions, OOM kills, segfaults
in C extensions, audio driver blow-ups, telephony disconnects — are
fully covered by the kernel page cache regardless of whether a
checkpoint has ever run. The voice failure modes we actually care
about for debugging are all in the application-crash category, so
the checkpoint timing is irrelevant to crash-durability
correctness.

**On unclean shutdown** (crash, SIGKILL), the WAL is left
uncheckpointed. This is fine — SQLite's WAL recovery reads the
uncheckpointed WAL natively on next open. The crash-recovery path
(T1.6) handles this: move the journal to `.easycat/crash-dumps/`,
emit a `RecoveredSessionMarker`, and the uncheckpointed WAL is
fully readable for bundle export and replay.

## Scaling and Deployment Topology

### The bridge is in-process by design

The three in-process bridges (`OpenAIAgentsBridge`, `PydanticAIBridge`,
`GenericWorkflowBridge`) are in-process Python Protocol implementations,
not RPC endpoints. This is deliberate: the expensive remote call is the
LLM inference (OpenAI API, Anthropic API, etc.), and that is already
remote — the framework SDK makes the network call. The bridge itself is
thin state management: message history, cursor tracking, interruption
patching, tool dispatch coordination. Splitting this across a process
boundary would add latency to the interruption hot path (where the
plan budgets 50ms P99 cumulative) with no benefit, because the
compute-heavy work is already remote.

The bridge boundary is not a network boundary. It is a responsibility
boundary between the voice runtime (which owns audio, VAD, TTS,
interruption timing) and the agent framework (which owns reasoning,
tools, and conversation state).

### Session affinity is a hard requirement

A `Session` object must not move between processes during a call.
The `VoiceDeliveryLedger`, `InterruptionController`, and bridge all
hold per-session in-process state that the barge-in path reads
synchronously. This is already stated in the Deployment Targets
section (session affinity on all tiers) and is restated here for
emphasis: horizontal scaling is achieved by routing sessions to
processes, not by migrating sessions between them.

### Scaling model: two patterns

**Process-per-session (Fly Machines pattern).** One process, one
call. Perfect isolation. The journal is a single-writer SQLite file.
No contention. When the call ends, the process can suspend or exit.
Best for production voice deployments where calls are long enough
(30s+) that startup cost (~50–200ms for Python + model loading) is
amortized. Recommended sizing: 2 vCPU / 1 GB for simple pipelines;
4 vCPU / 4 GB with Silero VAD + SmartTurn ONNX + Krisp.

**N-sessions-per-process with affinity.** One process handles
multiple concurrent sessions, with a load balancer pinning each
session to a process. Each `Session` is independent (own
`TurnContext`, own journal, own bridge instance). Silero and
SmartTurn models are loaded once and shared read-only across
sessions. Best for telephony gateways handling many short calls.
Capacity planning: 4 cores / 8 GB handles 10–25 concurrent sessions
(matches LiveKit Agents guidance). Use compute-optimized instances,
not burstable — CPU credit starvation causes inference timeouts.

Both patterns are supported. The plan does not require one or the
other; the journal, bridge, and stage designs are compatible with
both because all per-session state lives on the `Session` object.

### Session failover

If a process dies mid-call, the call is lost — the user hears
silence or a disconnect. The journal survives (crash-durable
SQLite, see WS1 T1.6) and can be exported as a bundle for
post-mortem debugging, but the live session does not resume.

Session migration (reconstructing a live Session from a journal
snapshot on a different process) is Phase 2 complexity that the
plan explicitly defers. The debug-first thesis is about
understanding what happened after a failure, not about preventing
the failure from ending the call. The journal survives; the call
does not need to.

### Remote agent deployment

Not every deployment runs the agent in the same process as the voice
runtime. Many companies want the agent to run inside their own
network — behind their firewall, with access to their tools and
data — while the voice orchestration runs as a managed service or
on edge infrastructure. This is the deployment model used by
Pipecat, Retell, Vapi, and similar platforms.

EasyCat supports this via the `RemoteResponsesAPIBridge`, a fourth bridge
that speaks the OpenAI Responses API over HTTP to a remote agent
server. See the "Remote Agent Bridge" section under Agent
Compatibility Boundary for the protocol design, and
`workstream-2c-remote-bridge.md` for the operational plan.

The in-process bridges remain the recommended path when the agent
can run co-located — they provide full debugging depth, all journal
record types, and the tightest interruption latency. The remote
bridge is the supported path when deployment separation is required,
with an explicit trade-off: reduced journal depth in exchange for
deployment flexibility.

## Non-Goals

Out of scope for this plan (and some also out of scope for EasyCat entirely):

- **Voice-to-voice / realtime speech-to-speech** (OpenAI Realtime,
  Gemini Live, Kyutai, etc.) — see the "Chained Only" rationale
  below and the Explicit Guardrails section at the end. Permanently
  out of bounds for EasyCat, not deferred to a follow-up.
- New chained providers beyond the current set (Deepgram Flux and
  similar STT upgrades remain peripheral follow-ups)
- CLI tooling (`easycat init`, `doctor`, `explain`, `dev`, `run`,
  `test`, `bundles`, `replay`, `cost` — entire command surface in
  `peripheral-cli.md`)
- Line-count budget enforcement on examples
- `run()`, `async with session`, string-keyed provider selection
- OTel export
- `easycat.testing` with Simulator + Judge
- Interactive web debugger UI
- `--for=claude-code` bundle export
- Forked replay / time-travel
- Latency budget CI enforcement, warmup stage
- Smart Turn v3.1 promotion, backchannel filter
- Offline preset, template ecosystem
- **LangChain and LangGraph bridges** — deferred, not excluded.
  Both frameworks fit cleanly onto the existing
  `ExternalAgentBridge` protocol and would ship as additional
  bridge classes alongside `PydanticAIBridge` and
  `OpenAIAgentsBridge`. See `peripheral-langchain-langgraph-bridge.md`
  for the protocol-fit analysis, implementation sketch, and
  examples. Adding these later is purely additive — no changes
  to the bridge protocol, journal schema, or workstream plans
  are required.

Everything in the non-V2V list depends on the journal or bridge landing
first. They are not competing with this plan; they are downstream of it.
See the peripheral follow-up files.

Also permanently out of bounds for EasyCat (guardrails at the bottom of this
doc): voice-to-voice realtime APIs, EasyCat-native tool API, EasyCat-native
MCP client or tool registry, EasyCat-native planner/router, EasyCat-native
memory or prompt compiler, EasyCat-native multi-agent abstraction beyond
compatibility bridges, hosted observability backend.

### Chained Only: Why Voice-to-Voice Is Out of Scope

Chained voice pipelines (STT → agent → TTS) and voice-to-voice
realtime sessions (bidirectional audio streamed through one model)
look similar at 30,000 feet and are fundamentally different at every
altitude that matters for a runtime:

| Axis | Chained pipeline | Voice-to-voice realtime |
|---|---|---|
| Audio flow | Discrete turns: user audio → transcript → agent → audio | Continuous bidirectional stream, no turn boundary |
| Latency target | P50 <1.0s, P90 <1.6s (acceptable conversational) | P50 <300ms, P90 <500ms (human-native) |
| State shape | Text history + delivered-audio ledger | Live multimodal session state owned by the model |
| Transcripts | Always available (STT output) | Partial, delayed, or absent |
| Interruption | Cancel TTS queue + patch text history | Session-level cancel signal to live model |
| Tool calls | Between turns | Mid-audio-stream while audio flows both ways |
| Cost model | STT seconds + text tokens + TTS characters | Audio input/output tokens (10–30× per-word cost) |
| Provider landscape | Deepgram, Cartesia, ElevenLabs, OpenAI STT/TTS | OpenAI Realtime, Gemini Live |
| Failure modes | STT errors, VAD false positives, TTS drift | Model hallucinations, WebSocket drops, audio token overruns |
| Debugging primitives | STT cassette replay, TTS cassette replay, turn-by-turn journal | Bidirectional audio cassette replay against live provider |

Trying to serve both with one runtime would force every abstraction
to satisfy both the "discrete turn with clean STT/Agent/TTS
boundaries" model and the "continuous multimodal session" model,
which means every abstraction would compromise on both. The `Stage`
protocol would grow fused-stage escape hatches, the journal would
need partial deferred records, the interruption contract would need
two code paths, replay would need two fidelity stories, the
debugger UI would need two views, and users would need two mental
models. The common-runtime savings are small; the per-abstraction
compromises compound everywhere.

The debug-first thesis is *only* credible if the runtime can
answer "what happened and can I replay it" uniformly. Chained
pipelines support that completely: STT outputs are captured, VAD
and Smart Turn decisions are byte-reproducible (per the Voice
Stage Decisions Must Be Reconstructable principle), TTS is
cassette-replayable, every stage boundary is journaled. Voice-to-
voice sessions do not support it uniformly: transcripts are
provider-decided, audio is bidirectional and huge, tool calls
happen mid-stream without commit boundaries, replay depends on
the live provider API, and the "which stage was slow" question
collapses into "the model was slow, we don't know why". Different
primitives, different debugging questions, different answers.

Users who want voice-to-voice should use the provider SDK
directly (OpenAI Realtime, Gemini Live). EasyCat's contribution is
a debug-first runtime for the chained pipeline, and that is what
it optimizes for end-to-end.

Preserving the current public surface verbatim is also out of scope. This
plan optimizes for a coherent runtime and coherent debugging model first;
compatibility shims are optional and must justify their maintenance cost.

## Principles

### One Source of Truth

Logs, spans, counters, and debug views derive from one execution journal. No
parallel observability systems with different payload shapes. The journal is
the debugging system; it is not a telemetry pipeline.

### Bring Your Own Agent

EasyCat owns the voice runtime around the agent call. OpenAI Agents and
PydanticAI own reasoning, tools, and workflow semantics. The bridge
translates framework-native events into journal records. It does not define
an EasyCat-native agent model.

### Debuggability Requires Replay

A record is not enough. Every major boundary must be replayable from
captured inputs or normalized artifacts.

### Voice Stage Decisions Must Be Reconstructable

VAD and Smart Turn (endpoint detection) are the two voice-specific
stages where "why did it decide that" is the most common debugging
question. Both must be fully reconstructable from the journal alone
— given the captured stage inputs and stage state, a later replay
must arrive at the byte-identical decision the live session made.
This is a stronger invariant than "every stage emits state_before /
state_after snapshots"; it requires that the snapshot payload is
sufficient to re-derive the decision, not just describe it.

**Audio frame defined.** In EasyCat, an *audio frame* is one VAD
inference window at 16 kHz, 16-bit mono PCM, 512 samples (32 ms)
wide. This is Silero's native window size and the unit VAD
decisions are made at. `VADStage.snapshot_state()` records
per-frame data at this granularity; the audio artifact is stored
once per frame-aligned window in the artifact store (content-
addressable, so duplicate silence compresses naturally). Smart Turn
operates on a larger audio *window* (the last N frames of detected
speech, typically 1–3 seconds) which is also stored by artifact
ref. Providers that deliver audio at different rates (e.g.,
telephony 8 kHz) are resampled to 16 kHz before hitting `VADStage`,
and the resampler is itself a journaled stage operation with its
own inputs captured — cross-rate drift is therefore visible in the
journal.

Concretely, `VADStage.snapshot_state()` must capture audio frame
timestamps, per-frame probability/energy values, the active
threshold, in-speech flag transitions, pause-timer deadlines, and
backend identity + version. `TurnStage.snapshot_state()` for Smart
Turn must capture the input audio window (by artifact ref), the
ONNX model identity and version, the feature inputs fed to the
model, the raw classification output, the decision threshold, and
the final endpoint decision. The captured payload plus the audio
artifact is the only thing a replay needs.

**Per-frame data placement.** Per-frame probability/energy values
do *not* inflate inline records. VAD captures them as a compact
packed numpy array (float32 probabilities + int16 energy, ~6 bytes
per frame) stored as a single artifact per turn and referenced
from the stage snapshot via `frames_ref`. A 10-second turn is ~300
frames ≈ 1.8 KB — well under the per-snapshot size ceiling and
trivially compressible. Inline records hold only the frame-count,
backend identity, and the decision events (`speech_start`,
`speech_end`), not the per-frame array.

This is a voice-specific invariant. Other stages satisfy the weaker
"replayable with captured inputs" rule; VAD and Smart Turn are held
to the stronger "byte-identical decision reproduction" rule because
their failure modes (false positives, false negatives, late
endpointing) are the hardest bugs to reproduce without determinism.

### Normalize Conservatively

EasyCat normalizes only the cross-framework concepts needed for runtime
debugging: generation, tool call, handoff, workflow node or specialist
transition, interruption, state commit. Framework-specific meaning stays
attached as metadata rather than being collapsed into a new EasyCat-native
agent model.

### Progressive Disclosure

Quickstart users never see journal, stage, or bridge concepts. Advanced
users reach them through explicit debug APIs such as `session.journal` and
bundle export/load, never through required config. This plan is allowed to
change the public config and import surface where that reduces complexity;
the requirement is clear migration documentation, not signature stability.
Any extra ergonomic polish beyond the core debug surface belongs in the
follow-up plan so this one stays focused on runtime correctness.

## Core Runtime Types

The plan introduces the following types incrementally. Most are internal
plumbing; the exceptions are the read-only journal/debug surfaces that
advanced users need in order to replace the legacy observability APIs.

### `RunContext`

Shared context for a session/runtime instance:

- `run_id`
- `session_id`
- safe config snapshot (hard-coded allowlist — see "Config and
  Environment Safety Default" below; a full `RedactionPolicy` lands
  in `peripheral-redaction.md`)
- runtime mode (`chained_pipeline` or `text_session`)
- artifact store handle
- journal handle

### `TurnContext` (extended)

Per-turn runtime state extracted from Session instance variables:

- `turn_id`
- turn timings
- interruption metadata
- cancel token
- playback state
- telephony state hooks

Currently much per-turn state lives on Session as instance variables
(`_agent_response_parts`, `_tts_chunks`, `_playback_mark_to_bytes`). These
must migrate into `TurnContext` so state can be snapshotted at stage
boundaries.

### `VoiceDeliveryLedger`

The runtime-owned record of what actually happened in the voice channel:

- user transcript inputs
- raw agent text
- post-processed spoken text
- playback acknowledgements (see definition below)
- estimated delivered assistant text at interruption time
- interruption cut points and confidence

**Playback acknowledgement defined.** A playback acknowledgement is a
tuple `(mark_id, acked_bytes, acked_at)` emitted whenever the
transport layer confirms that N bytes of a specific TTS chunk were
handed off to the downstream audio sink. The source of truth is
transport-specific:

- `LocalTransport`: bytes written to the output `AudioQueue` counted
  against each `mark_id` handed in with the chunk.
- WebSocket transports: explicit `playback_mark` messages echoed
  back from the browser/client after its audio element drained.
- Telephony transports (Twilio/SIP): `mark` events from the
  provider's media stream acknowledging frame playback.

The ledger maps `mark_id` back to the TTS chunk and its character
offsets so `InterruptionController` can compute "how much of the
assistant's text did the user actually hear before they interrupted."
A chunk with no ack by `end-of-playback + 500ms` is flagged as
`delivery_unknown` and interruption computation falls back to the
last acknowledged mark plus an estimator.

This ledger is the source of truth for barge-in behavior. It is distinct
from any framework conversation history.

**Ownership.** `VoiceDeliveryLedger` is extracted from the Session
monolith in Workstream 3 (T3.3). WS1 defines the journal hooks it
writes to and WS2 defines the `apply_interruption` contract it calls
into, but the type itself does not exist until WS3 extracts it from
the current `_session.py` instance variables
(`_agent_response_parts`, `_tts_chunks`, `_playback_mark_to_bytes`).
Essential-plan sections that reference it are describing the
end-state.

### `InterruptionController`

Runtime-owned controller for voice-specific interruption policy:

- detect interruption boundaries
- determine what text was likely delivered
- choose cancellation policy
- decide whether to drain or stop in-flight work
- apply interruption updates through the bridge

**Ownership.** Like `VoiceDeliveryLedger`, the `InterruptionController`
type is extracted from the Session monolith in Workstream 3 (T3.2).
WS2 defines the bridge-side `apply_interruption(delivered_text, mode)`
contract that the controller calls into; WS3 implements the
controller itself.

### `AgentRecorder`

Write-side shim used by bridges and stages to emit structured records
into the journal. Not a public API — bridges receive one per `invoke()`
call; stages hold one for their lifetime. `AgentRecorder` applies the
essential plan's Config and Environment Safety Default before any
record reaches the backend; a full `RedactionPolicy` hook lands with
the peripheral redaction work and plugs into the same call site
without changing the AgentRecorder protocol.

The full protocol (methods for unit entry/exit, tool calls, handoffs,
state snapshots, cancellation boundaries) is defined in Workstream
2A T2.1.5.

### `ExecutionJournal`

Append-only structured record store. Informed by Temporal's Event History
and Restate's operation-level journaling.

Responsibilities:

- record stage operations
- correlate artifacts by `run_id`, `session_id`, `turn_id`, `op_id`
- index with a monotonic sequence number per session (not just `op_id`)
  for deterministic ordering during replay
- store large payloads via indirection (`input_ref`, `output_ref` pointing
  into `ArtifactStore`, not inline blobs)
- visibility guarantee: `append` returns only after the record is readable
  through the session's `JournalView`, so stage output is never forwarded
  before the runtime can answer "what just happened?" Under the SQLite +
  `synchronous=NORMAL` configuration, this means the record has been
  handed to the kernel via `write()` — the data lives in the kernel page
  cache and is owned by the OS from that point on, which is what makes
  the guarantee cheap enough to honor synchronously on every storage
  substrate.
- record/artifact atomicity guarantee: a record may set `input_ref`,
  `output_ref`, or `state_snapshot_ref` only after the referenced artifact
  is fully committed in the selected artifact store. If capture is
  truncated, omitted, or rejected by policy, the ref stays `None` and the
  record carries explicit capture metadata instead. A loadable journal or
  bundle never contains dangling artifact refs.
- **application-crash durability (always on, inherent to the write path).**
  If the Python process segfaults, is OOM-killed, hits an unhandled
  exception, or exits for any reason short of a kernel crash, the
  committed journal is fully loadable afterward. This is not something the
  plan has to engineer — it falls out of SQLite's commit protocol. Commits
  go through `write()` into the kernel page cache, which means the pages
  are owned by the kernel, not the Python process. When Python dies the
  kernel's writeback thread still flushes those pages to disk on its
  normal schedule, regardless of whether the process that wrote them is
  still alive. The voice failure modes we care about — telephony
  disconnects, mic drivers, audio buffer underruns, provider exceptions,
  OOM kills — are all in this category, and are all covered by default
  under `debug="full"` with zero additional configuration.
- **kernel-crash / power-loss durability (best-effort, bounded by kernel
  writeback).** A true kernel panic, hypervisor failure, or power loss
  can lose any WAL pages not yet written back to the block device by
  the kernel. Under the checkpoint-on-close strategy (see "Journal
  backend implications" above), no `fsync()` happens during the
  session, so the kernel-crash window is bounded by the OS writeback
  schedule (typically 5–30s on Linux, controlled by
  `vm.dirty_writeback_centisecs`). This is a strictly weaker guarantee
  than application-crash durability, but kernel-level crashes are
  overwhelmingly ops failures (spot reclaim, hypervisor issues, noisy
  neighbors) rather than application bugs, and the last few seconds of
  a terminated instance are rarely the most valuable debug artifact.
  In-memory backends waive both guarantees and must log a single
  startup line making the tradeoff explicit.
- configurable backend, with explicit per-mode defaults:
  - `debug="off"` → no backend, zero writes, zero overhead. For
    throughput-bound production where debugging is handled
    separately.
  - `debug="light"` (default) → in-memory ring buffer, capacity
    bound. Crash-durability is waived with a startup log line.
    Intended default for quickstart and local development.
  - `debug="full"` → durable backend. The default is a SQLite WAL
    backend at `.easycat/journals/<session_id>.sqlite` per the
    WS1 T1.2.5 storage layout, but replicated backends such as
    `sqlite+litestream` and `libsql` plug into the same protocol.
    This is the mode production voice deployments should run in
    when they want the full debug story; see the "Deployment
    Targets" section below for how this interacts with serverless
    vs long-lived VM deployments.
  - Any other durable backend (Postgres, libSQL/Turso, object
    storage with WAL shipping) plugs into the same
    `ExecutionJournal` protocol as an additional backend. The
    essential plan ships the SQLite backend; the deployment
    targets section documents which hosted options we recommend
    for each class of deployment.

### `JournalView`

Read-only public journal surface exposed on `Session` as `session.journal`.

Responsibilities:

- iterate or slice records without exposing append/mutation methods
- tail live records via `follow(from_sequence: int | None = None) ->
  AsyncIterator[JournalRecord]` so existing subscriber-style debug flows
  have a direct migration path
- resolve artifact references through the artifact store
- surface `enabled` / `degraded` state without exposing backend internals
- support the migration path from `EventTraceLogger` subscriptions to
  journal reads or `follow()`
- remain stable enough to support bundle export and offline regression
  tests

### `ArtifactStore`

Stores larger payloads and sensitive blobs separately from inline records:

- audio snippets
- provider payload excerpts
- transcripts
- tool arguments and results
- serialized config snapshots (hard-coded allowlist only — see
  Config and Environment Safety Default below)
- request/response payloads

Artifact entries honor the essential plan's Config and Environment
Safety Default: raw `EasyCatConfig.__dict__` and raw `os.environ`
never land in an artifact. Other payload content (audio, transcripts,
tool args, provider bodies) is stored verbatim until the peripheral
`RedactionPolicy` from `peripheral-redaction.md` ships,
at which point those fields flow through the per-field policy.
Large snapshots are stored by reference; small inline fields remain
JSON-safe.

Artifacts are split into two capture classes:

- `replay_critical`: payloads required for deterministic replay or
  committable-boundary restore (for example STT cassettes, TTS chunks,
  VAD/Smart Turn artifacts, framework state snapshots). These must be
  written successfully before the record that references them is published.
  If they would exceed the hot-path size budget, the stage must segment or
  encode them into bounded chunks rather than emit one giant payload.
- `debug_verbose`: payloads useful for diagnosis but not required for
  replay correctness (for example provider request bodies, large tool
  results, verbose metadata dumps). These are subject to mode-specific size
  caps. If capturing them would violate the write-time budget, EasyCat
  stores a truncated excerpt or drops them and records
  `metadata.artifact_capture = {"class": "debug_verbose", "status":
  "truncated" | "dropped", "original_bytes": ...}` on the corresponding
  journal record instead of emitting a dangling ref.

Retention follows the same contract. In `debug="light"` the in-memory
artifact store is bounded and evicts records and their now-unreachable
artifacts together; `JournalView` never exposes a retained record whose ref
cannot be resolved. In `debug="full"` artifacts are persisted under
`.easycat/artifacts/<session_id>/` (or the backend-native equivalent) and
form part of the crash-recovery story.

### Config and Environment Safety Default

The essential plan ships exactly one hard-coded guardrail for
sensitive data and defers everything else to
`peripheral-redaction.md`:

- The journal and artifact store MUST NOT inline
  `EasyCatConfig.__dict__` wholesale.
- The journal and artifact store MUST NOT inline `os.environ`
  wholesale.
- A small hard-coded allowlist of debug-useful, non-secret config
  fields (provider role identifiers, model names, runtime mode,
  timeouts) is the only thing that gets serialized when a record
  needs a config snapshot.
- A small hard-coded allowlist of env vars (`EASYCAT_*` variables
  that control the runtime itself) is the only thing that gets
  serialized for environment metadata.
- Everything outside both allowlists is dropped.

This is not a policy system — it is a safe default that prevents
accidental API-key leaks before the full `RedactionPolicy` from
`peripheral-redaction.md` lands. Essential-plan bundles
carry a banner noting they contain raw transcripts, tool args, and
provider payloads and are dev-only until the peripheral redaction
work ships.

### `RunBundle`

Portable export unit for debugging and regression testing.

Contains:

- journal records
- artifact index (refs are content-addressed SHA-256 hex digests; the
  bundle is not tamper-evident — trust comes from the transport you
  use to share it, not from the bundle itself)
- safe config snapshot (hard-coded allowlist per Config and Environment
  Safety Default above)
- allowlisted environment metadata (hard-coded allowlist)
- provider version strings (Deepgram API version, ElevenLabs model ID, etc.)
  — voice provider behavior changes silently and this is critical for
  understanding why a replay diverges from production
- replay entry points
- bundle format version for forward compatibility
- **Dev-only banner**: every essential-plan bundle carries a banner
  noting it may contain raw transcripts, tool args, and provider
  payloads. The banner is replaced with a per-field policy summary
  once `RedactionPolicy` from `peripheral-redaction.md`
  ships.

### `FrameworkStateSnapshot`

Bridge-produced snapshot of the external framework state at a committable
boundary.

Requirements:

- JSON-serializable and stable across bundle export/load
- secret-safe by construction; no raw credentials or auth material
- no raw framework objects or live handles
- large/sensitive payloads stored via artifact refs

Examples:

- OpenAI Agents: current agent, response IDs, local history mirror
- PydanticAI: message history, deps/model settings, workflow active node

### `StageStateSnapshot`

Stage-produced snapshot of runtime state immediately before or after a stage
operation.

Requirements:

- JSON-serializable and stable across bundle export/load
- secret-safe by construction
- no raw provider client objects, transports, sockets, or cancellation
  primitives
- large payloads referenced indirectly through the artifact store

### `Stage` Protocol

```python
class Stage(Protocol):
    async def execute(self, input: Any, ctx: RunContext, turn: TurnContext) -> Any: ...
    def snapshot_state(self) -> StageStateSnapshot: ...
    def replay(self, spec: ReplaySpec) -> Any: ...
    async def handle_upstream(self, signal: ControlSignal) -> None: ...
```

`handle_upstream` enables bidirectional flow (Pipecat pattern). Control
signals — interruption, backpressure, cancel, pause — flow upstream through
stages, not just downstream data. Without this, every stage reads a shared
cancel token and the journal cannot record *which stage* observed a signal
in *what state*. With explicit upstream signals, each stage's reaction to
cancellation becomes a first-class journal event.

Initial stage set:

- `TransportStage`
- `AudioStage` (noise reduction + echo cancellation)
- `VADStage`
- `STTStage`
- `TurnStage`
- `AgentStage`
- `TTSStage`
- `TelephonyStage`

Every stage writes the same conceptual record shape:

- `stage`
- `operation`
- `input_ref`
- `output_ref`
- `state_before`
- `state_after`
- `timing`
- `metrics`
- `status`
- `error`
- `sequence_number`

This uniformity is what makes debugging and replay coherent.

### Runtime Modes

Two first-class modes. Voice-to-voice / realtime speech-to-speech
is explicitly not a runtime mode (see the "Chained Only" rationale
in Non-Goals and the Explicit Guardrails at the end).

`chained_pipeline`:

- transport → audio → VAD → STT → agent → TTS → transport
- discrete, well-defined stage boundaries

`text_session`:

- text-in, text-out directly against the agent bridge
- audio stages (`Transport`, `Audio`, `VAD`, `STT`, `TTS`,
  `Telephony`) are inactive
- `AgentStage` and `TurnStage` are active; `TurnStage` treats
  each `send_text` call as an explicit turn boundary (no
  endpointing needed)
- the same journal, the same bridge, the same framework transition
  records, the same interruption contract — just no audio around it
- intended use cases: running the agent under test without audio
  infrastructure, CI smoke tests that exercise the agent path,
  local REPL-style debugging of prompt or tool changes, reproducing
  agent bugs from a failing production bundle without replaying
  audio

Text mode is not a second agent framework or a separate code path.
It is the voice runtime with its audio stages disabled, driven by
an explicit text turn boundary instead of VAD endpointing. What
that buys, precisely:

- **Bugs in the agent, bridge, tool, interruption, or framework
  transition layers reproduce identically in text mode.** These
  are the layers that touch the same code paths in both modes.
- **Bugs in the audio layers (VAD, Smart Turn, STT, TTS,
  Transport, Telephony, noise reduction, echo cancellation, audio
  jitter, playback acknowledgement) still need voice mode to
  reproduce.** Disabling those stages disables the bugs that live
  in them.
- **LLM non-determinism is unchanged.** Text mode does not make
  an LLM turn deterministic; a replay from a text-mode bundle is
  as reproducible (or not) as a replay from a voice-mode bundle
  with the same `ReplayFidelity`.

Text mode's value is that it isolates non-audio bugs from audio
flakiness, not that it makes every voice bug reproducible. That
isolation is still load-bearing: the CI smoke path, the "I changed
the prompt, does the tool call still fire" loop, and the
production-bundle bug repro path all want the non-audio surface
exercised without the audio surface introducing noise. The
debugging mental model stays unified across both runtime modes
because the stage, journal, bridge, and interruption contracts are
the same — the only difference is which stages are instantiated.

## Agent Compatibility Boundary

Replace the current runner-centric adapter flow with a bridge-centric model:

- EasyCat owns the voice runtime
- OpenAI Agents and PydanticAI own agent behavior
- EasyCat bridges their native events into journal records

Currently, adapters inherit from `BaseAgentAdapter` and implement the
`StreamingAgent` protocol. They are tightly coupled to `AgentRunner`
expectations. The bridge model makes the boundary explicit and gives the
runtime structured access to framework execution state (handoffs, tool
calls, node transitions) as first-class observability records.

### `ExternalAgentBridge`

```python
class ExternalAgentBridge(Protocol):
    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]: ...

    def snapshot_state(self) -> dict[str, Any]: ...
    def apply_interruption(self, delivered_text: str, mode: str) -> None: ...
    def reset(self) -> None: ...
```

The bridge does not define tools, prompts, memory models, or routing rules.
It only translates framework-native runs into EasyCat runtime records.

### Voice State vs Framework State

This split is mandatory for voice correctness.

EasyCat owns:

- the voice delivery ledger
- playback-aware interruption decisions
- cancellation timing
- turn lifecycle
- TTS/STT/telephony coordination

The bridge owns:

- framework-native history representation
- framework-native interruption patching
- framework-native cancellation mapping
- framework-native state snapshots

This avoids pretending there is one generic conversation history across
OpenAI Agents and PydanticAI while still allowing shared voice behavior.

### Interruption and Cancellation Contract

The runtime decides when interruption happens. The bridge decides how that
interruption is represented inside the external framework.

Turn flow:

1. runtime detects interruption
2. runtime computes delivered assistant text from the voice delivery ledger
3. runtime selects a cancellation boundary
4. runtime requests bridge cancellation/drain behavior
5. runtime calls `apply_interruption(delivered_text, mode=...)`
6. bridge updates framework-native state
7. journal records both the voice event and the framework-state mutation

Cancellation supports three boundary modes:

- `immediate_stop`: stop streaming now and discard future non-essential events
- `drain_current_unit`: allow the current tool call or framework node to finish
- `drain_to_commit_point`: finish until the next safe framework state boundary,
  then stop before entering the next unit

The bridge must expose enough execution-state information for the runtime to
choose among those policies safely.

### Bridge Execution Cursor

The bridge maintains a typed cursor describing the active framework
execution unit:

- `unit_id`
- `unit_kind`: `agent | specialist | workflow_node | model_node | tool_call`
- `display_name`
- `parent_unit_id`
- `sequence`
- `entered_at`
- `committable`: whether state can be safely snapshotted here

This lets the journal and debugger show transitions inside a single user
turn without EasyCat inventing its own agent semantics.

#### Committable Boundary — Operational Definition

"Committable" is one of the load-bearing terms in this plan and is
used by the bridges (WS2), the interruption contract (WS2), the
stage replay path (WS3), and the replay entry-point enforcement
(WS4). It is defined once, here, in operational terms:

> A point in a bridge's execution is **committable** if and only if
> all three of the following hold at that point:
>
> 1. **Framework state is consistent.** The external framework's
>    in-memory representation (message history, node state, tool
>    call queue, response-chain pointers) is in a state the
>    framework itself would treat as valid — no half-applied
>    deltas, no in-flight tool call mid-dispatch, no partial
>    `ModelResponse` token accumulation.
> 2. **A `FrameworkStateSnapshot` taken here would round-trip.** A
>    snapshot captured at this point, persisted to an artifact,
>    loaded into a fresh bridge instance via `replay()` /
>    `from_snapshot()`, and resumed would produce the same
>    framework state and the same downstream behavior (modulo LLM
>    non-determinism).
> 3. **Interruption applied here is atomic.** Calling
>    `apply_interruption(delivered_text, mode)` at this point
>    mutates the framework and emits the corresponding journal
>    records without leaving the framework in a torn state on any
>    failure path.

When `committable=True`, *all three* of these guarantees hold. When
`committable=False`, at least one does not.

**What committable enables, concretely:**

- **Replay entry points (WS4 T4.8).** `RunBundle.replay_from(seq)`
  refuses to start replay at a non-committable sequence because
  the captured snapshot there cannot be resumed into a live bridge.
  Error points the user at the nearest committable sequences
  before and after.
- **Forked replay (peripheral follow-up).** Forked replay can only
  branch at committable boundaries because non-committable state
  is not round-trippable.
- **Drain-to-commit cancellation (WS2B T2B.1).** `CancellationMode
  .drain_to_commit_point` drains the bridge until the next
  committable cursor transition, then stops. A bridge that never
  marks anything committable within a turn degrades this mode to
  `drain_current_unit`.
- **Safe snapshot points for debugging.** Interactive debuggers
  freeze-frame at committable boundaries because stepping into a
  non-committable state leaves the framework inconsistent.

**Enumeration belongs to each bridge.** Which `unit_kind` values
are committable, and in which states, is bridge-specific and
published via a static `COMMITTABLE_BOUNDARIES` mapping (see
Workstream 2A T2.7.5). The runtime reads the mapping by reference;
it never guesses. Safe default when unknown: `committable=False`.

**Why the strict definition matters.** Relaxing any of the three
conditions produces a subtle class of replay bugs where a bundle
"loads" but downstream behavior diverges from production in ways
that look like LLM non-determinism. The three conditions are the
minimum needed to eliminate that class of bugs.

### Transition Records

Agent and workflow transitions are first-class journal records, not hidden
inside generic text/tool events:

- `FrameworkUnitEntered`
- `FrameworkUnitExited`
- `FrameworkStateCommitted`
- `FrameworkHandoff`
- `FrameworkToolPhaseChanged`
- `FrameworkCancellationBoundaryReached`

Each includes `from_unit`, `to_unit`, `transition_kind`, `reason`,
`framework_metadata`, `state_snapshot_ref`.

The important distinction:

- a **handoff** changes who owns the next model step
- a **node transition** changes the current execution substep within one framework
- a **tool phase transition** changes what work is in-flight but not who is in control

Normalization stops there. Deeper framework semantics stay in
framework-specific metadata.

### OpenAI Agents Bridge

Capture when available:

- rendered instructions
- model settings and run config
- response IDs and `previous_response_id`
- tool start/delta/result events
- handoff transitions
- framework-managed history snapshots
- provider request IDs and error payloads

Special handling:

- treat `last_agent` changes as committed handoffs
- record response-chain continuity separately from agent transitions
- support server-managed history cases where spoken-text patches must be
  represented as deferred notes rather than direct mutation
- distinguish text streaming cancellation from tool-call drain behavior

### PydanticAI Bridge

**One bridge, two input modes.** `PydanticAIBridge` accepts either
a `pydantic_ai.Agent` (Agent mode) or a `pydantic_graph.Graph`
(Graph mode) and provides deep event capture in both cases. Users
building single-agent apps and users building multi-agent
`pydantic_graph` workflows use the same bridge class — the Graph
mode layers `workflow_node` cursor entries on top of the exact
same inner event stream, so workflow users get the full PydanticAI
event taxonomy with graph-node context added, not a compromised
shim.

Both modes share one event translator module
(`_pydantic_ai_events.py`) that maps the PydanticAI event taxonomy
into `AgentRecorder` calls:

- `UserPromptNode`, `ModelRequestNode`, `CallToolsNode`, `End`
  transitions via `Agent.is_user_prompt_node()` /
  `is_model_request_node()` / `is_call_tools_node()`
- `PartStartEvent` and `PartDeltaEvent` (with `TextPartDelta`,
  `ThinkingPartDelta`, `ToolCallPartDelta`)
- `FunctionToolCallEvent` and `FunctionToolResultEvent` (tool name,
  args, `tool_call_id`, result content)
- `FinalResultEvent`
- final output object (including structured `output_type` values)
- `new_messages()` history updates
- provider request/response metadata and error payloads

**Agent mode** wraps a bare `pydantic_ai.Agent`. The bridge walks
`agent.iter()` (or consumes `agent.run_stream_events()`) directly,
emits `unit_kind="model_node"` during `ModelRequestNode` and
`unit_kind="tool_call"` during `CallToolsNode`, and patches
interruption by mutating the last `ModelResponse` `TextPart` in
place.

**Graph mode** wraps a `pydantic_graph.Graph` plus a state factory
and an initial-node factory. On each user turn it walks
`Graph.iter(initial_node, state=...)`, emits one
`unit_kind="workflow_node"` cursor per `BaseNode` visited (with
`display_name=type(node).__name__`), and forwards per-agent events
from agents called inside nodes via PydanticAI's own
`event_stream_handler` callback protocol. The bridge installs a
handler on the graph's state under a documented convention
(`state._easycat_event_handler`); graph nodes honor the convention
by passing `event_stream_handler=ctx.state._easycat_event_handler`
into their `agent.run(...)` / `agent.run_stream(...)` /
`agent.iter(...)` calls. Graph authors get deep bridging by
honoring a one-line convention; authors who ignore it still get
`workflow_node` entries but lose per-agent event capture inside
those nodes, and the bridge emits a warning journal record naming
the convention.

Graph-mode captures per turn:

- `Graph.iter()` walk — one cursor entry per node visited, with
  the node class name as `display_name`
- Transitions between different node types emit the
  `FrameworkUnitExited` → `FrameworkHandoff` → `FrameworkUnitEntered`
  triple (AC2.17) with `transition_kind="graph_transition"`
- Per-node agent calls produce nested cursor entries
  (`unit_kind="agent"`, `parent_unit_id=<workflow_node_id>`) with
  the full `PartStartEvent` / `PartDeltaEvent` / `FunctionToolCallEvent`
  / `FunctionToolResultEvent` / `FinalResultEvent` stream translated
  through the same shared event translator the Agent mode uses
- Graph state snapshot at committable boundaries (between nodes),
  stored via artifact ref because user-defined `State` dataclasses
  can be arbitrarily large
- `GraphRunResult.output` as the turn's final output
- `run.result.history` (the sequence of nodes visited) recorded as
  a workflow-level artifact

Graph-mode MCP pass-through forwards `EasyCatConfig.mcp_servers`
to every `pydantic_ai.Agent` instance referenced by the graph's
nodes, discovered via the constructor's `agents=` list or by
walking graph node definitions at construction time.

### Generic Workflow Bridge

Not every user builds with `pydantic_ai.Agent` or
`pydantic_graph.Graph`. The PydanticAI docs document several
custom orchestration patterns that sit outside those primitives —
programmatic app-loop hand-offs, output-type hand-off functions,
custom inference backends with hand-rolled tool dispatch, hybrid
designs mixing multiple frameworks — and EasyCat supports them as
first-class citizens via `GenericWorkflowBridge`.

The bridge wraps a user-defined `workflow` object implementing one
of two protocols, chosen by signature inspection at construction:

**Shallow mode (default).** The workflow implements
`on_user_turn(text) -> str` or
`on_user_turn_streaming(text) -> AsyncIterator[str]`. The bridge
wraps the whole turn in a single `workflow_node` cursor entry,
yields text output as `AgentBridgeEvent.text_delta`, and does not
see tool calls, sub-agent handoffs, or internal execution
structure. Low-effort integration — existing custom code usually
needs no changes beyond matching the protocol signature — but
debugging is limited to the turn boundary.

**Deep mode (opt-in).** The workflow implements
`on_user_turn(text, *, recorder: AgentRecorder,
cancel_token: CancelToken | None = None) -> AsyncIterator[str]`.
The bridge passes its `AgentRecorder` directly into the workflow
code, and the user calls `recorder.record_unit_entered` /
`record_tool_call` / `record_framework_handoff` /
`record_unit_exited` from inside their own orchestration to emit
whatever structure they want visible in the journal. The bridge
wraps the turn in an outer `workflow` cursor; everything the user
records shows up nested beneath it with the same shape as
framework-bridge records. No EasyCat-native tool code is
introduced — the user's own dispatch logic is what emits the
tool-call records.

Users can migrate from shallow to deep incrementally by adding the
`recorder` parameter and calling whichever recorder methods they
want to surface, one unit type at a time.

MCP pass-through in shallow mode logs a warning (the bridge
cannot know the user's inference backend). In deep mode, the
configured `mcp_servers` list is exposed on the recorder's
context so the user's workflow code can register it against
whatever backend it uses.

### Remote Agent Bridge (Responses API)

The three bridges above are in-process: the agent framework runs in
the same Python process as the voice runtime. The
`RemoteResponsesAPIBridge` is the fourth bridge — it speaks the OpenAI
Responses API over HTTP to a remote agent server, enabling
deployments where the agent runs on separate infrastructure from
the voice pipeline.

**Why the Responses API.** The wire protocol between the voice
runtime and a remote agent must handle: streaming text and tool-call
events during a turn, and interruption patching between turns. The
OpenAI Chat Completions API is too thin (no server-side tool
execution, no conversation continuity primitive, no structured
events beyond deltas). A custom WebSocket protocol would work
technically but imposes bad DX — every customer would need to
implement a proprietary message format with no ecosystem tooling.

The Responses API is the right level of richness:

- **Server-side tool execution.** The agent server executes tools
  internally and streams results back. The voice runtime sees
  `function_call_output` events but never proxies tool dispatch.
  The customer's tools run inside the customer's network.
- **Structured streaming events.** `response.output_item.added`,
  `response.content_part.delta`,
  `response.function_call_arguments.delta`,
  `response.output_item.done` — enough structure to populate
  journal records for tool calls, text deltas, and completion.
- **Conversation continuity via `previous_response_id`.** The
  voice runtime chains turns without sending full history on
  every request.
- **Ecosystem support.** The OpenAI Agents SDK speaks it natively.
  PydanticAI can target it. OpenResponses provides an open-source
  self-hostable implementation. Users can also point directly at
  the OpenAI API itself.

**Interruption via N-1 chain and partial replay.** When the user
barges in mid-response, the voice runtime:

1. Cancels the HTTP stream (standard). For `drain_current_unit`,
   it keeps reading SSE events until the current tool call's
   `response.output_item.done` arrives before cancelling.
2. Records the interrupted response's events (tool calls that
   completed, partial text delivered) — the voice runtime saw
   all of these from the SSE stream before cancellation.
3. On the next turn, chains from `previous_response_id` pointing
   at the **last fully completed response** (N-1, not the
   interrupted one), and includes the interrupted exchange as
   input items with the assistant text truncated to
   `delivered_text`:

```json
{
  "model": "...",
  "previous_response_id": "resp_N-1",
  "input": [
    {"role": "user", "content": "What's the weather?"},
    {"type": "function_call", "name": "get_weather",
     "arguments": "{\"city\": \"SF\"}", "call_id": "call_1"},
    {"type": "function_call_output", "call_id": "call_1",
     "output": "72°F, sunny"},
    {"role": "assistant", "content": "The weather is—"},
    {"role": "user", "content": "Never mind, tell me about stocks"}
  ]
}
```

The agent server sees a standard Responses API request. The
conversation history is correct: it includes completed tool calls
from the interrupted turn, the truncated assistant text the user
actually heard, and the new user input. No custom endpoint, no
custom protocol extension, no server-side cooperation required.

**Optional metadata convention for stateful agents.** Some agent
servers maintain state beyond conversation history (multi-agent
handoff chains, graph node positions, server-side memory). For
these, the voice runtime can send `easycat.*` metadata keys on the
next request to signal that the previous response was interrupted:

```json
{
  "metadata": {
    "easycat.interrupted_response_id": "resp_N",
    "easycat.delivered_text": "The weather is—",
    "easycat.cancellation_mode": "immediate_stop"
  }
}
```

Servers that understand `easycat.*` keys can patch their internal
state. Servers that don't understand them ignore the metadata
(standard Responses API behavior — metadata is opaque) and the
N-1 chain + partial replay handles the conversation history
correctly regardless. The voice runtime discovers server
capabilities from `easycat.supports_interruption` in response
metadata on the first turn.

**Debugging depth trade-off.** The remote bridge provides reduced
journal depth compared to the in-process bridges:

| Capability | In-process bridges | Remote bridge |
|---|---|---|
| Text deltas | Full | Full (from SSE stream) |
| Tool calls | Full (start/args/result) | Full (from SSE events) |
| Internal framework transitions | Full (per-node cursors) | Not visible |
| Framework state snapshots | Full (artifact-ref'd) | Local history only |
| Handoffs | Full (triple records) | Visible only if agent uses Responses API handoff conventions |
| Interruption | Framework state mutation | Local history patch |
| Committable boundaries | Per-framework mapping | Turn edges only |
| Replay fidelity | ARTIFACT / SIMULATED / LIVE | SIMULATED / LIVE only |

This is the right trade-off. Users who need full debugging depth
run in-process. Users who need deployment separation use the remote
bridge and accept the reduced observability. The three deployment
tiers:

- **Tier 1 — zero agent-side work.** Deploy any Responses API
  server (OpenAI API, OpenResponses, vLLM, custom). EasyCat calls
  it. Interruption uses N-1 chain + partial replay. Conversations
  work correctly. Journal captures turn-level events and tool
  calls from the stream.
- **Tier 2 — read the metadata.** Agent server reads `easycat.*`
  metadata keys and patches internal state on interruption.
  ~50 lines of middleware. Contributable as an OpenResponses
  plugin.
- **Tier 3 — in-process bridge.** Full `ExternalAgentBridge`
  implementation. Full debugging depth, all journal records,
  artifact replay.

### MCP Pass-Through

MCP pass-through is part of this plan — not because adding an external tool
standard is a debugging feature, but because it is the load-bearing
correctness test for the bridge boundary. If the bridge cannot forward MCP
server registration through to the underlying framework cleanly, the bridge
design is wrong and must be fixed before moving on.

```python
EasyCatConfig(agent=..., mcp_servers=[...])
```

Semantics:

- `mcp_servers` is a list of MCP URIs (stdio, SSE, HTTP)
- the bridge registers them with the underlying framework's MCP client at
  session construction
- connection lifecycle, auth, and tool discovery all live in the framework;
  EasyCat does not proxy calls
- MCP tool invocations appear in the journal through the bridge's existing
  tool-call events — no new record type is needed
- the OpenAI Agents bridge forwards to `Agent(mcp_servers=...)`; the
  PydanticAI bridge forwards to the agent's MCP toolset adapter

**Guardrail**: no EasyCat-native tool abstraction, registry, or decorator.
If the bridge cannot forward a tool cleanly, that is a bridge bug, not a
reason to grow EasyCat's surface.

## Debugger Surface (Core Only)

The interactive web debugger, dev waterfall, `--for=claude-code` export,
and live CLI all live in the follow-up plan. What this plan ships is the
data-plane surface they all build on.

### Default Behavior

Debugging defaults to `debug="light"`. `debug="off"` is an explicit opt-out
of capture, not the default.

### Debug Capability Matrix

`Session.journal` exists in every mode, but its capabilities differ
explicitly by `EasyCatConfig.debug`:

| `debug` value | Journal backend | Artifact capture | `session.journal` surface | `export_debug_bundle(...)` | Crash recovery | Replay |
|---|---|---|---|---|---|---|
| `"off"` | none | none | disabled view (`enabled=False`, zero records, `follow()` yields nothing) | unsupported; raises `DebugCaptureDisabledError` | unavailable | unavailable |
| `"light"` (default) | in-memory ring buffer | bounded in-memory artifact store; `replay_critical` preserved within the retention window, `debug_verbose` truncated/dropped by policy | read/slice/resolve/`follow()` available for retained records | best-effort from a live or just-finished session while required artifacts are still retained; clear failure if data has been evicted | unavailable | available only from retained in-memory capture; not crash-safe |
| `"full"` | durable backend (`sqlite`, `sqlite+litestream`, or `libsql`) | persistent artifact store with the same truncation/drop policy for `debug_verbose` payloads and durable retention for `replay_critical` payloads | read/slice/resolve/`follow()` available | supported | supported, subject to the backend's documented replication window | full essential replay surface |

Two clarifications are load-bearing:

- `debug="light"` is intentionally a live-debug mode, not a durability mode.
  If the process crashes or retention evicts required artifacts, export and
  replay may become unavailable for older records.
- `debug="full"` is the only mode that promises crash-recoverable bundles.
  Backends with remote replication (for example libSQL embedded replicas)
  may still have a documented trailing-loss window between local commit and
  remote sync; the backend must surface that window explicitly.

### Text Mode Entry Point

`Session.send_text(text: str) -> AsyncIterator[AgentBridgeEvent]`
is the public debug entry point for the `text_session` runtime mode.
It drives the same `AgentStage` / `ExternalAgentBridge` path the
voice runtime uses — same journal records, same framework
transition records, same interruption contract, same MCP
pass-through — with the audio stages inactive. A bug that
reproduces via `send_text` is the same bug the voice runtime
hits, which is the whole point of making text mode a first-class
runtime mode instead of a standalone chat helper.

### Live Journal Access

The core public debug surface shipped by this plan is:

- `session.journal` for live, read-only journal access
- `session.send_text(text)` for text-mode interaction with the agent
  bridge (text_session mode only)
- `session.export_debug_bundle(...)` for portable capture
- `RunBundle.load(path)` / `load_bundle(path)` for offline analysis

`session.journal` is always present. In `debug="off"` it is a disabled
view; in `debug="light"` and `debug="full"` it supports both point-in-time
reads and `follow()` for live tailing.

This is the supported migration path for users who currently depend on
event logging, tracing, or in-memory metrics exports.

### Debug Bundle Export

Production issues are exportable as a self-contained bundle:

- `session.export_debug_bundle(...)`
- optional inline artifacts
- stable schema for replay and regression tests
- provider version strings for reproduction fidelity
- dev-only banner on every essential-plan bundle until the peripheral
  `RedactionPolicy` ships (see `peripheral-redaction.md`)

### Replay

Three replay classes with explicit fidelity labels so users are never
surprised by non-determinism:

- `artifact_replay`: fully deterministic, VCR-style cassette playback from
  captured stage inputs/outputs. Suitable for STT and TTS stages.
- `simulated_replay`: mock-injected re-execution with captured context.
  Best-effort determinism. Suitable for the agent stage — LLM responses are
  inherently non-deterministic, and this must be documented clearly.
- `live_reexecution`: re-run from captured inputs against live providers.
  Non-deterministic. Suitable for reproduction attempts.

Each `ReplaySpec` carries its fidelity class explicitly.

Forked replay (time-travel from a chosen checkpoint) is a follow-up once
the three base classes are stable.

### Replay Safety for Side Effects

Replay must fail closed around tools and MCP. `ReplaySpec` therefore carries
an explicit `tool_policy`:

- `deny` (default): any tool or MCP invocation attempted during replay
  raises `ReplaySideEffectBlocked`. This is the default for both
  `SIMULATED` and `LIVE`.
- `stub`: the replay may satisfy a tool call only from captured tool
  results or explicit stub overrides supplied in the replay spec. No
  network, filesystem, or external side effect is allowed.
- `allow`: explicit opt-in for reproduction attempts that intentionally
  re-run real tool side effects. Replay logs a prominent warning and marks
  the resulting output as side-effecting/unsafe.

For simplicity and safety, the essential plan treats *all* tool and MCP
calls as side-effecting during replay unless they are satisfied by captured
results or explicit stubs. `artifact_replay` never re-enters a live
agent/tool path, and the agent stage's replay story remains
`simulated_replay` over captured bridge events.

### Redaction (Deferred)

A full `RedactionPolicy` write filter, `SafeConfigSnapshot` /
`SafeEnvironmentSnapshot` types, and an export-time second pass live
in `peripheral-redaction.md`, not in this plan.

The essential plan ships only the hard-coded "Config and Environment
Safety Default" described above: the journal cannot inline raw
`EasyCatConfig.__dict__` or `os.environ`, and only a small allowlist
of debug-useful fields is serialized. That is enough to avoid
accidental API-key leaks in dev but is **not** a production
redaction story.

Until the peripheral redaction work ships, essential-plan bundles
carry a dev-only banner and users are instructed not to attach them
to public issues or upload them to third-party services. Regulated
industry adoption is gated on that peripheral work landing.

## Runtime Changes

### Replace Multiple Observability Systems With One Journal

Today's `EventTraceLogger`, `Tracer`/`Span`/`SpanManager`, and
`InMemoryMetrics` become derived views over journal records.

Effects:

- logging becomes a journal formatter
- tracing becomes journal-derived
- metrics become journal-derived aggregations
- the future debugger UI becomes a journal reader

### Decompose the Session Monolith

`Session` (1,500+ lines, ~45 async methods) decomposes incrementally. A
big-bang rewrite of a 1,500-line class with comprehensive test coverage is
too risky. Each extraction preserves existing test behavior.

1. Extract per-turn state from Session instance variables into `TurnContext`
2. Extract interruption logic into `InterruptionController`
3. Extract voice delivery tracking into `VoiceDeliveryLedger`
4. Wrap existing provider calls with Stage interfaces (facade first, then
   migrate internals)
5. Session becomes a thin facade that wires stages and manages lifecycle

### Package Layout

```text
src/easycat/runtime/
  context.py
  journal.py
  records.py
  artifacts.py
  replay.py
  safe_defaults.py     # hard-coded allowlist for config/env snapshots

src/easycat/stages/
  base.py              # Stage protocol + shared helpers
  transport.py
  audio.py
  vad.py
  stt.py
  turn.py
  agent.py
  tts.py
  telephony.py

src/easycat/integrations/agents/
  base.py
  _pydantic_ai_events.py      # shared event translator
  openai_agents.py
  pydantic_ai.py              # unified Agent + pydantic_graph bridge
  generic_workflow.py         # shallow + deep modes for custom orchestration

src/easycat/debug/
  bundle.py
  export.py
```

Rewrite targets:

- `src/easycat/session/` (decompose the monolith)
- `src/easycat/agent_runner.py` (merge into stage + bridge)
- `src/easycat/event_logging.py` (replace with journal formatter)
- `src/easycat/tracing.py` (replace with journal-derived tracing)
- `src/easycat/metrics.py` (replace with journal-derived aggregations)
- `src/easycat/_span_manager.py` (absorbed by stage + journal)

Retained conceptually but rewritten around the new bridge boundary:

- `src/easycat/agents/openai_agents.py`
- `src/easycat/agents/pydantic_ai.py`
- `src/easycat/agents/pydantic_ai_workflow.py`
- `src/easycat/agents/factory.py`

## Migration Strategy

### Test-Driven Migration

The existing ~96-file test suite is the migration safety net. Every
refactoring step keeps integration tests green:

1. Add journal-based assertions alongside existing test infrastructure
2. Extract components behind interfaces that Session delegates to
3. Verify existing tests pass with new delegation (behavior-preserving refactor)
4. Add new journal-specific tests for new capabilities
5. Remove legacy observability code only after journal equivalents are proven

### User Migration

Breaking changes to config, imports, and adapter/debug surfaces are allowed
inside this plan. The requirement is that each workstream explicitly freeze
its public surface in the plan, document before/after usage, and update the
migration guide and release notes before legacy paths are removed.

### Release and Rollout

Workstreams ship incrementally under a `0.x` alpha tag. Each workstream
that introduces breaking changes is released as its own alpha bump
(`0.x.N-alphaWS{1..4}`) so external consumers can adopt them in order.
Breaking changes are batched per workstream completion rather than
trickled, so a single upgrade path covers all changes in that
workstream. The deprecation release covered by Workstream 5 T5.1 is
the final hardening pass before legacy modules are deleted: it adds
`DeprecationWarning`s on every symbol slated for removal, giving
external consumers one full version to migrate before the deletions
land in the workstream-5 release.

## Workstreams

Implementation is split into six workstreams, each with its own task
list, acceptance criteria, and verification procedure in a dedicated
file. This file contains the design rationale and target architecture;
the workstream files contain the operational plans.

Every workstream starts with an design review of its Phase N design before
implementation begins, keeps existing tests green throughout, and ends
with a completeness gate that must be closed before the next workstream
starts.

### Workstream 1: Journal Foundation

See `workstream-1-journal-foundation.md`.

**Goal**: replace `EventTraceLogger`, `Tracer`/`Span`/`SpanManager`, and
`InMemoryMetrics` with a single `ExecutionJournal` + `ArtifactStore`
backed by the hard-coded Config and Environment Safety Default and an
optional crash-durable SQLite backend. Strangler-fig adapters keep the
legacy systems running during migration. A full `RedactionPolicy`
write filter lands later in `peripheral-redaction.md`.

**Deliverable**: journal-backed observability, a read-only live journal
surface on `Session`, explicit migration notes for the new debug/config
surface, and the full existing test suite passing unmodified.

### Workstream 2A: Agent Bridge Protocol and Bridges

See `workstream-2a-agent-bridges.md`.

**Depends on**: Workstream 1.

**Goal**: replace the runner-centric adapter flow with an
`ExternalAgentBridge` protocol that exposes framework execution state
(handoffs, tool calls, node transitions) as first-class journal
records. Ship three concrete bridges (`OpenAIAgentsBridge`,
`PydanticAIBridge` with Agent + Graph modes, `GenericWorkflowBridge`
with shallow + deep modes), each capable of running turns
end-to-end with full event capture and single-phase
`apply_interruption`.

**Deliverable**: framework compatibility preserved, committed
handoffs and node transitions visible in the journal, bridges
shipped with `RecorderContext` side-channel, `unit()` context
manager, invariant enforcement, and `COMMITTABLE_BOUNDARIES`
publication. Unblocks Workstream 3.

### Workstream 2B: Interruption Contract and MCP Pass-Through

See `workstream-2b-interruption-and-mcp.md`.

**Depends on**: Workstream 2A. **Parallel dependency** with
Workstream 3 (the `InterruptionController` extracted in WS3 T3.2 is
the runtime-side consumer of WS2B's bridge-side contract, so WS2B
and WS3 land together rather than sequentially).

**Goal**: wrap the bridges shipped in WS2A with the four-step
journal-atomicity clause on `apply_interruption`, validate all three
cancellation modes (`immediate_stop`, `drain_current_unit`,
`drain_to_commit_point`) across every bridge/mode, ship the
shallow-mode downgrade path, and prove MCP pass-through on all
MCP-capable bridges. This is the workstream that validates the
bridge boundary correctness — if MCP pass-through cannot round-trip
cleanly through the bridges, the bridge design is wrong and must be
fixed before moving on.

**Deliverable**: atomic `apply_interruption` on every bridge,
paired `FrameworkStateCommitted`/`InterruptionApplyFailed` records
on failure paths, three cancellation modes tested end-to-end, MCP
forwarding proven on OpenAI Agents and PydanticAI (Agent + Graph
modes) with mock and filesystem-integration tests.

### Workstream 2C: Remote Agent Bridge (Responses API)

See `workstream-2c-remote-bridge.md`.

**Depends on**: Workstream 2A (bridge protocol must be stable).
**Runs in parallel** with Workstreams 2B and 3. Does not depend on
the `InterruptionController` or four-step atomic write ordering
because interruption is handled locally via N-1 chain + partial
input replay.

**Goal**: ship `RemoteResponsesAPIBridge`, a fourth `ExternalAgentBridge`
implementation that speaks the OpenAI Responses API over HTTP to a
remote agent server. Interruption uses the Responses API's native
`input` field to chain from the last completed response and replay
the interrupted exchange with truncated assistant text. Optional
`easycat.*` metadata convention for stateful agent servers.

**Deliverable**: remote agent deployment works end-to-end with any
Responses API-compatible server, interruption semantics are correct
via N-1 chain, journal captures turn-level events and tool calls
from the SSE stream, `EasyCatConfig` accepts a URL string for the
agent field, and the three deployment tiers (zero work / read
metadata / in-process) are documented with examples.

### Workstream 3: Stage Refactor and Session Decomposition

See `workstream-3-stage-refactor.md`.

**Depends on**: Workstream 1 and Workstream 2A. Runs in parallel
with Workstreams 2B and 2C.

**Goal**: decompose the 1,512-line `_session.py` into stage + context +
controller types. Extract `TurnContext`, `InterruptionController`,
`VoiceDeliveryLedger`, and `RunContext`. Port 8 stages (`Transport`,
`Audio`, `VAD`, `STT`, `Turn`, `Agent`, `TTS`, `Telephony`) behind a
common `Stage` protocol with bidirectional control signal flow. Reduce
Session to a thin facade.

**Deliverable**: coherent stage-based runtime with replayable
boundaries, Session reduced substantially toward the < 400-line target,
both `chained_pipeline` and `text_session` modes supported without
conceptual distortion.

### Workstream 4: Replay and Bundle Export

See `workstream-4-replay-and-bundle.md`.

**Depends on**: Workstreams 1, 2A, 2B, 2C, and 3.

**Goal**: make production failures local repro artifacts with honest
replay semantics. Ship `ReplaySpec` with three explicit fidelity classes
(`artifact`, `simulated`, `live`), `RunBundle` export with SHA-256
manifest and provider version strings, committable-boundary enforcement
so replay refuses to start at unsafe points, and minimal pytest fixture
helpers. Crashed sessions produce loadable bundles via Workstream 1's
crash-durability contract.

**Deliverable**: production failures become local repro artifacts,
crashed sessions can be turned into bundles after the fact, replay
fidelity is always explicit, and bundles persist only secret-safe
config/environment metadata.

### Workstream 5: Legacy Removal

See `workstream-5-legacy-removal.md`.

**Depends on**: Workstreams 1, 2A, 2B, 2C, 3, and 4.

**Goal**: delete the three legacy observability systems, the
`agent_runner.py` module, the strangler-fig adapters and feature flag,
and any duplicated state paths left on Session. Ship a migration guide
for external consumers.

**Deliverable**: cleaner codebase with one debugging model; a material
line-count reduction with 1,000 removed lines as a target rather than a
gate; `CLAUDE.md` updated; migration coverage for the removed public
surface shipped.

## Success Criteria

- Every turn has a stable journal trail.
- Every VAD decision and every Smart Turn endpointing decision is
  byte-identically reproducible from the captured journal and
  artifact store alone — no live provider call, no nondeterminism.
- Every major failure can be tied to one stage with concrete inputs and
  outputs.
- Both `chained_pipeline` and `text_session` runtime modes are supported
  without conceptual distortion. Voice-to-voice / realtime is
  explicitly unsupported — see Explicit Guardrails.
- Replay semantics are explicit about what is deterministic versus
  re-executed.
- No loadable journal or exported bundle contains dangling artifact refs;
  truncation/drop decisions are explicit in record metadata.
- Production failures can be exported and replayed locally, including from
  crashed sessions (journal is crash-durable with SQLite backend).
- Replay never executes side-effecting tools or MCP calls unless the caller
  explicitly opts into that behavior.
- The same underlying records power logs, metrics, and (eventually) the
  debugger UI.
- EasyCat still clearly presents itself as a runtime around external
  agents, not a new agent framework.
- MCP server pass-through works on all MCP-capable bridges with zero
  EasyCat-native tool code.
- Remote agent deployment works via the Responses API bridge with correct
  interruption semantics and no custom wire protocol.
- Session is no longer a monolith.
- The three pre-existing observability systems (`EventTraceLogger`,
  `Tracer`, `InMemoryMetrics`) are gone.

## Explicit Guardrails

The following are out of bounds for this redesign and for EasyCat in
general:

- **Voice-to-voice / realtime speech-to-speech APIs.** OpenAI
  Realtime, Gemini Live, Kyutai Moshi, and any future
  bidirectional-audio model that replaces the STT → Agent → TTS
  pipeline with a single streaming session. EasyCat is a chained
  voice runtime; the debug-first thesis only works when stage
  boundaries are discrete and replayable. Users who need
  voice-to-voice should use the provider SDK directly. This is
  a permanent scope decision, not a deferral — no
  `RealtimeBridge`, no `realtime_session` mode, no fused stages,
  no "we'll add it later".
- EasyCat-native tool API
- EasyCat-native MCP client or tool registry (pass-through only)
- EasyCat-native planner/router
- EasyCat-native memory layer
- EasyCat-native prompt compiler
- EasyCat-native multi-agent abstraction beyond compatibility bridges
- Hosted observability backend (self-hosted debugging first)
- A fourth progressive disclosure layer (three is the maximum)

If a feature would require owning agent semantics instead of recording or
transporting them, it stays outside EasyCat. If it requires EasyCat to
define a deeper cross-framework ontology for agent behavior than debugging
or replay needs, it also stays out of scope. If it requires the runtime
to treat continuous-audio bidirectional sessions as a first-class mode,
it stays out of scope.

## Appendix: Journal Record Schema

Concrete record types for Phase 0 agreement. All records share a base:

```python
@dataclass(frozen=True)
class JournalRecord:
    sequence: int              # monotonic within session
    op_id: str                 # stable across records in one logical op
    recorded_at_monotonic_ns: int
    recorded_at_utc: str       # RFC3339 UTC timestamp
    session_id: str
    run_id: str
    turn_id: str | None
    stage: str                 # e.g. "stt", "agent", "tts", "vad"
    operation: str             # e.g. "start", "complete", "error", "cancel"
    input_ref: str | None      # artifact store key
    output_ref: str | None     # artifact store key
    state_before: dict | None  # stage snapshot before operation
    state_after: dict | None   # stage snapshot after operation
    timing: TimingInfo         # wall_ms, cpu_ms, queue_ms
    metrics: dict[str, float]  # stage-specific counters
    status: str                # "ok", "error", "cancelled", "timeout"
    error: ErrorInfo | None
    metadata: dict[str, Any]   # stage-specific extras, including capture status
```

Framework transition records extend the base with typed fields:

```python
@dataclass(frozen=True)
class FrameworkTransitionRecord(JournalRecord):
    from_unit: str | None
    to_unit: str | None
    transition_kind: str       # "handoff", "node_change", "tool_phase"
    reason: str | None
    framework_metadata: dict[str, Any]
    state_snapshot_ref: str | None  # artifact store key
```

## Immediate Next Step

Start Workstream 1 by executing task T1.0 in
`workstream-1-journal-foundation.md`: write and merge the Phase 1
design covering journal record classes, artifact store
interface, backend selection, crash-durability contract, strangler-fig
wiring, and incremental test migration.

Each subsequent workstream opens with its own design-freeze task (T2.0, T3.0,
T4.0, T5.0), which must be reviewed and merged before implementation in
that workstream begins. Workstream plans gate workstream execution; the
essential plan in this file gates the workstream plans.
