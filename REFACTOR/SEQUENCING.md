# Sequencing & Dependency Plan

This is the execution order for everything in `REFACTOR/`. It merges the maintainability plan
(`QW*` quick wins, `QS*` structural, `QP*` process) with the code-quality findings (`#N`) into
one dependency-aware sequence, and — critically — flags the **shared-file collisions** where a
maintainability refactor and a bug fix touch the same file, so they don't clobber each other.

Legend: **→** means "must land after". IDs: `QW`=quick win, `QS`=structural, `QP`=process,
`#N`=code-quality finding.

---

## 1. Hard dependencies (must respect)

- **QW5 (LICENSE, BSD-2-Clause) → QP2 (publish workflow).** Can't publish an unlicensed wheel.
- **QW6 (SHA-pin + `persist-credentials: false`) → QW7 (pre-commit/zizmor in CI)** so zizmor's
  first run is clean. Do QW7's ruff-version alignment same-PR-or-before its CI job.
- **QW10 (`_net.py` leaf module) → QS4 (Import Linter forbidden-contract ignore list)** — QW10
  deletes cycle workarounds, shrinking the ignore list QS4 must seed.
- **QS1 (module line-budget ratchet) FIRST**, then every split (QS2, QS3, QS5, QS6) shrinks its
  own allowlist entry as it lands.
- **QS4 Phase 2 (full `layers` contract) → after QS2/QS3/QS6** (import edges settle first).
- **#2 (cancel streamed run on barge-in) pairs with #24 (delete write-only `interrupted` flag)** —
  #24 is the visible residue of #2; fix together.
- **#1 (tool-result `call_id`) → #2** conceptually — #1's `pending` map must actually empty
  before #2's drain condition can ever fire. Land #1 first or same PR.

## 2. Shared-file collisions (sequence or reconcile — do NOT parallelize blind)

These files are touched by more than one work item. Landing them out of order causes merge pain
or silent regressions.

| File | Items that touch it | Handling |
|------|--------------------|----------|
| `tests/test_ci_workflow.py` | QW3, QW4, QW6, QP2 | Sequence QW4 → QW6; each updates the hardcoded action/`uv sync` asserts. Reconcile in one branch or rebase carefully. |
| `pyproject.toml` | QW2, QW5, QW8, QW9, QS1(dev dep), QS4(dev dep), QS5(mypy paths) | Small non-overlapping edits, but rebase order matters; land QW2/QW5/QW9 first (they're independent), tooling dev-deps later. |
| `transports/twilio_media.py` | **#19** (reconnect race, bug) + **#27** (dual-class dedup) | Fix **#19 first** in the current duplicated form (it's a real race); #27 later consolidates both copies. If #27 lands first, ensure the #19 guard lands in the single shared copy. |
| `transports/webrtc.py` | **QS6** (WebRTC convergence) + **#8** (signaling dup) + **#16**, **#34**, **#38**, **#40** | **#8 is a subset of QS6** — treat QS6 as the umbrella; fold #8 into it. Land the small independent bug/smell fixes (#16, #34, #38, #40) **before** the big QS6 extraction so QS6 moves already-correct code. |
| `server/webrtc_routes.py` | QS6, #8 | Same as above — part of the QS6 convergence. |
| `cli/debug/bundles.py` | **QS2** (split, 2445 lines) + **#4**, **#35**, **#36** (bugs) | Fix the three bugs **first** in place (small, targeted), then QS2 splits the corrected file behind a facade. |
| `runtime/journal_sql.py` | **#5** (sync-thread lock) + **#13** (crash-dump startup) | Independent regions of the same file; land in either order, one PR each. |
| `session/_session.py` | **#23** (dead re-check) + **#32** (stopping-window guard) + QW8 (`@deprecated` on shutdown/close/destroy) | #23/#32 are tiny; land them, then QW8 decorates the same methods. Sequence #32 → QW8 to avoid touching the guard twice. |
| `turn_manager.py` | **#6** (PTT in PROCESSING, bug) + **#26** (turn-start dedup) | Fix #6 first (behavioral), then #26 dedups the (now 3) turn-start paths including the one #6 touched. |
| `stt/elevenlabs_provider.py` | **#12** (batch retry) + **#42** (final-timeout config) + **#31** (batch-flush dedup) | Independent edits; land #12 and #42 (both add config fields) together, #31 dedup after. |
| `integrations/agents/openai_agents.py` | **#2** (cancel run) + **#24** (dead flag) + **#10**/**#30** (bridge dedup) | #2+#24 together first; bridge-cursor consolidation (#10/#30) after, on corrected code. |

## 3. Recommended wave order

Waves are batches that can proceed with minimal cross-contention. Within a wave, items are
mostly independent; respect the collision table above.

### Wave 0 — Freeze & unblock (land first, tiny)
- **QS1** module line-budget ratchet (freezes growth of the 13 oversized files before any split).
- **QW5** LICENSE (BSD-2-Clause, © 2026 Yi Ding) — unblocks QP2, zero code risk.
- **#1**, **#2 (+#24)** the two HIGH-severity OpenAI Agents bugs — highest correctness value.

### Wave 1 — Quick wins (hours each, mostly independent)
- CI/tooling batch, sequenced per collision table: **QW4** → **QW6** → **QW7**; plus **QW3**.
- Independent config/API quick wins: **QW1**, **QW2**, **QW8**, **QW9**, **QW10**, **QW11**.
- Priority bug batch: **#3**, **#3-config**, **#4**, **#5**, **#6**, **#7**, **#11**, **#12**,
  **#13**, **#14** (each small, test-backed; respect collisions on bundles.py / journal_sql.py /
  turn_manager.py / elevenlabs_provider.py).
- Low-bug + error-handling cleanup: **#18–#22**, **#32–#37**.
- Dead-code deletions: **#23**, **#24** (with #2), **#25**.
- Cheap smells: **#39**, **#40**, **#41**, **#42**, **#43**.

### Wave 2 — Structural refactors (multi-PR, sequenced)
- **QS2** split `cli/debug/bundles.py` (after its bugs #4/#35/#36 land).
- **QS3** split `debugger/server.py` (5 PRs).
- **QS5** `LaneHarness` extraction in `validation/runner.py` (4 PRs; after #7's redaction fix).
- **QS6** WebRTC convergence (umbrella for #8; after #16/#34/#38/#40 land).
- **QS4 Phase 1** Import Linter contracts (after QW10); **QS4 Phase 2** after the splits.
- Duplication consolidations: **#9**, **#10**, **#26**, **#27**, **#28**, **#29**, **#30**, **#31**
  (each "one helper at a time"; #27 after #19, #8 folded into QS6).

### Wave 3 — Process (set-and-forget)
- **QP2** publish via PyPI Trusted Publishing (after QW5 + the human PyPI registration steps).

---

## 4. Parallel-safe set

If multiple people/agents work at once, these have **no shared files** with each other and can
go fully in parallel: `QW1`, `QW2`, `QW9`, `QW11`, `#18` (providers.py), `#20` (artifacts.py),
`#21` (silero.py), `#33` (websocket_base.py), `#37` (_stt_committer.py), `#41` (telephony/server.py).

## 5. Notes on effort honesty

Several code-quality "bugs" are **narrow-reachability** (single-threaded asyncio makes the race
windows tiny) — they are recorded as latent-gap hardening, not firefighting. The docs are honest
about this per item. The two genuine "fix now" items are the HIGH-severity OpenAI Agents bugs
(#1/#2): they mis-bill, run tools after barge-in, and corrupt next-turn state on any tool-using turn.
