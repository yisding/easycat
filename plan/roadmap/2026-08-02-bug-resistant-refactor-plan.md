# Bug-resistant refactor: implementation plan

Status: active backlog.
Date: 2026-08-02 (re-reviewed after PR 795/main; accepted concurrency,
epoch/session, execution/measurement, and security findings incorporated).
Companion to
[the architecture proposal](2026-08-02-bug-resistant-architecture.md), which
holds the evidence and rationale. This document holds the work: concrete,
PR-sized tasks with files, acceptance criteria, and ordering. Read the
proposal first; sections referenced as §N below are its sections.

## How to use this plan

- Every numbered or lettered delivery slice is a **one-concern PR**:
  independently landable, independently
  revertible, behavior-preserving unless explicitly marked **[behavior fix]**
  (a bug being fixed) or **[behavior change]** (an intentional semantic
  change that needs its own test asserting the new semantics).
- **Tiers gate spend** (§10 of the proposal): Tier A ships unconditionally.
  A deterministic Tier-A exit gate permits only WS2.1's first vertical slice;
  bulk Tier B adoption waits for the 60-day Tier-A outcome gate. Tier C also
  requires the peer-set decision (below) and the Tier-B outcome gate.
- Each workstream names focused verification targets. The global regression
  net is `just check`; each PR records the exact focused command it ran.
- Progress inventory: the ratchet baselines (WS0.3). Outcome metric: the
  family-scoped recurrence gate in "Re-measurement".
- Source symbols and behaviors are normative; line anchors are wayfinding from
  the pinned audit and must be refreshed with `rg` in each implementation PR.
- Sizes: S ≈ under half a day, M ≈ a day, L ≈ multi-day. Honest engineering
  effort if everything ships: ~45-60 PR-days. The mandatory 60-day observation
  gates are serial: Tier C cannot be eligible earlier than roughly five to six
  months after Tier-A work begins, before adding maintainer scheduling delays.
  Review bandwidth is the binding constraint; any PR touching more than ~10
  source files is too big for this codebase's history and must be split.

## Blocking decision: the peer set

The critique's T1 recommends demoting several agent bridges to out-of-tree
and deleting WebTransport; as of 2026-08-02 none of those cuts has been
decided or made. **Record the peer-set decision before any per-peer
implementation migration starts.** It gates WS1.4, WS2.3, WS2.6, WS3.2+,
WS4.2+, and WS5.1's backend migrations. Contract suites and peer-neutral
primitives may proceed; urgent product/security fixes do not wait. Every gated
item migrates the *retained* set only.
Building shared machinery around peers whose existence is an open question
either wastes the work or forecloses the decision by momentum.

Acceptance for the decision: a checked-in ADR names every transport and agent
bridge, its retained/in-tree/out-of-tree/deleted disposition, compatibility or
migration obligations, and the owner/date for any deferred removal. A vague
"keep for now" does not unblock a migration; an undecided peer stays excluded.

## Dependency graph

```
WS6.1a manifest/rubric ──→ Tier-A implementation begins
WS0.1a ──→ WS0.1b ──→ WS0.3a ──→ WS0.3b                  [Tier A]
WS0.2a ──→ WS0.2b1 ──→ WS0.2b2 ──→ WS0.2b3 ──→ WS0.2b4 [Tier A]
WS0 (all slices) ──→ WS1.1 ──→ WS1.2a-e + WS1.3         [Tier A]
WS2.7a, WS3.1, WS4.1, WS5.2, WS6.1b                     [Tier A]
                  │
                  ▼
        deterministic Tier-A exit ──→ named WS2.1 vertical slice
                                              │
                                              ▼
                                        14-day slice soak
                                              │
                                              ├──→ WS2.1 foundation complete
                                              └──→ WS1.2f-g

Tier-A completion SHA/date ──→ A60 ──→ WS1.4, WS1.5, WS2.2-2.6,
                                      WS2.7b-c, WS5.1, WS5.3       [Tier B]

Peer ADR + bridge B60 + WS3.1 + retained-peer sketches ──→ WS3.2+ [Tier C]
Peer ADR + transport B60 + WS4.1 + WS1.4 + WS2.3 + WS5.1
                                                        ──→ WS4.2+ [Tier C]
```

Note: **WS2.1 must land before WS2.2-2.6** so every adopted scope has an
explicit lifecycle root and, when Session-owned, a named teardown cohort from
its first PR. An adoption may not broaden the pipeline broadcast cohort.

---

## WS0 — Foundations (Tier A)

### WS0.1a — concurrency and survivor contract [M-L]

Create `src/easycat/_concurrency.py` (package-root leaf, no intra-package
imports, mirroring `_net.py`/`_env.py`). It owns the low-level task creation
needed by these helpers and the concrete ownership value types; `RuntimeScope`
composes them in WS2.1.

- `checkpoint_pending_cancellation()` — promote the private helper from
  `runtime/scope.py:16-19`.
- `RuntimeSupervisor` / `SurvivorRegistry` / `OwnedTask` — one explicitly
  anchored ownership chain. All child scopes under a lifecycle root share its
  registry; reservations charge both the root quota and the single supervisor
  for that event-loop/application runtime, so N roots × K cannot grow without
  bound. Standalone lifecycles obtain that same supervisor rather than creating
  private ones. The supervisor stores task + stable `owner_id` metadata, never
  a Session/object reference, and remains the strong anchor after owner drop.
- `await start_owned(factory, *, registry, owner_id, task_name) -> OwnedTask` —
  the only parkable-task constructor. This is deliberately async: the helper
  must checkpoint a pending caller cancellation both before reservation and
  after the injected `reserved` journal callback, then retain a task if its
  factory requests cancellation while creating it. A synchronous constructor
  cannot distinguish those phases. It reserves capacity before invoking the
  awaitable factory. Bare coroutine objects are rejected, and an already
  running task can be adopted only when its reservation was allocated at its
  original spawn.
- Reservation states are explicit and exhaustive:
  `reserved -> active -> released` on normal/exceptional settlement, or
  `active -> parked -> released` when the eventual result is consumed.
  Failure/cancellation before factory invocation releases `reserved`;
  duplicate parking of the same `OwnedTask` is idempotent and creates no
  second reservation. No state evicts, drops, or replaces a live task. A
  journal callback is injected without importing the journal package.
- `reap(owned, *, timeout=None) -> BaseException | None` — cancel-and-await a
  child without consuming the caller's own cancellation: sleep(0)
  checkpoint, `cancelling()` baseline capture, conditional re-raise,
  trailing-cancel case. **Returns** the child's exception instead of
  choosing a policy — the four canonical sources disagree on
  swallow-vs-raise (`twilio_media.py:1703-1723` logs,
  `runtime/scope.py:331-341` re-raises first,
  `_turn_runner.py:597-609` propagates, `server/transports.py:617-618`
  swallows), so the policy belongs to the caller, explicitly. On the
  caller-cancel re-raise path, a still-pending child is parked through its
  `OwnedTask`; a settled child releases capacity before caller cancellation is
  re-raised.
- `shielded_cleanup(factory) -> CleanupSettlement` — the
  `while not task.done(): shield` loop, but with an explicit result contract.
  `CleanupSettlement` records cleanup result/error and caller-cancellation
  requests; the caller selects precedence synchronously. No cleanup exception
  or caller cancellation is silently discarded.
- `hard_timeout(owned, deadline)` — bound the wait, cancel, park the survivor,
  and return a typed outcome. `deadline` is an absolute loop-clock deadline;
  duration-based callers convert once at their boundary. Unify
  `server/transports.py:624-668`,
  `transports/webtransport.py:158-183`, and the inline variant at
  `transports/_webrtc_audio.py:367-401`. Ownership is **hierarchy-scoped,
  runtime-bounded, strongly anchored, and journaled** — replacing the
  three module-global ledgers. A bare global task set would leak cross-session
  and pin Sessions; the bounded supervisor instead owns isolated root
  registries and string metadata. Rule:
  parking is forbidden while the parked task holds a lifecycle lock;
  teardown paths must release locks before parkable awaits (see the twilio
  `_lifecycle_lock` hazard — a parked lock-holder bricks the transport
  permanently). WS0.1a therefore introduces a supervisor-aware
  `LifecycleLock`: plain `asyncio.Lock` exposes only `locked()`, not the owning
  task, so it cannot enforce this contract. The WS0.1b vertical adoption must
  validate this surface before the concurrency API is frozen.
- `swallow_cancel()` — **an async context manager** (`async with`): the
  checkpoint must be awaited in `__aenter__` before the `cancelling()`
  baseline is captured; a sync CM cannot distinguish a pre-entry pending
  cancel from a swallowable child cancel. The only sanctioned suppression
  form; optional journal hook.

Capacity is an admission limit, not a retention hint. A never-finishing
survivor consumes its root + supervisor reservation and leaves its owner in
observable terminal state `closed_with_survivors`; the owner rejects new work,
exposes survivor metadata to the postmortem journal, and may retry escalation
without claiming it drained. Root close never reports a clean drain while that
state remains.

New stated invariant: **no `Task.uncancel()` outside `_concurrency.py`**
(ratcheted in WS0.3) — the baseline scheme breaks silently otherwise.
`asyncio.timeout()` calls `uncancel()` internally, so composability with it
is part of the spec.

Tests (`tests/test_concurrency.py`), at minimum: every reservation transition;
factory not invoked at root/supervisor capacity; normal and exceptional
release; caller cancellation before factory invocation and before/after child
creation; reap of finished/cancelled/failing/pending children; duplicate park;
aggregate capacity across child scopes and roots; dropping the lifecycle owner
while a pending survivor remains supervisor-anchored; final release on
completion; hard-timeout deadline/caller-cancel races; lock-holder rejection;
`CleanupSettlement` over cleanup success/failure crossed with one/repeated
caller cancellations; and `swallow_cancel` pre-entry plus
`asyncio.timeout()` composition.

Acceptance: every parkable task starts through `start_owned`; helpers have no
hidden unbounded ledger or unowned-task fallback.

### WS0.1b — prove the helper contract in one vertical slice [M]

Before freezing the API or adding the spawn ratchet, migrate one complete
`server/transports.py` caller (factory creation through owner close) onto an
explicit server-runtime supervisor/registry. Preserve its current exception
policy. The legacy private helper and `_BACKGROUND_TIMEOUT_TASKS` stay
grandfathered for the module's other callers until WS2.5; this slice neither
claims nor attempts partial ledger retirement. Tests cover external caller
cancellation, hard timeout, never-finishing survivor, owner drop, journal
attribution, and both quota levels. If the slice needs caller-specific escape
hatches, revise WS0.1a before freezing it.

Implementation target: the proving caller is
`WebSocketSessionRuntime`'s `server.wait_closed` stage. Application entry
points inject one explicit `RuntimeSupervisor`; the runtime creates one named
root registry and exposes it for later child scopes. A timed-out or externally
cancelled listener wait is retried through the same `OwnedTask`, so the
listener factory is never invoked concurrently. Cooperative cancellation
requested by the hard deadline remains an incomplete cleanup to retry, while
independent listener exceptions retain the legacy propagate policy. The
manager sweep, connection close, handler cancellation, `_safe_await`, and
`_BACKGROUND_TIMEOUT_TASKS` remain unchanged and grandfathered.

### WS0.2a — teardown-budget manifest and no-growth inventory [M]

Create a checked-in manifest classifying timeout/deadline declarations and
defaults plus the calls that enforce them in
`stop`/`close`/`disconnect`/`drain`/`cancel` closures as
`lifecycle_budget`, `protocol_local`, `configurable`, or `not_teardown`.
Starting inventory includes the previously listed Session/turn/audio/
WebRTC-aclose/llama/server values **and** current-main omissions such as
`_OFFER_CANCEL_DRAIN_TIMEOUT_S`, `_CANCEL_SEND_TIMEOUT`,
`_SOCKET_CLOSE_SEND_TIMEOUT`, `_POST_DONE_STREAM_DRAIN_TIMEOUT_S`,
`_COMPLETED_STREAM_DRAIN_TIMEOUT_S`, and browser-event send/close bounds.

An AST-based no-growth test requires every source result to have exactly one
manifest classification and a non-empty rationale. Fingerprints use relative
path, enclosing qualname, construct, normalized surrounding AST, and an
occurrence index, so line-only movement is stable and delete-plus-add changes
remain visible. Updating requires explicit `--update-baseline` plus a reviewed
rationale; new skeleton entries remain `unclassified` and intentionally fail
until reviewed. False-positive classifications explain why the site is not a
teardown budget.

Freeze result after WS0.3b: 159 sites, comprising 32 timeout-bearing defaults,
61 bounded calls in lifecycle closures, and 66 named declarations. Their
reviewed classifications are 71 `lifecycle_budget`, 41 `protocol_local`, 17
`configurable`, and 30 `not_teardown`.

Acceptance: manifest/source bijection, deliberate additions fail the ratchet,
line insertion preserves fingerprints, regeneration preserves reviewed
classifications, and no entry has an empty classification or rationale.

### WS0.2b1 — central agent lifecycle-budget defaults [S]

Create `teardown_budgets.py` and move the three concrete agent cleanup
defaults identified by WS0.2a: post-`done` stream drain, Llama post-cancel
await, and completed Remote Responses stream drain. Import aliases preserve
the local semantic names and call shapes while leaving one canonical value.

Acceptance: all three values and behaviors are unchanged, focused agent
cancellation/drain tests pass, and the manifest moves the three declarations
without changing classification totals.

### WS0.2b2 — central Session lifecycle-budget defaults [M]

Move the concrete Session/audio/turn cleanup defaults, including force-start
lock and superseded-stop waits, barge-in cutoff, outbound audio drain, inline
send cancellation grace, and application-prompt drain. Keep the configurable
STT timeout source in `TimeoutConfig`; its lifecycle call sites consume that
policy but do not define a second numeric default.

Acceptance: Session teardown, prompt cancellation, audio drain, stalled-send,
and barge-in behavior retain their exact values and focused regressions pass.

Migration result: eight concrete values move to the canonical module. The
manifest grows from 159 to 165 sites because six inline or non-timeout-named
values now have explicit timeout declarations; the reviewed classification
totals become 77 `lifecycle_budget`, 41 `protocol_local`, 17 `configurable`,
and 30 `not_teardown` without adding runtime budget enforcement.

### WS0.2b3 — central runtime and transport lifecycle-budget defaults [M]

Move the concrete journal and WebRTC audio/offer cleanup defaults.
`WebTransportConnectionTransport.wait_closed(timeout=None)` is an opt-out
sentinel rather than a concrete default, while WebTransport handler reaping and
listener close consume the configurable server force-shutdown value and move
with WS0.2b4. Protocol-local sends, handshakes, idle bounds, and
acknowledgements remain with their protocols.

Acceptance: close/cancel/drain behavior and values are unchanged across each
runtime and transport, and their focused lifecycle suites pass.

Migration result: six concrete values — four journal process/thread joins and
two WebRTC cleanup bounds. Naming four formerly inline journal values and
retaining the overridable WebRTC audio class alias grows the reviewed manifest
to 170 sites, including 82 `lifecycle_budget` entries.

### WS0.2b4 — central configurable server lifecycle defaults [M]

Move the remaining server lifecycle defaults and make configurable voice,
WebSocket, WebRTC, WebTransport, and Twilio server fields draw unchanged
defaults from `teardown_budgets.py`. Public configuration remains authoritative
at every call site. `reap(..., timeout=None)` remains a sentinel meaning no
default deadline, rather than inventing a concrete policy value. The analogous
internal cleanup and WebTransport `wait_closed(timeout=None)` sentinels remain
unchanged, and the configurable STT provider close timeout stays protocol-owned.

The 159-site inventory showed that combining discovery, classification, and
these migrations would exceed this plan's review-size limit. Each child slice
updates the manifest in the same PR. Moving an existing named declaration
preserves its classification; turning an inline literal into a canonical named
default may add a reviewed declaration entry whose rationale points to the
existing enforcement site. This remains default consolidation until WS2.1
consumes lifecycle entries as named phase/policy budgets.

Acceptance: every concrete server lifecycle default has one canonical
definition, public configuration behavior and values are unchanged,
protocol-local values remain local, and the WS0.2a manifest/source bijection
stays green.

Migration result: three canonical values now supply thirteen public server
configuration/default sites without changing their `30.0`, `10.0`, and `5.0`
values. The reviewed manifest grows from 170 to 173 sites, with 20
`configurable`, 82 `lifecycle_budget`, 41 `protocol_local`, and 30
`not_teardown` entries. WS0.2 is complete without changing runtime enforcement.

### WS0.3a — Enforcement: structural call-site ratchets [M]

All grandfathering is production-source-only (`src/easycat/`); tests,
examples, docs, perf tools, and scripts may create raw tasks to orchestrate
races. File-wide Ruff ignores are not a ratchet: they permit unlimited new
violations in an already grandfathered file, and TID251 cannot reliably infer
an arbitrary variable named `loop`.

**AST-based pytest ratchets (`tests/ratchets/`)** record structural call-site
fingerprints: relative path, enclosing qualname, callee/construct shape, and a
normalized surrounding-AST hash with locations removed. An occurrence index
is used only to distinguish identical normalized subtrees. They fail when an
old call is deleted and a different call of the same shape is added elsewhere
in the function, and update only via an explicit
`--update-baseline` flag plus a reviewed rationale. They cover:

- `asyncio.create_task`, `asyncio.ensure_future`, qualified
  `loop.create_task`, and statically resolvable import/assignment aliases
  outside `_concurrency.py` and `runtime/scope.py`; name resolution is lexical
  and deliberately does not guess through `getattr`/reflection
- `Task.cancelling` outside `_concurrency.py` / `runtime/scope.py`
- `Task.uncancel` outside `_concurrency.py`
- `except asyncio.CancelledError` arms outside `_concurrency.py`
- `gather(..., return_exceptions=True)` in cancellation paths (the
  suppression shape the original inventory missed — 31 sites)
- module-global `set[asyncio.Task]` declarations
- shield-loop shape (`while not t.done():` + `shield` in body)
- `generation`/`_epoch` integer-field declarations outside the epoch
  module, with the STT audio-accounting watermarks excluded by
  module list (else the baseline is polluted from day one)

Baselines are **measured at freeze time** (the proposal's counts are
evidence, not baselines; measured 2026-08-02: spawns ~90, cancelled-suppress
~117 by the AST shape, `cancelling()` 61, instance ledgers 12,
module-global ledgers 4, shield-loops 13, epoch-field declarations ~23
unscoped / ~15 with watermarks excluded).

Freeze result after WS0.1b: the checked-in structural baseline records 89 raw
spawns, 109 `CancelledError` handlers, 57 `cancelling()` calls, 31
`gather(return_exceptions=True)` calls, four module task/future ledgers, 12
inline shield loops, 18 generation/epoch field declarations, and zero
`uncancel()` calls outside `_concurrency.py`. The zero baseline makes the last
category a structural hard ban before WS0.3b adds Ruff's qualified-call layer.
Epoch findings are declarations only (module/class fields or `self` fields
initialized in `__init__`), with the named STT accounting modules excluded;
later resets do not inflate progress. Reviewed baseline changes require
`--update-baseline` and a non-empty `--baseline-rationale`.

Also: the C901/PLR grandfather list **stays in ruff**; extend
`tests/test_complexity_ignores.py` to fingerprint current violations and
reject new violations inside an already ignored file, rather than merely
checking that the ignore-entry count decreases. Add the `mkdocs.yml`
nav-coverage guard (verified 2026-08-02: still missing).

Documentation: a short section in `CONTRIBUTING.md` explaining the
enforcement rule and the §3 review rule (a recurring-class fix lands in the
primitive/engine or adds an allowlist entry — never silently).

Acceptance: CI green at frozen baselines; deliberate new `create_task` calls
in both a new production file and an already-grandfathered production
function fail; the same construct in a race test remains permitted; a
delete-plus-add replacement, insert-before-existing call, import alias,
assigned alias, and deliberate new `except CancelledError` all have regression
fixtures that fail the ratchet.

### WS0.3b — Enforcement: qualified zero-baseline hard bans [S]

After WS0.3a freezes and verifies the structural baseline, Ruff banned-api
(TID251) becomes a second hard ban for each statically qualified API whose
repository-wide production-source baseline is zero, beginning with
`asyncio.Task.uncancel`. Keep the lint-extension lists synchronized in
`pyproject.toml`, `CLAUDE.md`, and the `justfile`. There are no per-file spawn
waivers. `_concurrency.py` and `runtime/scope.py` remain the only sanctioned
raw-spawn modules; the AST ratchet continues to cover instance calls,
`loop.create_task`, and dynamically shaped calls Ruff cannot prove.

Acceptance: Ruff rejects a qualified zero-baseline API fixture, the AST
ratchet rejects the equivalent instance-call fixture, and the documented lint
policy matches the executable configuration.

### WS0.4 — Journal discarded teardown-gather failures [S]

Correction from review: `server/voice_server.py:842` and
`server/webrtc_routes.py:513` are **not** untracked fire-and-forget — both
are gathered and awaited under a hard timeout (`voice_server.py:845-848`,
`webrtc_routes.py:517-525`). The real residue is that their
`return_exceptions=True` results are silently discarded. Inspect only
exception results with task identity; classify `CancelledError` caused by the
preceding explicit teardown cancellation as expected, and journal unexpected
exceptions. Tests: expected cancellation emits no failure, while a genuine
finalizer exception is observable with its task name.

### WS0.5 — Websocket resampler-tail fence [S] [behavior fix]

Moved forward from the epoch workstream on review: this is a live race
(`transports/websocket.py:198-203` enqueues the flushed resampler tail with
no liveness check; `webrtc.py:1279-1283` fences the identical operation)
and it needs nothing from the Epoch primitive — `websocket.py` already has
`_connection_generation` (`:382,:436,:460,:513`). Capture the generation at
loop start, compare in the `finally`, matching the webrtc shape. Regression
test: a late tail flush after reconnect does not emit onto the new
connection. WS1.4 later converts the mechanism like every other site.

---

## WS1 — Staleness primitive (Epoch/Lease)

### WS1.1 — `_epoch.py` primitive [M-L] (Tier A)

New `src/easycat/_epoch.py` (leaf, like `_turn_context.py`): `Epoch`
(owned, bumpable), `Lease` (captured; `is_current()`,
`guard(on_stale='skip'|'raise')`, **and `lease.value`** — the payload
captured atomically with the epoch read, without which callers re-read the
live pointer beside the lease check and recreate the torn read), `Stale`
exception.

The docstring must state (per the concurrency review — these are the
design, not implementation detail):

- **Guarantees:** `is_current()`/`guard()` are exact at the check point;
  `lease.value` is the payload that was current at capture.
- **Non-guarantees:** nothing is atomic across an `await`. Binding an epoch to
  a scope requests prompt unwinding, but tasks may catch cancellation, shield
  cleanup, or deliberately resist it. Therefore every liveness-sensitive
  commit re-guards immediately before its effect even after scope binding,
  unless a mechanically enforced no-suppression region is introduced later.
- **Threading:** `guard()` is loop-only; off-loop use (provider threads) is
  capture-then-reverify-on-loop; `is_current()` cross-thread is advisory.
  The memory model is stated explicitly (mutex over bump+payload+capture),
  not by analogy to `CancelToken` (whose one-way latch is a different
  model).

Unit suite: capture/bump ordering; guard on both stale policies; atomic
(epoch, payload) capture under concurrent bump; bump-on-clear; threaded
bump during a loop-side guard; re-guard-after-await example as an
executable doc test.

Implementation result: the leaf primitive starts at generation zero,
atomically publishes replacements through `bump(value)`, and returns immutable
leases from `capture()`. A lease holds its captured generation and payload,
offers a thread-safe advisory `is_current()`, and makes loop-only `guard()`
either return `False` or raise `Stale`. The module-level contract documents the
mutex memory model and the mandatory re-guard immediately before an effect;
domain adoption remains in WS1.2+.

### WS1.2 — turn identity and activity inside `session/` [L, split a-g]

Use one primitive but **two semantic state machines**: a Session-owned turn-
identity Epoch and a TurnManager-owned activity-phase Epoch/latch. Gated replay
proves they cannot be one counter: `reset(preserve_token=True)` ends the
manager-active phase while the installed turn, token, agent stream, and replay
bookkeeping intentionally remain live.

- **a. Writer/phase inventory + guards (Tier A).** Check in every identity
  publish/replace/clear and activity transition, including
  `TurnManager.begin_application_turn`, `TurnManager.reset`,
  `bot_stopped_speaking()` → IDLE, `Session.begin_turn`, all `TurnStarted`
  producers/subscribers, application/VAD/PTT/barge-in/replay paths, and text
  turns. Add registration/AST guards so new writers cannot bypass the listed
  seams. Freeze the current behavior whereby a hand-built public
  `TurnStarted` can command a Session install.
- **b1. Canonical Session identity owner (Tier A).** Add a synchronous Session
  `TurnLifecycle.publish_identity/clear_identity` seam. Replacing/clearing
  `Session._turn` bumps identity, and `TurnContext.generation` is dual-written
  from the identity epoch during migration with debug assertions.
- **b2. Canonical manager activity owner (Tier A).** Add the distinct manager
  activity transition seam. Manager reset/IDLE bumps activity but does not
  stale identity when gated replay retains it.
- **c. Private lifecycle before public observation (Tier A).** Internal
  producers create a private `TurnPublication` carrying the exact leases and
  await a private lifecycle callback that preserves today's handoff/STT-start/
  install order; only then do they emit a marked public `TurnStarted`
  observation. The public event carries no internal lease. A reserved internal
  pre-handler (registration order guarded by test) no-ops marked observations
  and routes unmarked hand-built events through the same lifecycle callback
  before user handlers, preserving the current command compatibility without a
  second public emission. Text-turn events are marked observation-only and
  never mutate voice-turn identity. If this cannot preserve the compatibility
  contract, making public events observation-only is a separately approved
  **[behavior change]**, not hidden in this PR.
- **d1. Synchronous predicate inventory + guard (Tier A).** Freeze and
  classify identity pointer/generation, activity, cancellation, token-owner,
  phase-latch, and null-object predicates in `_tts_scheduler.py`,
  `_stt_committer.py`, and `_turn_runner.py` before migration.
- **d2. Migrate synchronous predicates (Tier A).** Convert the classified
  identity/activity predicates to leases as their semantics require.
  Null-object, cancellation, token-owner, and phase-latch checks retain their
  distinct meanings.
- **e. Commit guards and phase latches (Tier A).** Inventory every await-to-
  effect edge and guard immediately before each liveness-sensitive commit.
  Scope cancellation never substitutes for this guard. Preserve
  `_preemptive_finalized_generation` as a per-turn one-way
  `preemptive_take_closed` phase latch; it is not an identity Epoch. Freeze the
  late-STT-final-during-`end_stream` race.
- **f. Turn child scope + compound-predicate deletion (Tier B).** Only after
  the 14-day slice soak, completed WS2.1 foundation, and a membership inventory
  of every per-turn task, bind identity invalidation to prompt scope unwinding.
  Then replace `_cancel_cleanup_owns_turn` and remaining generation checks with
  identity/activity/token guards, still re-guarding at commits. Tests freeze
  manager-started, session-only, application, VAD, push-to-talk, replay,
  clear/reset, hand-built event, and successor-during-cleanup behavior.
- **g. Remove the old identity carrier (Tier B).** Remove public reads of
  `TurnContext.generation`, dual-write assertions, and the matching baseline
  only after every writer, member, and predicate is migrated.

WS1.2a freeze result: 43 classified sites — six direct identity-pointer
assignments, six old-carrier assignments, three identity publish calls, ten
identity clear calls, three activity-state assignments, seven canonical
transition calls, three reset calls, three `TurnStarted` producers, and two
subscriptions. The manifest distinguishes the one command producer/subscriber
pair from the two observational application/text producers and browser
observer. A behavior contract freezes a correlated hand-built `TurnStarted`
installing Session identity before a later public type subscriber observes it.

WS1.2b1 migration result: `TurnLifecycle` now owns the Session identity Epoch;
all production publications and clears use its synchronous seams, while the
private `_turn` compatibility property routes focused harnesses through the
same owner. The refreshed 44-site manifest has zero direct production
turn-pointer assignments, one identity-owner initialization, six publication
calls, fifteen clear calls, and four legacy-carrier assignments. The only new
carrier write is the asserted `TurnContext.generation` dual-write. Manager
activity ownership remains isolated to WS1.2b2.

WS1.2b2 migration result: TurnManager now owns an
`Epoch[TurnManagerState]`, and `_transition` is its sole writer. The refreshed
45-site manifest has zero direct activity-state assignments, one activity-owner
initialization, one Epoch bump site, nine calls through the transition seam,
and the same three external reset calls. Ordinary transitions retain their
existing log and journal observations; reset and focused-harness compatibility
setup remain silent while still bumping activity. Adversarial contracts prove
that repeated IDLE resets and bot-stopped-to-IDLE stale prior activity leases,
while `reset(preserve_token=True)` leaves the retained Session identity current
and its token uncancelled.

WS1.2c migration result: voice, application, text, and hand-built paths now
construct a private `TurnPublication`; voice publication carries the exact
manager token/activity lease into a directly awaited Session lifecycle
callback, which returns the installed identity lease before the public event is
emitted. Internal public `TurnStarted` events are privately marked
observation-only without adding a public field or carrying a lease. A reserved
Session pre-handler runs before global, exact-type, and parent observers: it
no-ops those marked observations and routes unmarked hand-built events through
the same lifecycle callback without a second emission. The refreshed 51-site
manifest records four private publication constructors, one callback binding,
two observation markers, two public producer sites, and two subscriptions; no
public producer remains an identity command. Ordering contracts prove existing
global and exact observers see voice identity installed and STT active, while a
running text session's TurnStarted observation never installs voice identity.

WS1.2d1 inventory result: 73 synchronous predicate sites are frozen across the
three turn collaborators — eight identity-pointer checks, nine legacy identity-
generation checks, nine activity-state checks, fourteen cancellation checks,
three token-owner checks, twenty-nine null/no-turn checks, and one explicit
preemptive phase latch. The location-free AST ratchet distinguishes all seven
semantics and rejects structural replacement drift. WS1.2d2 must remove the
pointer/generation/activity liveness mechanisms through lease adoption while
leaving cancellation, token ownership, null-object semantics, and the one-way
phase latch independently visible.

WS1.2d2 is split into WS1.2d2a identity-lease adoption and WS1.2d2b activity-
lease adoption so the two state machines can be reviewed and rolled back
independently. WS1.2d2a migration result: the Session wiring now exposes atomic
identity capture; `TurnRunner` carries the captured lease through end-of-speech,
streaming TTS, application prompts, and preemptive attempts; and `TTSScheduler`
re-guards that same lease before every post-await stop, drain, mark, or clear
effect. Same-object republication contracts prove an epoch bump fences stale
work even when pointer identity is unchanged. The refreshed 55-site predicate
manifest has zero identity-pointer and zero legacy identity-generation checks;
the nine activity checks remain deliberately frozen for WS1.2d2b, alongside
the independent cancellation, token-owner, null-object, and phase-latch
semantics.

WS1.2d2b migration result: delayed segment commits re-guard the captured pause
activity immediately before provider dispatch; `TurnEnded` captures processing
activity beside identity and carries both through end-of-speech; and the
manager returns the exact bot-speaking/idle leases published by its playback
transitions so TTS admission and finalization never reconstruct state from a
live read. Same-state pause, processing, and bot-speaking republication
contracts prove those bumps fence stale commits and settlement independently of
identity. The refreshed 46-site predicate manifest has zero manager-state,
zero identity-pointer, and zero legacy identity-generation liveness reads; only
the explicitly independent cancellation, token-owner, null-object, and phase-
latch predicates remain.

WS1.2e is split into WS1.2e1 commit-edge inventory and WS1.2e2 guard completion
so discovery drift is independently reviewable from the behavioral migration.
WS1.2e1 freezes 66 turn-scoped effects across the STT committer, TTS scheduler,
and turn runner: five manager-activity commits, eight identity commits, one
one-way phase-latch commit, eleven provider dispatches, thirty-one public
observations, one Session lifecycle commit, and nine turn-bookkeeping writes.
The location-free manifest records each effect's own AST fingerprint plus its
suspension relationship: forty-five are directly awaited, sixteen occur after
an earlier await, and five are synchronous. A structural guard also pins the
existing behavioral regression for a trailing STT final racing `end_stream`,
including its end-of-speech take, late-final injection, and agent-call witness.
WS1.2e2 will classify liveness requirements for these frozen effects and add or
prove the commit-time identity/activity/token/phase guards without using
cancellation as a substitute.

WS1.2e2 migration result: all 66 effects now carry an explicit reviewed
boundary — three admission guards, four identity guards, thirty-three combined
identity/activity guards, one identity/phase guard, one one-way phase latch,
six publication-scoped effects, two Session-scoped effects, five serialized
text-task effects, and eleven correlated diagnostic observations that
intentionally survive stale cleanup. Private publication admission re-guards
activity after preemptive and predecessor drains; STT pause commits carry exact
identity beside activity and recheck both after provider suspension; STT event
consumers reject stale same-object republications; transcript and first-audio
bookkeeping re-guard immediately before mutation; and first-payload lifecycle,
queued synthesis, voice output, application-text output, raw-stage history,
simple-agent history, bridge-shadow history, and prepared-response history all
use an exact commit predicate. Adversarial contracts cover identity and
same-state activity republication during every newly guarded suspension class.
The predicate ratchet remains at 46 sites with no identity-pointer, legacy
generation, or manager-state liveness reads; one cancellation-as-stream-
admission check was removed, leaving thirteen cancellation checks, while the
application-text ownership branch adds one explicit null-object check. The
late-STT-final phase contract remains independent and unchanged.

Acceptance: both state-machine inventories and guards are complete; gated
replay keeps identity current while invalidating activity; every effect has a
commit-time guard; public-event compatibility is either preserved or separately
approved; final predicates use identity/activity/token/phase semantics rather
than a catch-all counter.

### WS1.3 — TurnManager pause generation [M] (Tier A)

`turn_manager.py:216,432-441,540-541,613,636` `_pause_generation` and the
`_pause_generation_by_future` plumbing in `_stt_committer.py:104` become an
Epoch owned by TurnManager with leases carried by the futures.

WS1.3 is split into WS1.3a inventory and WS1.3b migration so integer/future
correlation discovery is independently reviewable from the Epoch conversion.
WS1.3a freezes 21 sites: two owner writes and five private owner reads in
TurnManager, one public generation read in STTCommitter, four receivers and
three call-boundary handoffs, plus one future-map owner, one correlation write,
and four correlation takes. Location-free fingerprints reject structural
replacement drift. Structural guards also pin the cancellation-resistant
smart-turn timer race and the delayed segment-final race, proving that neither
an old timer nor an old future correlation may shorten a later pause.

WS1.3b migration result: TurnManager now owns a dedicated `Epoch[None]` and
opens each pause before `VADStopSpeaking` becomes observable. The silence
timer and STT commit path carry exact `Lease[None]` values; segment futures
retain their originating lease until the matching final or cleanup consumes
it. Seven commit guards reject stale timers, provider continuations, failed-
commit recovery, and punctuation hints. The public integer generation seam
and integer future map are gone. The evolved location-free inventory freezes
27 epoch, capture, carrier, guard, and correlation sites, while end-to-end
audio-router evidence proves a zero-delay commit both follows the boundary
audio write and captures the newly opened pause.

### WS1.4 — Transport connection epochs [M per transport] (Tier B; peer-gated)

After the peer-set decision and Tier-A outcome gate, one PR per retained
transport: `websocket.py` `_connection_generation`,
`twilio_media.py` (`:1465,:1559,:1595`), `webrtc.py`
`_peer_generation`/`_retiring_peer_generation` (`:167-168,:239-242` — the
two-field retire case becomes two epochs or a lease held across handoff).
Mechanism conversion only; the websocket fence bug already landed in WS0.5.

### WS1.5 — Telephony epochs [M] (Tier B)

`telephony/ivr.py:250,311` (`_activation_epoch`, 18 call sites),
`telephony/outbound.py:292` (`_lifecycle_epoch`) **and the separate
`_placement_epoch`** (checks at `:419,:442,:450,:688`, definition `:521` —
a fourth telephony mechanism the first draft missed),
`telephony/call_state.py:133,324` (`CallState._generation`).

Out of scope for WS1: STT audio-accounting watermarks
(`_audio_epoch`, `_committed_through_epoch`, `_stream_generation`, …) — they
are arithmetic over stream positions, not liveness fences.

---

## WS2 — Scope tree (Tier B; WS2.7a is a Tier-A safety net)

### WS2.1 — Extend `RuntimeScope` [L, three PRs]

Keep this foundation reviewable and make the soak SHA unambiguous:

- **2.1a — named vertical slice:** after the Tier-A structural gate, add child
  hierarchy, explicit parent/root attachment, the WS0.1 supervisor/registry,
  and one Session-owned `_audio_router` inline-send cohort in
  `runtime/scope.py` + `session/_audio_router.py`. Its merge SHA starts the
  14-day soak; no other package adoption is part of it.
- **2.1b — policy/cohort engine:** after that soak, add mode-dependent policy,
  named phase barriers, escalation, and graceful-to-force supersession.
- **2.1c — finalization/result model:** add ordered finalizer nodes and retained
  terminal results, then run the full current-stop mapping before WS1.2f.

The final API in `runtime/scope.py` includes:

- **Child scopes** registering with the parent.
- **Named teardown phases and cohorts**, not one root broadcast. Current force
  semantics require an early turn-work phase (text cancel/drain, prompt
  cancel/drain, preemptive drain), then a synchronous pipeline/TTS/outbound
  broadcast barrier before draining that cohort, then STT/runtime/barge-in
  cleanup while providers remain live, followed by ordered provider/transport
  finalization. `signal_cohort(name)` and `drain_cohort(name)` express those
  partial-order edges. Siblings inside a cohort have no invented total order.
- **Ordered finalizer nodes** (AsyncExitStack-style members) so non-task
  steps — `stop_ingress`, `_outbound_queue.close()`,
  `transport.disconnect()`, provider closes — hold ordered positions
  between task drains.
- **Mode-dependent member policy** with orthogonal fields: `cohort`,
  `signal_token`, `task_action=finish|cancel`, `grace_deadline`, and
  `hard_deadline`. The policy table freezes observable behavior before code:

  | member | graceful | force |
  |---|---|---|
  | application prompt | no token signal; finish; no deadline | signal token; cancel; existing drain bound |
  | text turn | signal token; cancel; existing bound | same |
  | preemptive generation | cooperative signal/drain | cooperative signal/drain before pipeline barrier |
  | pipeline/TTS/outbound cohort | current graceful action per member | synchronous cancel barrier, then drain |
  | cancellation-resistant cleanup | signal as currently specified; finish | signal; hard deadline; park if still live |

  The complete mapping in WS2.7a adds every current member before the rewrite.
  There is no unconditional token-cancel first step: `signal_token=False` is
  required for the graceful application prompt.
- **Escalation** applies the selected policy fields in phase order; token
  cancellation never implies scope cancellation. At the hard deadline, an
  `OwnedTask` remains anchored/parked through its existing reservation.
- **Admission control**: closed/closing states; spawn into a closed scope
  is rejected deterministically (coroutine closed, journaled), including
  `spawn_from_sync` racing close from another thread.
- **Close-supersede protocol**: a concurrent `close(force=True)` overtakes
  an in-flight graceful close (the ownership-transfer machinery currently
  implemented inline in `Session.stop()` moves here).
- `BackgroundTaskScope`: a retained-terminal-result mode for named slots
  whose errors are currently *raised* from `disconnect()` — its
  log-and-drop `_on_done` would otherwise silently change error
  propagation, so retained results are mandatory for parity.

All child scopes share the lifecycle root registry and charge the runtime
supervisor quota. `closed_with_survivors` is distinct from clean `closed`;
capacity is reserved by `await start_owned` before factory invocation. Survivor
completion may transition the owner to clean `closed`; retry escalation and
postmortem inspection remain available while terminal-with-survivors.

Tests: each named partial-order edge without total-order assumptions; all
policy-field combinations used by the mapping; graceful prompt completes with
an uncancelled token; force prompt cancellation precedes the pipeline barrier;
token-only signal leaves unrelated members running; budgets come from the
manifest; closed-scope spawn rejection across threads; force supersession at
every phase; aggregate child/root quota exhaustion; owner-drop anchoring; and
survivor completion/state transition.

### WS2.2-2.6 — Package adoption [M each]

One coherent slice per PR (a package may require several slices under the
10-source-file limit), converting spawn idioms to scopes, drain shapes to
`_concurrency` helpers or scope drains, deleting local ledgers/helpers,
shrinking baselines. **Acceptance for every adoption PR:** each scope attaches
to the lifecycle that must wait for it. Session-owned collaborators attach to
the Session root and to their mapped teardown cohort; server/debugger work
attaches to a server root; telephony to call/server roots; standalone objects
own and close a root registered with the runtime supervisor. Only members in
the current pipeline broadcast cohort are tested against that barrier.

- **2.2 providers**: `stt/base.py`, `stt/websocket_base.py:138`,
  `tts/_multi_context_ws.py`, `reconnecting_ws.py:172,418`,
  `_provider_helpers.py` (fold the fire-and-forget emit +
  `_drain_emit_tasks` family — including the forks at
  `transports/_base.py:196-338`, `transports/_webrtc_audio.py:141`,
  `supervisor.py:335-428` — onto one scope-backed emitter; delete the
  `getattr` duck-typed reach-across at `stt/base.py:318-320`).
- **2.3 retained transports (peer-gated)**: after the peer-set decision,
  `twilio_media.py:1566`, `websocket.py:462`,
  `webtransport.py:585,:1754,:2027`, `webrtc.py:178,:357,:836,:1197`,
  `_webrtc_audio.py:101,:141`, `_browser_events.py:112`, `local.py:580`,
  the four module-global ledgers.
- **2.4 telephony**: `ivr.py:524`, `outbound.py:557,:584`, `screening.py`,
  `dtmf.py`, `config/_telephony_wiring.py:173,:201`.
- **2.5 server + debugger**: adopt the remaining `server/transports.py`
  paths and only then delete the grandfathered private helper/global ledger;
  `server/voice_server.py:117,:746`,
  `server/webrtc_routes.py:151`, `session_manager.py:217`,
  `_health_check.py:90,:106`, `supervisor.py`, `debugger/server.py:221`,
  `debugger/dev.py:79`, `helpers.py:143`.
- **2.6 retained integrations (peer-gated)**: after the peer-set decision,
  `llama_agents.py:163,:591,:1100-1130` (the
  hand-rolled `ensure_future` + `wait` race), `responses_api.py:199`.

### WS2.7 — `Session.stop()` on the root scope [L]

Three PRs:

- **a. Coverage inventory + ordering tests (Tier A).** Much of the entry
  protocol is
  already tested (`tests/session/test_session_lifecycle_teardown.py`:
  force-preempts-hung-graceful, join-supersede, failed-stop-blocks-restart,
  force-cancels-in-progress-start, …) — inventory against the scenario
  list and write only the gaps. The genuinely missing net, and where the
  rewrite risk actually lives, is **ordering**: add ordering-observation
  tests — instrumented fake providers/transport/journal recording events —
  for each partial-order edge/barrier the tree must reproduce
  (barge-in cleanup drained while providers live; outbound stopped before
  `transport.disconnect()`; pipeline/TTS/outbound all signalled before any
  member of that cohort is awaited; ingress/health/queue/manager/provider
  order in `Session.stop`). Do not assert an incidental total order among
  siblings inside one broadcast cohort. Pass/fail scenarios cannot see these
  edges; barrier assertions can.

WS2.7a inventory result: the checked-in coverage map freezes 27 observable
scenarios against concrete pytest nodes — twelve entry/admission and
supersession cases, five turn-work policies, five ordering contracts, three
resource-ownership rules, and two postmortem guarantees. Existing tests
already covered prompt policy, retry admission, startup cancellation, runtime-
owned reentrancy, barge-in cleanup while providers are live, scoped STT work,
provider/queue ownership, and journal preservation. The two missing concepts
now have observation-based nets: force stop requests cancellation for the
pipeline, TTS, outbound, and scoped members before any member settles; and
both force/graceful modes preserve the reviewed branch plus common-finalizer
partial-order edges through ingress, health, helpers, queue, outbound,
transport, manager, agent, provider siblings, identity, journal, and closed
publication. Provider siblings deliberately have no asserted total order.

- **b. Mapping table (Tier B, after WS2.1).** In the PR description (or a
  short note in this
  file): every step of the complete `Session.stop()` symbol — entry admission,
  start/stop-lock fencing, ownership/supersession, body, failure bookkeeping,
  and final admission cleanup — mapped to a scope/cohort, finalizer position,
  or retained-policy-code line. Do not use a partial line range as the
  inventory boundary. If a step has no
  home, the WS2.1 API is incomplete — stop and extend it first. The supersede
  loop, event-bus poisoning, and failure bookkeeping are expected to remain
  policy code in `stop()`; the target is "teardown choreography lives in the
  tree", not a line count.
- **c. Rewrite (Tier B, after package adoption)** behind (a)'s tests. Graceful
  prompt wait stays unbounded
  (`deadline=None`); any newly bounded wait is **[behavior change]** and
  must be called out. The firm decision (single `stop(force=)` verb,
  postmortem view preserved) is unchanged.

---

## WS3 — Bridge engine

### WS3.1 — portable rows + internal scenario drivers [L] (Tier A)

Keep `AgentBridgeContractSuite` public and source-compatible: add only
universally observable rows expressible through its portable provider
factory. Add an internal `BridgeLifecycleScenarioSuite` with per-SDK
capability drivers for controlled unknown-event injection, in-flight tool
gates, inner-stream close probes, recorder/transient-state probes, and a
normalized history projection. Its checked-in execution matrix marks each row
`required` or `not_applicable` with rationale; retained built-ins may not
silently skip required rows. Scenarios cover:

- malformed/unknown stream-event tolerance (the `4eff9a78`/`a721884d` class)
- cancellation drain: tool in flight at cancel; delivered-text commit rule
- cleanup-on-close: recorder cursors exited, stream `aclose`d, transient
  context purged (the `langgraph.py:684-725` choreography as scenarios)
- interruption history: `apply_interruption` must never rewrite a prior
  turn's message (the critique's #107 PydanticAI class)
- `reset()` / `snapshot_state()` after each of the above

Run shared row logic against an unmarked offline fake on every PR. Real SDK
drivers remain `integration_external`; the Tier-A gate requires either required
extras cells in PR CI or a successful nightly artifact built from the exact
candidate SHA. Any bridge fix is its own PR. This is rows **and capability
wiring**, not an assertion that today's `provider_factory` can inject these
faults.

Deliver WS3.1 as four one-concern PRs:

- **a. Scenario matrix inventory.** Freeze the seven shipped bridges by five
  lifecycle capabilities, plus the universal reset-isolation and JSON-safe
  snapshot postconditions. Classify every bridge/scenario cell as required or
  not applicable, with the exact pre-harness evidence or a named gap.
- **b. Internal suite + offline driver.** Introduce the internal scenario and
  capability-driver protocols, then run all shared row logic against an
  unmarked deterministic model bridge on every PR.
- **c. Portable public rows.** Add only lifecycle behavior observable through
  the existing provider factory to the source-compatible public
  `AgentBridgeContractSuite`.
- **d. Built-in capability drivers.** Wire each applicable built-in cell,
  close every gap in (a), and produce the required-extras or exact-candidate-
  SHA nightly evidence. Bridge defects discovered here remain separate PRs.
  Deliver this as framework-bounded child slices: **d1** Generic Workflow plus
  Remote Responses and the matrix-to-suite binding; **d2** the
  LangChain/LangGraph event-stream family; **d3** OpenAI Agents; **d4**
  PydanticAI; **d5** Llama Agents; and **d6** the zero-pending matrix ratchet
  plus exact-candidate-SHA extras artifact. Each child updates the same
  execution registry, and a production bridge fix interrupts the sequence as
  its own PR.

WS3.1a inventory result: the checked-in 7-by-5 matrix freezes 35 cells. Thirty-
three are required and two are not applicable: generic workflows have no
provider event taxonomy, while Llama interruption metadata does not rewrite
assistant history. Existing focused tests cover twenty required cells; thirteen
capability-level gaps remain for WS3.1d. The matrix records reset isolation and
JSON-safe snapshotting as postconditions of every applicable scenario. This
inventory slice changes no production behavior; later WS3.1 slices must evolve
the same matrix until no required cell remains `missing`.

WS3.1b result: the private `BridgeLifecycleScenarioSuite` now owns the five
framework-neutral assertions and the universal after-scenario postconditions.
Its driver protocol exposes direct events, recorder cursor ids, inner-stream
close counts, transient-item counts, delivered-text history, and normalized
history projections; SDK-specific drivers therefore cannot substitute a bare
pass/fail flag for observable evidence. An unmarked deterministic model bridge
exercises malformed and future events before a valid terminal response, a
gated in-flight tool cancellation that drains only the tool result, consumer
close propagation, balanced recorder cleanup, and an empty current-turn
interruption after seeded prior history. Every row also proves JSON-safe state
before and after `reset()`, with the post-reset normalized state empty. The
internal module reuses the public contract kit's reviewed timeout and is not
re-exported from `easycat.testing`; this slice changes neither public API nor a
built-in bridge.

WS3.1c result: the source-compatible public suite adds two portable lifecycle
rows. A consumer close after the first bridge event must complete within the
existing contract timeout, balance every recorder unit entered during the
turn, and leave a JSON-safe snapshot. Separately, `reset()` after a completed
turn must restore the exact fresh-session stable state, not merely return some
serializable value; a suite subclass may declare isolation-identity fields
that must rotate while every other snapshot field stays exact. The first row
exposed the reference contract fake's straight-line cursor exits; the fake now
owns them in `finally`, so it models the contract on both exhaustion and early
close. The public suite also exposes a no-op settlement hook for frameworks
whose interruption boundary is correctly deferred until async state
persistence. Both credential-free shipped factories satisfy the default rows.
The five optional-SDK classes remain the WS3.1d extras-matrix responsibility;
when the extras are absent they skip explicitly under `integration_external`.
No built-in bridge implementation or public import was changed.

WS3.1d1 result: the execution registry is now a second, progress-bearing layer
of the checked-in matrix. It derives each wired suite's scenario set from the
same required/not-applicable cells, verifies the pytest suite class exists, and
keeps all unwired bridges explicitly `pending`; the discovery-time
`coverage` counts remain preserved as the audit baseline. Generic Workflow's
four applicable rows and Remote Responses' five rows are wired, leaving five
bridge drivers pending. The shared tool observation now splits recorder phases
before and after cancellation, so a deep generic workflow can prove a gated
tool result drained even though its portable stream exposes text rather than
`tool_*` events. History isolation compares the normalized prior-turn
projection, allowing a framework to retain current user input without changing
the prior assistant message. Controlled workflow and SSE innermost streams
prove exact close propagation, balanced cursors, purged transient state,
delivered-text history, unknown-event tolerance for Responses, and reset/
snapshot postconditions. Generic's sole skipped row is the matrix-declared
unknown-event non-applicability; no required row skips.

WS3.1d2 prerequisite fix result: the first LangChain/LangGraph driver probe
found that both bridges observed the cancel token before translating a pending
`on_tool_end`, so their framework stream stopped without delivering the
matching tool result to the runtime's drain policy. The correction is isolated
from driver wiring: the shared event-family module now tracks tool starts seen
before cancellation, records the boundary once, forwards only matching tool
deltas/results while draining, suppresses post-cancel model text, and stops
after the final pending result. LangChain then commits only the delivered text
to its history; LangGraph commits the same partial assistant message before
interruption. A shared regression proves both result phases arrive, innermost
event streams close, and a follow-up interruption rewrites the current partial
turn without touching the prior assistant reply. WS3.1d2 remains pending until
the five-row driver suites land in the next PR.

WS3.1d2 result: a single credential-free, close-aware `astream_events` harness
now drives the LangChain and LangGraph bridges through all five required
scenarios on every PR. It injects a non-mapping value and future event before a
valid model delta, gates a tool result across cancellation, measures exact
innermost-stream close propagation, opens nested framework/model recorder
cursors before consumer close, and cancels an empty current turn after seeding
prior history. The drivers normalize typed and dict-shaped messages, include
LangGraph's pending mutation and transient-context lists in cleanup state, and
model `reset()`'s thread rotation explicitly in the one-state graph double.
Both bridges prove balanced cursors, zero remaining transient work, delivered-
text-only tool cancellation history, prior-assistant isolation, JSON-safe
snapshots, and empty post-reset state. The execution registry advances from two
to four wired bridges; OpenAI Agents, PydanticAI, and Llama Agents remain the
three explicit pending drivers.

WS3.1d3 prerequisite fix result: the OpenAI Agents history-isolation probe
reproduced an empty-current-turn corruption: both interruption rewriting and
post-processing scanned backward past the latest user entry and edited the
prior assistant reply. Both reverse scans now treat that user entry as the
current-turn boundary. Reaching it before an assistant message makes the
rewrite a no-op, preserving both prior history and an active response-id chain;
normal current-turn assistant rewrites retain their existing behavior. WS3.1d3
driver wiring remains pending in the next PR.

WS3.1d3 result: a credential-free controlled `RunResultStreaming` driver now
runs all five required OpenAI Agents scenarios on every PR. Its close-aware SDK
iterator injects an unknown future run item before valid text, gates a function
result across `after_turn` cancellation, verifies hard close calls immediate
run cancellation and closes the delegated iterator, balances the agent cursor
while a tool is pending, and snapshots an empty current turn with its user
boundary before applying interruption. The normalized driver observes only
user/assistant history and counts both live SDK work and pending interruption
metadata as transient state. Every row proves JSON-safe snapshots and empty
post-reset state. The execution registry advances to five wired bridges and
two pending drivers: PydanticAI and Llama Agents.

WS3.1d4 result: a credential-free PydanticAI `agent.iter()` driver now runs all
five required scenarios on every PR. Controlled `ModelRequestNode` and
`CallToolsNode` streams inject an unknown future event before valid text, gate
a function result across cancellation while suppressing later text, expose
exact inner context close state, and snapshot SDK-shaped current-turn messages
on consumer close. The recorder row closes with a tool pending; the history row
cancels after the current user message and exercises PydanticAI's existing
user-boundary guard without touching the prior response. Per-test fake message
modules keep the driver deterministic without the optional package, while the
real bridge's dynamic message imports and full `agent.iter()` choreography are
still exercised. Every row proves balanced cursors, zero live run/stream state,
JSON-safe snapshots, and empty post-reset history. The execution registry
advances to six wired bridges with only Llama Agents pending.

WS3.1d5 result: a credential-free local-workflow driver now runs all four
applicable Llama Agents scenarios on every PR; prior-turn assistant rewriting
remains the matrix-declared non-applicable row. A controlled workflow stream
injects a future custom event before valid text, exposes exact source close
state, and models a blocked workflow step as the tool boundary: it records tool
start before cancellation and the drained result from `cancel_run()` while the
bridge suppresses all later deltas. The recorder cleanup row closes a pending
step whose handler remains nonterminal, proving the bridge balances its
workflow cursor, closes the source, clears active/pending handler fields, and
drops the unsafe retained Context. Every row proves JSON-safe snapshots and
empty post-reset state. All seven bridge drivers are now wired; WS3.1d6 owns
the explicit zero-pending ratchet and exact-candidate-SHA optional-extras proof.

WS3.1d6 result: the execution registry is now closed rather than merely
progress-bearing: every one of the seven shipped bridges must remain `wired`,
and the ratchet rejects any reintroduced `pending` state. The existing nightly
extras matrix now identifies the six real-SDK bridge cells (including both
supported PydanticAI generations), reruns the exact public contract class after
the isolated extra install, and rejects zero tests, skips, failures, or errors.
Each such cell compares `git rev-parse HEAD` with the workflow's `github.sha`
and uploads the JUnit result plus a deterministic JSON attestation whose name
includes that candidate SHA. A manually dispatched run at a PR ref therefore
produces the same auditable exact-candidate evidence required by the Tier-A
gate without making optional SDKs part of the default developer environment.

### WS3.2 — LangChain/LangGraph shared core [L] (Tier C; peer-gated)

After the peer-set decision retains both bridges, extract the near-fork
choreography they duplicate around
`_langchain_events.py`: the `invoke`/`_drive_stream`/`_finalize_done` split
(`langgraph.py:503-524` ≡ `langchain.py:276-290`), `_stream_event_object`,
the turn accumulator, `_handle_cursor_lifecycle`, `_content_of`/
`_set_content`. This step is close to pure win independent of the engine
bet.

### WS3.3+ — Engine generalization, gated [M-L each] (Tier C)

Only for the retained peer set, after the peer-set decision and behind the
**generalization gate**. Before freezing an interface, build disposable
adapter sketches for every retained bridge and record which hooks are truly
family-neutral versus framework-specific. Then:
`BridgeEngine` (`integrations/agents/_engine.py`) grows out of the WS3.2
core — stream driving, cursor lifecycle (`turn_cursor` engine-internal),
the interruption protocol over an adapter state codec, malformed-event
tolerance, cancellation drain (absorb `_agent_runner.py`'s
`_BridgeToolDrain:101-135` and `close_stream_after_done:71-98`), close
semantics. Then bridge-by-bridge: `openai_agents` first (also deletes its
private `_resolve_model_id` only if the shared helper preserves exactly the
old accepted shapes; widening to `.model_name`/`.name` is a separate
**[behavior change]** with its own test), then `responses_api`, `pydantic_ai`,
`llama_agents` (may need a declared concurrency exemption — its SDK
requires concurrent waiting), last `generic_workflow`/`template.py` (the
template must showcase the adapter shape third parties copy).

**The gate:** after the retained-peer sketches, a migration stops when it
requires a framework-specific engine hook or exemption not present in that
pre-registered interface. A peer-neutral refinement discovered by multiple
sketches does not fail the experiment; an escape hatch for one SDK does.
Per-bridge acceptance: all applicable WS3.1 scenarios green; adapter has no
`asyncio` import unless the sketch recorded a named SDK requirement;
duplicated choreography is deleted, not deprecated.

---

## WS4 — Transport lifecycle engine (Tier C; WS4.1 is a Tier-A safety net)

### WS4.1 — portable rows + built-in lifecycle harness [L]

Keep the public `TransportContractSuite` source-compatible and limit additions
to universally observable rows. Add an internal
`TransportLifecycleScenarioSuite` with a model fake and capability drivers for
every retained built-in transport. Drivers inject connect leadership races,
disconnect-during-connect, startup rollback, mid-stream teardown, late frames,
queue overflow, degraded emission, and interrupted-disconnect publication.
The execution matrix is checked in; applicable rows cannot silently skip.
Seed it from duplicated edge cases, including each semantic clause of Twilio's
disconnect predicate. The current provider fake alone is not proof that a
built-in passes.

Deliver WS4.1 as reviewable, progress-bearing child slices:

- **a. Scenario matrix inventory.** Freeze the five shipped transport families
  by the eight lifecycle scenarios, record exact pre-harness evidence, and
  leave every built-in capability driver explicitly pending.
- **b. Internal suite + model driver.** Introduce the private scenario/driver
  protocols and run every row against an unmarked deterministic transport
  model on each PR.
- **c. Portable public rows.** Add only lifecycle behavior observable through
  the source-compatible public `TransportContractSuite` factory.
- **d. Built-in capability drivers.** Wire framework-bounded children: **d1**
  Local plus WebSocket, **d2** Twilio, **d3** WebRTC, **d4** WebTransport, and
  **d5** the zero-pending ratchet plus any exact-candidate optional-backend
  evidence. Every child updates the same execution registry; production fixes
  discovered by a driver interrupt the sequence as separate PRs.

WS4.1a inventory result: the checked-in 5-by-8 matrix freezes forty required
cells across Local, WebSocket, Twilio, WebRTC, and WebTransport. Thirty-six
cells have exact pre-harness tests and four remain named gaps: Local connect
leadership, Local disconnect-during-connect, Local interrupted-disconnect
publication, and Twilio queue overflow. All five capability drivers remain
explicitly pending. This inventory changes no transport behavior; later WS4.1
slices must evolve the same registry until every required row is wired and no
pending driver remains.

WS4.1b result: the private `TransportLifecycleScenarioSuite` now owns all
eight framework-neutral assertions and JSON-safe, quiescent postconditions.
Its capability-driver protocol exposes backend start/close counts, caller
generation results, lifecycle publications, retained cleanup ownership,
delivered frames, queue acceptance/drop observations, normalized degraded
events, receiver termination, and rollback resource state rather than opaque
pass/fail flags. An unmarked deterministic transport model runs every row on
each PR with gated startup and cleanup, shared connect/disconnect task
ownership, a one-frame bounded queue, exact generation fencing, and a live
receive iterator. The internal module is not re-exported from
`easycat.testing`; this slice changes neither public API nor a built-in
transport.

### WS4.2 — compositional lifecycle controller [L, split a-d] (peer-gated)

After the peer ADR, transport B60, WS4.1, WS1.4, the relevant WS2.3 slices,
and WS5.1, introduce a transport-neutral component consumed by retained peers.
Do **not** grow `ServerTransportBase`: it is specifically a
`websockets.serve` host, while WebRTC uses aiohttp, WebTransport has distinct
server/connection objects, and local transport has no listening socket.

- **4.2a FSM core:** `TransportLifecycleController` owns state transitions,
  leader/follower bookkeeping, and reentrancy. A policy supplied by each
  adapter preserves its current lock-block versus task-join behavior.
- **4.2b task/event ownership:** receive-task `OwnedTask` reaping and
  interrupted-disconnect publication, without rollback/error changes.
- **4.2c rollback/finalization:** startup rollback and cleanup aggregation,
  parameterized to preserve each transport's exception type and precedence.
- **4.2d epoch integration:** connect the already-migrated connection Epoch to
  the controller and delete duplicate state fencing. Queue and bind policy are
  outside this controller.

No extraction PR chooses new cross-transport semantics. Converging lock-vs-
join behavior or adopting `ExceptionGroup` is a separate product decision and
**[behavior change]** PR after migration parity, with public compatibility
tests.

### WS4.3+ — Per-transport migration [M-L each]

Retained transports only. Order by delta:
`websocket.py`, `twilio_media.py`, `webrtc.py`; include `webtransport.py`
and `local.py` only if the decision retains them. Per-transport
acceptance: WS4.1 suite green; transport keeps only
codec/socket/negotiation specifics; every applicable internal built-in scenario
runs without skip; duplicated lifecycle code is deleted.

---

## WS5 — Boundary primitives

- **WS5.1 authorized bind capabilities [M-L] (Tier B; peer-gated)**: inventory
  every retained listener backend (`websockets.serve`, aiohttp `TCPSite`,
  aioquic/WebTransport, direct socket bind, and intentional test/embedding
  exceptions). Add typed backend wrappers or
  `authorized_bind(policy, binder)` so every path applies `enforce_bind_guard`
  while preserving each backend's current exception behavior. Ruff bans only
  statically qualified zero-baseline APIs; the source AST ratchet covers
  dynamic `sock.bind`/binder aliases. Migrate one backend per PR.
- **WS5.2 extend the secret-repr inventory [M] (Tier A) [behavior fix]**:
  current main already has `tests/config/test_secret_reprs.py`, including
  provider-catalog discovery, a curated config list, and nested named-provider
  parameters. Extend it rather than creating a second test. The confirmed
  active leak is `WebTransportTransportConfig(auth_token="sentinel")`, whose
  repr currently exposes the token; set that field `repr=False` immediately,
  independent of the peer decision. Cover all public config dataclasses with
  a source-level dataclass/secret-field inventory plus runtime sentinel
  constructors from provider catalogs and one authoritative public-config
  registry. A drift guard compares that registry with public exports. Do not
  use the internal transport union as a proxy for all configs or recursively
  import optional-SDK packages.
- **WS5.3 TTS residue [M] (Tier B)**: fold `_get_mgr`, `_route_key`,
  `_on_global_frame`, `_reset_persistent_audio_alignment`,
  `_discard_persistent_audio_state`, `_decode_message`
  (`elevenlabs_tts.py:500-541` vs `cartesia_tts.py:173-262`) and
  `_replay_request` (`deepgram_tts.py:197`, `elevenlabs_tts.py:660`,
  `cartesia_tts.py:263`) into `_multi_context_ws.py`.

---

## WS6 — Meta-layer and measurement

- **WS6.1a pre-registration [M, before Tier-A implementation]**: check in
  `plan/metrics/refactor-families.json`, `adjudications.json`,
  `incidents.json`, and the measurement contract in
  `plan/metrics/README.md` before post-treatment data exists. They freeze the
  family/control manifests, per-cohort window/anchor shape, severity rubric,
  attribution/adjudication workflow, formulas, thresholds, and invalidation
  rules. The undecided bridge/transport cohorts freeze their candidate set and
  selection rule now; the peer ADR must lock the exact retained subset before
  the first production treatment commit.
- **WS6.1b report engine [M-L] (Tier A)**: implement the script, JSON/Markdown
  schemas, and fixture tests for exact windows, migration exclusion,
  family/control assignment, insufficient exposure/zero denominators, control
  invalidation, candidate clustering, and stable output. This is measurement
  infrastructure only; no outcome can be claimed before a window closes.
- Remaining work is explicitly out of this implementation plan and lives in
  [critique T5](../critique/2026-07-26-full-critique.md#t5-—-the-meta-layer-became-a-second-product-competing-for-the-same-maintenance-budget).
  Standing rules here: new guards assert values, never prose; a new generated
  output has one generator + drift check; add the mkdocs-nav guard in WS0.3.

---

## Verification matrix

Every PR runs its row plus `just check`; a row may be narrowed only when the PR
description explains why the omitted targets cannot observe the change.

| slices | required focused targets | external/nightly evidence |
|---|---|---|
| WS0 | `uv run pytest tests/test_concurrency.py tests/ratchets tests/test_complexity_ignores.py -q` | none |
| WS1 | `uv run pytest tests/turns/test_turn_manager.py tests/session/test_session_streaming_behavior.py tests/session/test_session_lifecycle_teardown.py -q` | none |
| WS2 | `uv run pytest tests/runtime/test_scope.py tests/session/test_session_lifecycle_teardown.py tests/integration/test_session_lifecycle_e2e.py -q` | none |
| WS3 | `uv run pytest tests/contracts/test_agent_bridge_contracts.py tests/integrations/agents/test_bridge_lifecycle_scenarios.py -q` | required extras cells, or successful nightly artifact at the exact candidate SHA |
| WS4 | `uv run pytest tests/contracts/test_transport_contracts.py tests/transports/test_transport_conformance.py tests/transports/test_lifecycle_scenarios.py -q` | retained-backend cells when an offline SDK/backend is optional |
| WS5 | `uv run pytest tests/config/test_secret_reprs.py tests/server/test_auth.py tests/transports/test_webrtc_auth_browser_playground.py -q` | retained aioquic cell for WebTransport bind migration |
| WS6 | `uv run pytest tests/test_refactor_metrics.py tests/ratchets -q` | none |

New paths in this table are deliverables of their owning slice. The SDK-gated
real bridge suite is not silently treated as part of local `just check`.

---

## Re-measurement and release gates

Before Tier-A implementation, each cohort manifest freezes: treated members,
recurring bug classes, controls, minimum exposure (touching commits and churn),
the non-inferiority tolerance `epsilon`, attribution reviewer, and exclusions.
On completion, that cohort records its own immutable merge SHA/date `D` and
migration-commit list. Windows are exactly `[D-60d, D)` and `[D, D+60d)` by
committer timestamp. A later treated production change resets `D`; a control
receiving the treatment is invalidated and yields `insufficient_data`, never a
post-hoc replacement.

`scripts/refactor_metrics.py` emits checked-in JSON + Markdown reports and:

- finds candidate **multi-member recurrence**: the same declared bug class
  touching at least two manifest members within seven days. For peer families,
  members are peers; for Tier-A Session work they are lifecycle/staleness
  modules, so the metric does not pretend Session is a peer family;
- persists human adjudication (`same_fix`, `not_same_fix`, evidence, rationale,
  reviewer) because subjects cannot decide logical sameness;
- counts each non-migration commit once and defines fix density as adjudicated
  fix commits / all commits touching the cohort; per-KLOC churn is a sensitivity
  view; and
- computes `delta = post_density - pre_density` for treated and pooled controls.

A cohort result is decidable: a zero denominator, invalid control, or exposure
below its pre-registered minimum is `insufficient_data` and does not pass. It
passes only with zero adjudicated multi-member recurrences, adequate exposure,
and `treated_delta <= control_delta + epsilon`; when `pre_density > 0`, it also
requires `post_density < pre_density`. A healthy zero-fix pre-window instead
requires zero post fixes and non-inferiority, not impossible strict decrease.

The soak incident source is `plan/metrics/incidents.json`, populated from
linked issues, regression PRs, reverts, and release-blocking CI failures. The
rubric is frozen in WS6.1a: P1 is security/cross-session corruption, data loss,
or service-wide unavailability; P2 is a supported lifecycle path that hangs,
leaks owned work, misroutes state, or requires a hotfix/rollback. The manifest's
named reviewer records attribution evidence; an unresolved attribution dispute
does not pass the soak.

The gates are deliberately separate:

1. **Tier-A structural exit (immediate):** all Tier-A contracts, behavior-
   parity tests, source ratchets, and inventories are green. This permits one
   peer-neutral WS2.1 vertical slice, and nothing else from Tier B.
2. **Vertical-slice soak (14 days from WS2.1a's merge SHA/date):** no attributable
   P1/P2 incident under the rubric above. This permits WS2.1b-c; WS1.2f-g also
   require that foundation and the per-turn membership inventory.
3. **Tier-A 60-day outcome (A60):** apply the formula above to the pre-registered
   Session lifecycle/staleness cohort. Any fail or `insufficient_data` stops
   bulk Tier B and triggers reassessment.
4. **Tier-B 60-day outcomes (B60):** apply the same rule independently to each
   migrated family using its own `D`. Bridge B60 gates WS3; transport B60 gates
   WS4. There is no single anchor reused across families.

After each eligible window, regenerate the persisted report and run:

```bash
uv run python scripts/refactor_metrics.py --manifest plan/metrics/refactor-families.json
uv run pytest tests/ratchets -q
uv run ruff check .
```

Raw rolling-90-day fix-commit share may remain dashboard context, but it is
not a gate and cannot substitute for the pinned-window report. The 60-day
observation periods are part of the calendar, not parallel work assumed away
by the engineering-day estimate.

## Standing risks

- **The migration is itself churn.** Mitigations baked in: tests-first for
  every extraction (WS2.7a, WS3.1, WS4.1), one peer per PR,
  delete-don't-deprecate only within a PR that proves parity, the
  >10-source-file split rule, and honest labeling — every known
  **[behavior change]** is enumerated above rather than discovered in
  review.
- **Dual mechanisms during transition** (generation+lease, old spawns +
  scopes) are windows for divergence. WS1.2's derivational dual-write,
  complete writer inventory, and debug assertions bound a window that crosses
  the Tier-A/Tier-B gate deliberately; the old generation carrier is not
  removed until epoch-to-scope binding lands. The enforcement freeze prevents
  new code from adopting the old form meanwhile.
- **Firm-decision compliance**: WS2 and WS3 touch surfaces governed by the
  firm decisions (single `stop()` verb, cooperative token — not exceptions
  — for turn/TTS cancellation, delivered-text history,
  cancellation-resistant task ownership). The §4.2 policy/cohort engine is
  designed to preserve these; each such PR cites the decision it preserves
  and runs the named contract tests from `docs/architecture.md`.
