# Redaction and Safe Snapshots — Peripheral

> **This is a peripheral initiative.** It is not essential to the
> debug-first thesis in `essential-debug-first-runtime.md`. The
> essential plan ships a hard-coded "Config and Environment Safety
> Default" that prevents accidental API-key and env-var leaks, and
> stamps every bundle with a dev-only banner. That is enough for
> local debugging but not for any production deployment, any flow
> that ships bundles off-box, or any regulated-industry adoption.
> This file owns the full redaction policy that turns the journal
> into something users can legally share.
>
> **Sibling peripheral docs:**
>
> - `peripheral-dx-onboarding.md` — line budgets, library helpers,
>   template content, error diagnostics
> - `peripheral-cli.md` — `easycat` CLI, including `bundles export
>   --redaction ...` which applies the policies defined here
> - `peripheral-provider-ecosystem.md` — Deepgram Flux, Smart Turn
>   promotion, backchannel filter
> - `peripheral-observability-and-cost.md` — OTel export, cost
>   modeling, latency budgets, warmup stage
> - `peripheral-eval-and-debugger-ui.md` — `easycat.testing`,
>   Simulator + Judge, forked replay, interactive debugger UI, dev
>   waterfall
>
> **In scope (this file):** `RedactionPolicy` write filter with
> per-field strategies, `SafeConfigSnapshot` and
> `SafeEnvironmentSnapshot` typed snapshots, export-time second
> redaction pass, three ready-to-use policies (`development`,
> `production`, `regulated`), crash-dump redaction interaction,
> bundle banner upgrade, migration path off the essential plan's
> hard-coded allowlist.

## Context

The essential plan's journal captures state at every stage boundary,
and the essential `RunBundle` export format turns those records into
portable artifacts that cross process boundaries — pytest fixtures,
Claude Code context packs, bug report attachments, crash-dump files
on disk. The moment the journal becomes portable, everything inside
it becomes a potential leak:

- PHI transcripts from healthcare voicebots
- PCI data from payment collection flows
- voiceprint biometrics in audio artifacts
- API keys in provider request payloads
- bearer tokens and auth headers in framework state snapshots
- raw `os.environ` dumps in stage snapshots

The essential plan defers all of this to a dev-only banner and a
narrow hard-coded allowlist. That is the right call for the
debug-first thesis — redaction is not what makes the debugger work
— but it means essential-plan bundles cannot ship to production,
cannot be attached to public issues, and cannot power the
`--for=claude-code` / regulated-industry flows that the whole plan
is built around.

This peripheral turns the journal from a local debugging tool into
something users can legally share. It is a hard prerequisite for
any production deployment and for every cross-file feature that
exports data off-box (OTel spans, cost dashboards, CI bundle
fixtures, `easycat bundles export --for=claude-code`).

## Essential Plan's Minimum Guarantee (Starting Point)

Before this peripheral lands, the essential plan ships exactly one
hard-coded guardrail, documented in
`essential-debug-first-runtime.md` under "Config and Environment
Safety Default":

- The journal and artifact store MUST NOT inline
  `EasyCatConfig.__dict__` wholesale or `os.environ` wholesale.
- A small hard-coded allowlist captures the narrow set of config
  fields needed for debugging (provider role identifiers, model
  names, runtime mode, timeouts).
- A small hard-coded allowlist of env vars (`EASYCAT_*` variables
  that control the runtime itself) is the only environment
  metadata that gets serialized.
- Everything outside both allowlists is dropped.

This is not a policy — it is a safe default that prevents
accidental API-key leaks. Essential-plan bundles carry a banner:
**"Contains raw transcripts, tool args, and provider payloads.
Safe to share with your own team in dev; do not upload to
third-party services or attach to public issues until redaction
policy is configured."**

The essential plan reserves the `AgentRecorder` and stage-write
paths for a future redaction hook; this peripheral plugs the
`RedactionPolicy` into those hooks without changing the protocol
shape.

## `RedactionPolicy` Write Filter

Redaction is a **journal write filter**, not a post-hoc scrub, so
sensitive data never persists unredacted in the first place. You
cannot grep a SQLite file for a transcript you thought you had
sanitized if it never got written.

The policy controls per-field sensitivity with four strategies —
`redact`, `hash`, `drop`, `retain` — across the following field
categories:

- **transcript text capture**: STT partials, STT finals,
  agent-visible user text
- **audio retention**: VAD frames, STT raw audio, TTS output chunks
- **tool argument and result retention**: bridge-captured tool calls
- **provider payload retention**: raw request/response bodies from
  Deepgram, ElevenLabs, OpenAI, etc.
- **environment metadata exposure**: extended `os.environ` allowlist
  beyond the essential-plan default
- **framework state snapshots** (from essential WS2A bridges):
  message history, deps, model settings
- **stage state snapshots** (from essential WS3 stages): provider
  clients, configuration excerpts

### Filter Placement

The filter runs **inside** `journal.append` and inside every
artifact store write. Bridges and stages are forbidden from writing
raw snapshots outside the filter; this is an architectural
invariant, not a code-review guideline. A guardrail test injects a
known secret into a snapshot field configured `redact`, exports,
greps the backend and artifact store for the secret, and asserts
zero hits for every bridge and every stage.

### Strategy Semantics

- **`retain`**: the raw value is written verbatim. Equivalent to
  the essential plan's default for non-secret fields.
- **`redact`**: the raw value is replaced with a typed placeholder
  that preserves shape (`<redacted:transcript:42chars>`). Length
  and type are still recoverable for debugging; content is not.
- **`hash`**: the raw value is replaced with a stable SHA-256 hash
  truncated to 12 hex characters. Enables "did this field change
  across turns" comparisons without exposing content. Hash salt is
  per-session so bundles cannot be cross-referenced.
- **`drop`**: the field is removed entirely. Downstream readers see
  it as absent, not empty.

## `SafeConfigSnapshot` and `SafeEnvironmentSnapshot`

Typed, allowlisted snapshot types used by the journal and bundle
exporter wherever config or environment metadata is persisted.
These replace the essential plan's hard-coded allowlist with a
typed, user-extensible policy.

Rules:

- never serialize raw `EasyCatConfig.__dict__`
- never serialize raw environment variables wholesale
- include only fields explicitly marked safe for persistence
- hash, redact, or drop secrets such as API keys, bearer tokens,
  and auth headers
- encode large values or provider payloads via artifact refs rather
  than arbitrary nested objects

Until this peripheral ships, the essential plan's hard-coded
allowlist is the authoritative source of "what can appear in a
journal-writable config snapshot".

## Export-Time Second Pass

Debug bundle export (essential WS4 `Session.export_debug_bundle`)
accepts an optional `redaction=` argument that applies a **second,
potentially stricter** redaction pass on top of the runtime
default. A running session may retain transcripts for live
debugging, while a bundle exported from the same session drops them
entirely before leaving the process.

The stricter pass runs at export time, not at runtime, so the
original journal is untouched. Export redaction is idempotent: an
already-redacted field stays redacted.

## Crash-Durability Interaction

Crashed session bundles (essential WS4 `RunBundle.from_partial_journal`)
MUST pass through the redaction filter before being written to
`.easycat/crash-dumps/`. A crashed session where the redaction
filter itself was the source of the crash is the one degraded case:
the crash dump writes a marker record naming the filter failure and
falls back to the essential-plan hard-coded allowlist. Users
reading the dump see the marker and know the filter was bypassed.

## Ready-To-Use Policies

Three default `RedactionPolicy` values ship with this peripheral:

- **`development`** (default once this peripheral lands): retain
  transcripts and tool args, drop raw API keys and auth headers,
  retain provider model identifiers. Matches the essential plan's
  minimum guarantee plus richer retention of debug-useful fields.
- **`production`**: hash transcripts, drop audio, drop tool
  arguments (retain tool names only), redact all provider
  payloads, allowlist env via the `SafeEnvironmentSnapshot`
  default list.
- **`regulated`** (HIPAA/PCI-ready): drop transcripts entirely,
  drop audio, drop tool args and results, drop provider payloads,
  drop all environment metadata except runtime mode. Bundles are
  effectively structural — you can see which stages ran, which
  turns happened, which errors fired, but no payload content.

Users can compose their own `RedactionPolicy` or start from one of
the three and override specific fields.

## Bundle Banner Upgrade

Essential-plan bundles carry a dev-only banner noting they may
contain raw transcripts, tool args, and provider payloads. Once
this peripheral ships, the banner is replaced with a per-field
policy summary: the loaded `RunBundle` shows which fields were
retained, hashed, redacted, or dropped, and names the policy by
identifier (`development`, `production`, `regulated`, or a custom
policy hash).

## Dependencies on the Essential Plan

| Item | Depends on |
|---|---|
| `RedactionPolicy` write filter | essential Phase 1 (journal + artifact write paths) |
| `SafeConfigSnapshot` / `SafeEnvironmentSnapshot` | essential Phase 1 |
| Framework state snapshot redaction | essential Phase 2 (bridge snapshot shape) |
| Stage state snapshot redaction | essential Phase 3 (stage snapshot shape) |
| Export-time redaction second pass | essential Phase 4 (`export_debug_bundle`) |
| Crash-dump redaction pass | essential Phase 4 (`RunBundle.from_partial_journal`) |
| Bundle banner upgrade | essential Phase 4 (`RunBundle` loader) |

## Suggested Sequencing

Ship immediately after essential Phase 4 (Workstream 4) closes.
Without this peripheral, essential-plan bundles carry the dev-only
banner and users are instructed not to attach them to public
issues or upload them to third-party services. Regulated-industry
adoption and the `--for=claude-code` bundle flow are both gated on
this work landing.

Sequencing is strictly after WS4 because the export-time pass and
the `RunBundle` loader banner both extend APIs that WS4 introduces.
The write filter itself could land earlier, in principle, but
shipping it without the export-time pass and loader banner would
leave a half-finished story that is harder to document than
shipping the full thing once.

## Dependencies on Other Peripherals

- **`peripheral-cli.md`**: `easycat bundles export --for=claude-code`
  is gated on this peripheral because Claude Code context packs MUST
  use at least the `production` policy by default. The CLI's
  `--redaction` flag is the operator-facing surface; policy
  implementation lives here.
- **`peripheral-observability-and-cost.md`**: `JournalToOTelExporter`
  MUST apply the runtime `RedactionPolicy` before emitting spans;
  OTel backends are third-party systems and the same per-field
  strategies apply.
- **`peripheral-eval-and-debugger-ui.md`**: bundle-as-fixture pytest
  loading should default to `development` policy for local
  fixtures and `production` or `regulated` for CI fixtures shared
  across repositories.

## Competitive Context

- **LangSmith redaction controls**: field-level retention/drop is
  table stakes for any 2026 tracing product. LangSmith's model is
  a useful reference for the per-field strategy enum.
- **Pydantic Logfire**: ships an explicit "scrubber" that runs at
  span creation time. Same write-filter philosophy — sensitive data
  never hits the backend.
- **PHI/PCI compliance for voice**: Hamming AI, Vocode Enterprise,
  and Retell all advertise HIPAA-compatible deployments. The
  `regulated` default policy is what makes EasyCat credible in
  that conversation.
- **OWASP LLM Top 10 (2025)**: "Sensitive information disclosure"
  is LLM02. A voice framework without a redaction story fails the
  most basic LLM security review.
