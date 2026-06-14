# Neo Risk Register

Status: active planning risk log.

## Summary

Neo touches user-facing APIs, server runtime, observability, evals, and docs.
The risks below should be reviewed before each milestone and updated when a
mitigation lands.

## Risks

### R1 — `VoiceApp` duplicates `EasyConfig`

**Risk:** `VoiceApp` grows provider/audio/telephony fields until it becomes a
parallel config system.

**Impact:** Divergent behavior, duplicated validation, confusing docs.

**Mitigation:** Keep `VoiceApp` as orchestration. It builds or clones
`EasyConfig` and delegates to `create_session`.

**Decision rule:** If a field controls one session’s provider/pipeline behavior,
it belongs in `EasyConfig` or a grouped config dataclass. If it controls how an
app is run, it belongs in `VoiceApp`.

### R2 — `VoiceServer` becomes a new pipeline runtime

**Risk:** Server code starts constructing providers or managing turn/audio logic.

**Impact:** Divergence from `Session`, harder testing, inconsistent behavior.

**Mitigation:** `VoiceServer` accepts factories returning `EasyConfig` or
`Session`. If it gets `EasyConfig`, it calls `create_session`.

### R3 — Multi-session shutdown races

**Risk:** Server calls `SessionManager.stop_all()` while active connection tasks
are adding/removing sessions.

**Impact:** Orphaned sessions, noisy teardown, flaky tests.

**Mitigation:** `VoiceServer` owns connection tasks, marks draining, stops
listeners, waits/cancels handlers, then drains sessions.

### R4 — Auth behavior breaks existing local demos

**Risk:** Production-safe auth defaults make local/browser demos harder.

**Impact:** Worse onboarding.

**Mitigation:** Keep loopback/dev no-auth defaults where safe; require auth for
non-loopback binds unless explicitly disabled.

### R5 — Secret leakage in planning/metrics

**Risk:** Provider planning or server metrics expose tokens, phone numbers,
headers, session IDs, or transcript text.

**Impact:** Security/privacy regression.

**Mitigation:** Use env-var references in manifests, redact planning output, and
allow only low-cardinality safe metric labels.

### R6 — Provider planner diverges from provider factories

**Risk:** `easycat plan` says a config is valid, but `create_session` later
fails; or vice versa.

**Impact:** Loss of trust in planning/readiness.

**Mitigation:** Extract/shared provider and transport metadata rather than
duplicating factory rules. Planner should report likely issues, but final
creation remains authoritative.

### R7 — Debugger autolaunch surprises production users

**Risk:** Durable `debug="full"` starts opening browser tabs or binding UI
servers unexpectedly.

**Impact:** Production instability and broken current assumptions.

**Mitigation:** Keep autolaunch explicitly dev-only: `EASYCAT_DEV=1`,
`VoiceApp(dev=True)`, or explicit `debugger_autolaunch=True`.

### R8 — Promoted tests contain PII or audio unexpectedly

**Risk:** Production bundle promotion writes transcript, tool results, or audio
into committed test fixtures without clear warning.

**Impact:** Privacy/security incident.

**Mitigation:** Promotion CLI prints PII warning, defaults to no audio, supports
redaction, and clearly labels fixture fidelity.

### R9 — Replay executes tools by default

**Risk:** Production replay accidentally calls real tools/APIs.

**Impact:** External side effects, user-impacting actions, cost.

**Mitigation:** Keep tool replay denied by default. Require explicit opt-in and
make tests assert the default.

### R10 — Budget stage names fragment

**Risk:** Runtime, validation, evals, CLI, and debugger use slightly different
names for the same latency milestone.

**Impact:** Confusing reports and brittle tests.

**Mitigation:** Centralize budget models/reporting in `easycat.budgets`; keep
aliases for existing `total_ms`, `tts_ttfb_ms`, and `llm_ttft_ms`.

### R11 — `aiohttp`/FastAPI split confuses server users

**Risk:** WebRTC/debugger use aiohttp, Twilio examples use FastAPI, and
`VoiceServer` chooses one path.

**Impact:** Harder docs and extension story.

**Mitigation:** Use aiohttp internally for `VoiceServer` first because existing
WebRTC/debugger surfaces already use it. Keep FastAPI as an adapter/future
integration rather than a core dependency.

### R12 — Next-major scope balloons

**Risk:** VoiceApp, VoiceServer, manifests, planning, evals, debugger UI, and
budgets all land in one giant branch.

**Impact:** Hard review, high regression risk.

**Mitigation:** Follow `roadmap.md` milestones. Ship additive slices with tests
and docs before deprecating old paths.

## Decision Points To Revisit

- Should `VoiceApp` be top-level only, or also `easycat.app.VoiceApp`?
- Should project manifest be TOML, YAML, or both?
- Should `easycat serve` mean dev server, production server, or both with mode
  flags?
- Should `VoiceServer` expose Prometheus text directly or rely on OpenTelemetry
  first?
- How much Twilio server functionality belongs in Phase 1 versus Phase 2?
