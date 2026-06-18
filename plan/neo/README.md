# Neo Plan Index

Status: active next-major planning packet. **Phases 1–2 have shipped** (PR #283
`neo/phase-1-voice-app` for M1–M3; PR #284 `neo/phase-2-voice-server` for
M4–M8); **Phase 3 (M9–M13) is being implemented** on
`neo/phase-3-feedback-loop`, which is based on `origin/neo/phase-2-voice-server`
and contains all prior phase work.

This folder collects the implementation assets for the proposed next-major
EasyCat product surface. It is intentionally separate from the historical
roadmap/workstream plans so maintainers can evaluate, slice, and execute the
work without digging through chat transcripts.

## What Neo Means

Neo is the next-major direction for EasyCat:

> Make EasyCat the fastest path from “I have an agent” to a production-grade,
> inspectable, deployable voice product across local, browser, WebSocket,
> telephony, and CI/eval workflows.

The plan is organized around three phases:

1. **VoiceApp** (shipped, PR #283) — a product-level app surface with
   browser-first development and unified local/browser/WebSocket/Twilio modes.
2. **VoiceServer** (shipped, PR #284) — a production process layer with
   health/readiness, auth, metrics, and graceful shutdown, plus two
   separately-deliverable foundations:
   a manifest loader (M6a) and a declarative provider planner (M6b). M6 is split
   into M6a/M6b and is the highest-risk milestone. WebRTC keeps the number 7; a
   new M8 (server metrics + read-only endpoints) is inserted after M7, so only
   old M8–M12 shift to M9–M13 (see [roadmap.md](roadmap.md)).
3. **Feedback Loop** (in progress on `neo/phase-3-feedback-loop`) —
   always-available dev timelines, native evals,
   replay-as-tests (a security-sensitive *hardening* of the existing unsafe
   `journal promote` path — see below), and latency/cost budgets across runtime,
   debugger, CLI, validation, and CI.

## Assets

Read these in order:

1. [vision.md](vision.md): product thesis, north-star API, and non-goals.
2. [architecture-boundaries.md](architecture-boundaries.md): the core split
   between `Session`, `EasyConfig`, `VoiceApp`, `VoiceServer`, journals, and
   evals.
3. [phase-1-voice-app.md](phase-1-voice-app.md): implementation plan for
   `VoiceApp`, browser-first dev, unified modes, CLI migration, and Twilio
   extraction.
4. [phase-2-voice-server.md](phase-2-voice-server.md): implementation plan for
   the production server, auth, health/readiness, metrics, and graceful
   shutdown, plus the manifest loader (M6a) and the declarative provider planner
   (M6b) as two separate deliverables.
5. [phase-3-feedback-loop.md](phase-3-feedback-loop.md): implementation plan
   for debugger dev mode, eval/simulation APIs, replay promotion, and budgets.
6. [roadmap.md](roadmap.md): milestone ordering (M1–M13, with M6 split into
   M6a/M6b, WebRTC keeping the number 7, a new M8 (server metrics) inserted after
   M7, and old M8–M12 renumbered to M9–M13), dependencies, sequencing, and
   suggested first PRs.
7. [acceptance-matrix.md](acceptance-matrix.md): measurable acceptance criteria
   and test/doc evidence by phase.
8. [risk-register.md](risk-register.md): risks, failure modes, mitigations, and
   decision points.
9. [open-questions.md](open-questions.md): unresolved product/architecture
   decisions to answer before locking the major-version scope.

## How To Use This Folder

- Phases 1–2 (M1–M8) are **shipped** — see the per-milestone status notes in
  [roadmap.md](roadmap.md) and the status headers in
  [phase-1-voice-app.md](phase-1-voice-app.md) and
  [phase-2-voice-server.md](phase-2-voice-server.md). Phase 3 (M9–M13) is being
  implemented on `neo/phase-3-feedback-loop`; track scope in
  [phase-3-feedback-loop.md](phase-3-feedback-loop.md).
- Use [roadmap.md](roadmap.md) to split implementation PRs.
- Use [acceptance-matrix.md](acceptance-matrix.md) as the review checklist for
  each milestone.
- Keep this folder current as design decisions land; do not let it become a
  transcript archive.
- When a milestone ships, add a short status note to the relevant phase file and
  link the implementation PR.
- Treat the promotion-to-test workstream (Phase 3) as **security-sensitive**: it
  is a *hardening* of the existing, unsafe `journal promote` path (which today
  copies raw NDJSON + every audio blob + verbatim transcripts with zero
  redaction), not a preservation of already-safe behavior. Promotion must be
  redact-by-default with `--no-audio` as the default — review it accordingly.
- If docs route maps are edited as part of implementation, follow the repository
  guidance and regenerate `llms.txt` / `llms-full.txt` with
  `uv run python scripts/regen_llms_txt.py`.

## Source Baseline

An authoritative code-ground-truth review
([neo-plan-review.md](neo-plan-review.md)) has since been completed against the
current tree and folded into this packet. Every symbol named in the plan was
verified to exist at a compatible signature, so the original staleness caveat
**did not bite for symbol existence**. The corrections that were applied are
conceptual — *where* logic actually lives (e.g. capacity/draining is not in
`SessionManager`; the `config_factory` seam, not a `ConnectionContext` type) —
and net-new-vs-existing labeling (e.g. `assert_budgets_pass`,
`promote_turn_to_test`, the planner metadata for vad/transport/agent/noise/echo,
and the `easycat plan`/`easycat eval` CLIs are net-new, not re-exports).

The original draft was authored from a static read of branch `work` at commit
`18cbf07` with no `origin` remote configured; that note is now **superseded** by
the completed ground-truth verification above and should not be read as an
outstanding caveat.
