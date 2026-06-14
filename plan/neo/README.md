# Neo Plan Index

Status: active next-major planning packet.

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

1. **VoiceApp** — a product-level app surface with browser-first development
   and unified local/browser/WebSocket/Twilio modes.
2. **VoiceServer** — a production process layer with manifest-first projects,
   health/readiness, auth, metrics, graceful shutdown, and provider planning.
3. **Feedback Loop** — always-available dev timelines, native evals,
   replay-as-tests, and latency/cost budgets across runtime, debugger, CLI,
   validation, and CI.

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
   production server, manifest loader, capability planning, auth, health,
   metrics, and graceful shutdown.
5. [phase-3-feedback-loop.md](phase-3-feedback-loop.md): implementation plan
   for debugger dev mode, eval/simulation APIs, replay promotion, and budgets.
6. [roadmap.md](roadmap.md): milestone ordering, dependencies, sequencing, and
   suggested first PRs.
7. [acceptance-matrix.md](acceptance-matrix.md): measurable acceptance criteria
   and test/doc evidence by phase.
8. [risk-register.md](risk-register.md): risks, failure modes, mitigations, and
   decision points.
9. [open-questions.md](open-questions.md): unresolved product/architecture
   decisions to answer before locking the major-version scope.

## How To Use This Folder

- Use [roadmap.md](roadmap.md) to split implementation PRs.
- Use [acceptance-matrix.md](acceptance-matrix.md) as the review checklist for
  each milestone.
- Keep this folder current as design decisions land; do not let it become a
  transcript archive.
- When a milestone ships, add a short status note to the relevant phase file and
  link the implementation PR.
- If docs route maps are edited as part of implementation, follow the repository
  guidance and regenerate `llms.txt` / `llms-full.txt` with
  `uv run python scripts/regen_llms_txt.py`.

## Source Baseline

This plan was drafted after static code exploration of the current branch. The
attempt to pull latest `origin/main` failed because the checkout has no `origin`
remote configured. The current local branch was `work` at commit `18cbf07`.
