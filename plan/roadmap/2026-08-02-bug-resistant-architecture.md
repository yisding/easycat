# A bug-resistant architecture for EasyCat

Status: design reference.
Date: 2026-08-02 (adversarially reviewed in two passes; the post-PR-795 pass
covered concurrency/scope/transport, epoch/session/bridge contracts, and
execution/measurement/security, with accepted findings incorporated).
Scope: structural changes that eliminate recurring implementation-bug classes
and reduce churn. Companion to
[the full critique](../critique/2026-07-26-full-critique.md) (what is wrong
today), the firm decisions in
[docs/architecture.md](../../docs/architecture.md) (what the semantics must
be), and [the refactor plan](2026-08-02-bug-resistant-refactor-plan.md)
(the PR-level work). This document is about *mechanism*: how to restructure
the code so the bug classes that dominate the fix history stop being
writable, and so one logical fix stops costing N commits.

Measurement note: evidence is pinned to `4f09c70b` over the fixed committer-
date window `[2026-02-02, 2026-08-03)` unless a narrower method is stated.
Treat counts as magnitude evidence, not live inventories. Ratchet baselines
(§7) are re-measured at freeze time.

## 1. Thesis

Over that six-month window the repo absorbed 4,016 commits; 831 subjects
(~20.7%) match `fix|bug|regress|guard|prevent` case-insensitively. The fixes
are not random: they cluster into a
small number of classes — cancellation/teardown lifecycle races, stale-turn
and stale-connection fencing, and the same fix re-applied across peer
implementations. Per-line quality is high (the critique said the same); the
problem is that the invariants behind these classes are maintained by
*discipline at hundreds of sites* instead of by *construction at one*.

The proposal, in one sentence: for each recurring bug class, push its
invariant up one or more enforcement levels — from convention, to a primitive
that makes the wrong program unrepresentable, to a shared engine verified by
one contract suite, with a CI ratchet that inventories the remaining
hand-rolled sites and only permits the count to shrink.

## 2. Evidence: what actually breaks, and why fixes don't stick

### 2.1 Class A — lifecycle and cancellation races (dominant)

The commit log signature: "harden slice {five..twelve} lifecycle boundaries"
(eight commits, one audit finding, walked file-by-file), "drain cancelled
prompt tools safely", "avoid self-canceling WebRTC delivery cleanup",
"preserve teardown cancellation", "suppress cancelled resampler tails",
"reclaim late manager turn cancellation". Fix-flavored commit density per
file: `session/_session.py` 56, `telephony/call_state.py` 28,
`session/_turn_runner.py` 26, `transports/twilio_media.py` 25,
`transports/webrtc.py` 22, `reconnecting_ws.py` 21. (`config.py` 44 and
`cli/_app.py` 37 rank between these; their churn is config-surface and
docs-registry work, addressed by the critique's T5/T7 rather than by this
document.)

Why the class recurs — the current concurrency substrate, measured:

- **Two disjoint cancellation systems.** `CancelToken` (`cancel.py:10`) is a
  cooperative latch with no relationship to `asyncio.Task` cancellation:
  cancelling a token cancels no task; cancelling a task sets no token. Every
  seam bridges them by hand, and every bridge must answer "whose cancellation
  is this — the child's I cancelled, or mine from my caller?" The codebase
  holds **61 `Task.cancelling()` baseline comparisons across 20 files** and
  ~80 `current_task()` calls (mostly self-identity guards) to answer it,
  plus a 4-line "checkpoint a pending cancellation before sampling the
  baseline" preamble copy-pasted in `runtime/scope.py`, Session/turn cleanup,
  and `server/transports.py`.
- **Nine task-spawn idioms** across ~90 raw spawn sites:
  `RuntimeScope.create_task` (session only), `create_journaled_task`,
  `BackgroundTaskScope`, single named slots
  (`self._receive_task = create_task(...)`), twelve independent
  instance-level `set[asyncio.Task]` ledgers, four module-global ledgers,
  local create-and-await, `loop.create_task` from sync callbacks, and
  `asyncio.ensure_future` (15 sites). `asyncio.TaskGroup` appears zero
  times. The set of live tasks is not enumerable at any point in the
  program.
- **Seven distinct drain-on-stop shapes**, including thirteen independent
  copies of the drain-loop "uninterruptible cleanup" idiom (eleven
  shield-based, two event-wait variants — `stt/base.py:574`,
  `runtime/journal.py:196`, `transports/webrtc.py:362`,
  `session/_journal_sink.py:355`, `stages/base.py:388`,
  `debugger/server.py:221`, …).
- **Six distinct `CancelledError`-suppression shapes** — `contextlib.suppress`,
  bare `except: pass`, `except (CancelledError, Exception)`,
  `gather(return_exceptions=True)`-as-suppressor (31 sites), `cancelling()`
  compare-and-reraise, and the sleep(0) checkpoint. An audit of cancellation
  handling cannot even be done by grep.
- **Hand-implemented teardown protocols.** `Session.stop()`
  (`session/_session.py`, currently ~290 lines) implements ownership
  transfer between racing stop callers, admission fencing against racing
  `start()`, per-task self-identity guards, force-vs-graceful ordering, and
  failure bookkeeping — all inline. `transports/twilio_media.py` disconnect
  opens with an **11-clause idempotency predicate** (`:1665-1677`); every
  clause is a separately discovered leak.
  `_turn_runner.cancel_preemptive_generation` (`:574`) spends 35 lines to
  cancel one task correctly.
- **Teardown timeouts are declared locally in ~12 places** with no shared
  budget (`_BARGE_IN_CUTOFF_TIMEOUT_S`, `_POST_CANCEL_AWAIT_TIMEOUT`,
  bare `0.5` literals, …), so nobody can state the worst-case stop latency.

### 2.2 Class B — staleness/fencing bugs

"Fence stale IVR decisions" (#778), "fence active call helpers" (#791),
"skip pre-cancelled bridge turns", "preserve successor during TTS
cancellation". The question "does this event/callback belong to a live
turn/call/connection?" is answered by **at least 12 mutually incompatible
mechanisms**: `TurnContext.generation` counters, object-identity compares
against `Session._turn`, a `no_turn` sentinel (11 occurrences in
`_stt_committer.py` alone), `CancelToken.is_cancelled` as a liveness proxy,
a 4-way compound predicate (`_cancel_cleanup_owns_turn`,
`session/_session.py:1865` — whose docstring is a bug report), a
`_preemptive_finalized_generation` watermark, `TurnManager._pause_generation`
threaded through callbacks as a parameter, per-transport
`_connection_generation` / `_peer_generation` pairs, and telephony
`_activation_epoch` / `_lifecycle_epoch` / `_placement_epoch` /
`CallState._generation`. The staleness predicates in `session/` and
`transports/` do not share a signature.

The cost of divergence is concrete: the same "flush the resampler tail on
teardown?" decision is fenced by peer generation in `webrtc.py:1279`, by
websocket identity in `twilio_media.py:897`, and **not fenced at all** in
`websocket.py:198` — indistinguishable from correct until the race fires.
(That missing fence is a live bug; the refactor plan lands it as an
immediate WS0 fix rather than holding it hostage to the epoch migration.)

### 2.3 Class C — one logical fix, landed N times

Token-normalized clone detection shows literal copy-paste is modest (5-12%).
The churn driver is **duplicated choreography**: the same ordered protocol
re-implemented per peer file with framework-specific bodies. Measured by
same-named methods present in 3+ peer files:

| family | peer LOC | choreography LOC |
|---|---|---|
| transports (5) | 6,237 | ~1,335 |
| agent bridges (7) | 8,035 | ~2,256 |
| stt (5) | 2,634 | 669 |
| tts (4) | 1,983 | 562 |

Documented N-times sequences (all verified in the log):

- The reconnect invariant ("state must not cross a transparent reconnect")
  landed as **six commits, one per provider** (2026-07-31 → 08-01:
  `acd6aa99`, `e5458885`, `e0ba9d9b`, `e26ddcd3`, then re-fixes `f1238952`,
  `d9700d19`).
- "agents: ignore malformed stream fields" (`4eff9a78`) patched
  `langchain.py` + `langgraph.py` in parallel — one day after `a721884d`
  fixed the same class in the same two files.
- `11cdd9d5` (a 46-file review-feedback commit) applied the literally
  identical edit to `cartesia_tts.py`, `deepgram_tts.py`,
  `elevenlabs_tts.py` (+4/-7 each).
- Four commits on `twilio_media.py` for one "validate a positive finite
  duration" concern, while `_base.py:43` and `reconnecting_ws.py:80` already
  had the validators.
- The "harden slice N lifecycle boundaries" series: eight commits for one
  finding, because there was no single place to fix it.

Concrete duplicated-choreography exhibits: the single-flight
connect/disconnect wrapper exists in **eight structurally similar copies
across four transports, in two dialects** (lock-based in
`webtransport.py:1419` / `webrtc.py:275` / twilio's *disconnect*
`:1639-1658`; task-shield in `websocket.py:398` / twilio's *connect*
`:1497-1526` — twilio uses a different dialect per direction);
`_publish_interrupted_disconnect` in four copies;
`_reap_receive_task_for_disconnect` is byte-identical (minus a log string)
between `websocket.py` and `twilio_media.py`; the "cancel a child without
consuming my own cancellation" idiom has **at least 16 hand-rolled sites**;
LangGraph↔LangChain are near-forks (same 15-line `invoke` body with the same
6-line comment); `_plan_interruption`/`_apply_planned_mutation`/
`_serialize_framework_state` are implemented once per bridge, seven times
each (~398 lines).

**The in-repo counter-example shows the cure can work here.** STT is
consolidated on `WebSocketSTTBase` (356 lines): 4 of 5 providers implement
only `_on_start/_on_audio/_on_end/_handle_json_message`
(`cartesia_provider.py`'s provider class is ~160 lines), structural
duplication across the family measures **18 lines**, and no provider
hand-rolls a socket. STT's fix-density is correspondingly low. A caveat the
adversarial review rightly insisted on: STT abstracts a *homogeneous* wire
protocol, while the agent bridges wrap *heterogeneous* execution models —
so the precedent transfers with confidence to transports and TTS, and only
provisionally to bridges (§6.1 carries an explicit generalization gate for
that reason).

### 2.4 Root cause 4 — shared abstractions are opt-in, so entropy wins

The repo repeatedly extracts the right helper and then leaves adoption
optional; the next fix lands in the non-adopters:

- `AgentRecorder.turn_cursor` was added explicitly to "centralize per-turn
  cursor cleanup"; 3 of 7 bridges adopted it. `bb4112b7` then fixed lifecycle
  boundaries in exactly the three non-adopters.
- `apply_standard_interruption` — 4 of 7 bridges; the other three re-wrap the
  lower-level protocol themselves.
- `runtime/scope.py:16` `_checkpoint_pending_cancellation` is the canonical
  helper for the cancellation idiom; it is private and imported by nobody.
  `server/transports.py:594` **cites it in a comment while re-implementing it**
  ten lines later.
- `ServerTransportBase` owns connect/disconnect/teardown; 2 of 5 transports
  inherit it (and even in those two, the connection-level classes holding
  the lifecycle choreography sit outside the base). The other three take
  `AudioQueueMixin` only and re-derive teardown.
- The hard-timeout + survivor-ledger idea exists as two near-duplicate
  private copies plus a third inline variant, with three separate
  module-global ledgers.
- `RuntimeScope` itself: heavily used in `session/`, `BackgroundTaskScope`
  adopted by two telephony modules — and nothing else. The packages with
  the highest raw-spawn density (`transports/`, `stt/`, `tts/`,
  `integrations/`, `server/`, `debugger/`) use neither.

### 2.5 Churn that isn't bugs

Of commits touching `src/easycat` in the last four months, **19% also had to
touch `docs/` or the docs/teaching/examples guard tests**, and a further 305
commits touched only that layer. The critique's T5 covers the remedy
(value-asserting guards, one generator per output, fewer prose snapshots);
this document treats it as a secondary mechanism (§8) rather than
re-litigating it. The same multiplier logic applies to peer breadth: every
in-tree peer implementation multiplies Class C, so the critique's T1
demotions are also bug-prevention measures.

## 3. The design rule: three enforcement levels

For an invariant that has produced repeated fixes, "we'll be careful" is
level zero and has empirically failed. Each mechanism below pushes an
invariant to one of:

- **L1 — primitive.** The wrong program is unrepresentable or fails
  immediately: you cannot spawn an unowned task, you cannot compose your own
  staleness predicate, you cannot open a socket without the bind guard.
- **L2 — engine + contract suite.** The choreography exists once; peers are
  translation-only adapters; one scenario suite runs against every adapter,
  so a fix is one commit and its regression test covers all peers.
- **L3 — ratchet.** Remaining hand-rolled sites are inventoried in a
  checked-in baseline; CI fails if a new site appears; the baseline may only
  shrink. (Contrast with the current `pyproject.toml` grandfather list,
  which is static — a permanent waiver list is a gate that has been turned
  off.)

Review rule to adopt alongside: a fix for a known recurring class may not
land as a leaf patch alone. Either it lands in the primitive/engine, or the
PR adds the ratchet entry that marks the site as still hand-rolled.

## 4. Mechanism 1 — one task model (Class A)

### 4.1 A scope tree instead of nine idioms

`RuntimeScope` (`runtime/scope.py`) is the right seed: named tasks,
`cancel_and_drain()`, self-task detach, `cancelling()`-aware drain, journal
records. The concurrency review established that "reach everywhere" is
necessary but not sufficient — the scope model needs the following extensions
before it can express what `stop()` encodes today:

- **Every task is owned by a scope; every scope is owned by a lifecycle**
  (Session, transport connection, provider stream, server, call). Scopes
  form a tree; a child scope registers with its parent. Session-owned work
  attaches to the Session root, server/debugger work to a server root,
  telephony work to call/server roots, and standalone objects expose and close
  a root registered with the runtime supervisor. **Task-to-scope
  assignment is a design decision, not an implementation detail**: the
  assignment rule is "a task lives in the scope whose teardown must wait for
  it" (e.g. the outbound audio pump belongs to the transport-connection
  scope precisely because it must stop before `transport.disconnect()`).
- **Named phases and broadcast cohorts.** Force teardown is not one global
  barrier. It first cancels/drains text work, prompt work, and preemptive work;
  only then does it synchronously cancel the pipeline/TTS/outbound cohort
  before draining that cohort. STT/runtime/barge-in cleanup and resource
  finalizers follow in defined partial order. The scope API exposes
  `signal_cohort` + `drain_cohort`; it does not broaden a pipeline-local
  guarantee to every provider/server task or invent a total sibling order.
- **Ordered finalizer nodes.** Teardown interleaves non-task steps —
  `stop_ingress`, `_outbound_queue.close()`, `transport.disconnect()`,
  `turn_manager.shutdown()`, provider closes — between task drains. Scopes
  therefore hold not just tasks but **ordered async finalizers**
  (AsyncExitStack-style members), so orderings like "outbound task stops
  before `transport.disconnect()`" are expressed as member order within one
  scope.
- **Orthogonal mode-dependent member policy.** Each mode declares `cohort`,
  `signal_token`, `task_action=finish|cancel`, `grace_deadline`, and
  `hard_deadline`. Graceful prompt policy is `(signal_token=False, finish,
  deadline=None)` because current stop waits before cancelling its turn token;
  force is `(True, cancel, existing drain bound)`. Text, preemptive,
  pipeline, and cancellation-resistant members get separately frozen rows.
  A static action or unconditional token signal silently changes behavior.
- **Admission control.** Scopes gain closed/closing states: spawning into a
  closed scope is rejected deterministically (coroutine closed, error
  journaled), including from `spawn_from_sync` on other threads — otherwise
  the tree recreates the unowned-task class during teardown, exactly when
  it matters most.
- **Journaled by default.** Scope-spawned tasks emit the task-lifecycle
  records `session/` already gets, making transports and providers visible
  in bundles for free.
- **Error propagation is part of the migration contract.**
  `BackgroundTaskScope._on_done` logs-and-drops exceptions; several named
  slots it would replace currently *raise* aggregated errors from
  `disconnect()`. Migrating those sites needs a retained-terminal-result
  mode (or RuntimeScope children), or the refactor silently converts raised
  cleanup errors into log lines.
- **Hierarchy-wide bounded survivor ownership.** Child scopes share a root
  registry, and roots charge the single `RuntimeSupervisor` for their event-
  loop/application runtime. Standalone objects obtain the same supervisor, not
  a private quota. It strongly anchors survivors after owner drop and holds only
  tasks plus string owner IDs, never Session references. This makes the bound
  runtime-wide rather than N scopes × K survivors.

With those in place, `Session.stop()` shrinks to admission/ownership policy
plus a phase-split close of the root scope. It does not shrink to a slogan:
the supersede protocol, event-bus poisoning, and failure bookkeeping remain
real policy code that stays in `stop()` (the refactor plan requires a
line-by-line mapping of the current body to tree structure before the
rewrite is attempted).

### 4.2 Bridge the two cancellation systems through explicit policy

Keep `CancelToken` (the firm decisions require cooperative cancellation, and
CLAUDE.md is explicit: token, *not* exceptions, for turn/TTS cancellation).
The naive unification — one call that sets the token *and* task-cancels —
is wrong by construction: the codebase deliberately separates the two in
time (barge-in cancels the token, then *drains* TTS/prompt cleanup tasks
that must run un-cancelled; `reset(preserve_token=True)` tears down manager
state while keeping the token live for an in-flight stream).

The correct bridge is a policy interpreter implemented once in the scope
layer. For each named phase it selects the mode row, optionally calls
`token.cancel()`, applies the declared task action and grace deadline, and at
the hard deadline parks the already-owned task (§4.3). There is deliberately
no unconditional step 1: graceful application-prompt policy does not signal
the token before finishing.

Directionality is explicit: cancelling a token never implicitly cancels a
scope (many sites cancel tokens with no task-cancel intent); closing a scope
interprets the phase plan. Cooperative-only members ("cancellation-resistant
tasks remain owned until they finish" — a firm decision) are exempt from
step 3 and bounded only by their resource boundary, exactly as today.

Graceful-to-force supersession re-evaluates every remaining member under its
force row at the current named phase; tests cover supersession at each barrier.

### 4.3 One public helper per idiom, then close the old path

Create `easycat/_concurrency.py` (or grow `runtime/scope.py`) with the
helpers that currently exist as 3-16 copies each. The adversarial review
sharpened their specs; the load-bearing details:

| helper | replaces | spec notes |
|---|---|---|
| `checkpoint_pending_cancellation()` | 4 inline copies | promotion of the existing correct helper |
| `await start_owned(factory, registry, owner_id, task_name)` | raw parkable-task construction | async so cancellation can be checkpointed before factory/resource acquisition and after task creation; reserves root + supervisor capacity before invoking the factory; returns `OwnedTask`; bare coroutine input is rejected |
| `reap(owned)` | ≥16 hand-rolled sites | returns the child's exception rather than choosing caller policy; on caller cancellation it parks a pending child or releases a settled one before re-raising |
| `shielded_cleanup(factory)` | 13 drain-loop copies | returns `CleanupSettlement` carrying cleanup result/error and caller-cancellation requests; caller selects precedence explicitly |
| `hard_timeout(owned, absolute_deadline)` | 2 copies + 1 variant, 3 module-global ledgers | parks through existing ownership; supervisor-aware `LifecycleLock` ownership makes the release-before-park rule enforceable |
| `swallow_cancel()` | 6 shapes, ~117 sites | **must be `async with`** — the `cancelling()` baseline requires an awaited checkpoint in `__aenter__`; a sync context manager provably cannot distinguish a pre-entry pending cancel from a swallowable child cancel |

Two invariants the whole baseline-comparison scheme silently depends on,
now stated and enforced: **no `Task.uncancel()` outside `_concurrency.py`**
(a single stray uncancel deflates every captured baseline; note
`asyncio.timeout()` calls it internally, so the helpers must be specified
and tested as composable with `asyncio.timeout`), and the sleep(0)
checkpoint before any baseline capture.

The state machine is explicit:
`reserved -> active -> released` or
`reserved -> active -> parked -> released`; failure before factory invocation
releases `reserved`, and duplicate parking is idempotent. Child scopes share a
root registry; every root charges a bounded `RuntimeSupervisor`, which is the
strong task anchor after lifecycle-owner drop and stores only string metadata.
The leaf types accept an injected journal callback rather than importing the
journal layer. No transition evicts or drops live work.
A never-finishing survivor leaves its owner in observable terminal state
`closed_with_survivors`, with journal/postmortem metadata and retry escalation
available; root close may not report a clean drain in that state.

The helper API is not frozen in an abstraction-only PR. Before the spawn
ratchet lands, one complete `server/transports.py` caller migrates end to end.
The other callers and legacy global stay grandfathered until their WS2.5 slice;
the proving PR does not claim partial ledger retirement. It proves owner-drop
anchoring, both quota levels, exception policy, cancellation, and journaling.
The selected proving caller is `WebSocketSessionRuntime`'s
`server.wait_closed` stage: it receives an application-runtime supervisor,
owns a named root registry, converts the shared force deadline once, and
retries an existing parked listener task rather than starting a concurrent
cleanup. Its manager/connection/handler paths deliberately remain on the
grandfathered helper and ledger in this slice.

Plus a teardown-budget manifest that classifies every timeout found inside
lifecycle-symbol closures. Lifecycle defaults centralize in
`teardown_budgets.py`; protocol-local bounds remain local; configurable values
retain configuration. An AST source/manifest bijection is the no-growth guard.

### 4.4 Enforcement

- L3 ratchets (§7): raw `asyncio.create_task` / `ensure_future` /
  `loop.create_task` outside `_concurrency.py` / the scope module;
  `except asyncio.CancelledError`
  outside `_concurrency.py`; `Task.cancelling()` and `Task.uncancel()`
  outside `_concurrency.py`; `gather(return_exceptions=True)` used as a
  cancellation suppressor; module-global task sets; inline shield-loops.
- The two `server/` fire-and-forget clusters flagged in an earlier draft
  turned out to be tracked (gathered and awaited under a hard timeout); the
  real residue is that their gather results are silently discarded. Inspect
  exception results by task identity, treat cancellation caused by the
  preceding explicit teardown as expected, and journal only unexpected
  failures; benign return values are not failures.

## 5. Mechanism 2 — one staleness primitive (Class B)

Introduce a single `Epoch`/`Lease` pair (package-root leaf, like
`_turn_context.py`):

- An **`Epoch`** is owned by exactly one writer per domain: the turn, a
  transport connection, a telephony call, a provider stream. Bumping the
  epoch invalidates all outstanding leases.
- A **`Lease`** is captured at operation start and carries both the check
  (`is_current()`, `guard(on_stale='skip'|'raise')`) **and the payload**
  (`lease.value` — the turn/connection object captured in the same
  operation). Without the payload accessor, callers re-read the live
  pointer next to the lease check and recreate the torn read the primitive
  exists to kill.
- Liveness fences collapse onto the primitive, but not onto one Epoch instance.
  Turn identity and manager activity are distinct: gated replay ends activity
  through `reset(preserve_token=True)` while deliberately retaining the
  installed turn/token/stream. STT audio-accounting watermarks and one-way
  phase latches such as `preemptive_take_closed` are not identity fences and
  remain explicit state.

**Write discipline — the part that actually kills the bug class.** The
concurrency review demonstrated that a lease alone merely relocates
`_cancel_cleanup_owns_turn`'s torn read; the primitive works only with
these rules, which are part of the design, not implementation detail:

- **Inventory every writer before choosing the linearization point.** Turn
  publication is not confined to one private manager method: the inventory
  includes `TurnManager.begin_application_turn`, `TurnManager.reset`,
  `bot_stopped_speaking()` entering IDLE, `Session.begin_turn`, the
  `TurnStarted` subscriber install, and every producer. Text turns are
  observational and classified separately. An AST or
  registration guard prevents a new writer from bypassing the canonical
  synchronous `publish_turn` / `clear_turn` seam.
- **Run lifecycle privately, then publish observation.** Internal producers
  create a private `TurnPublication` and await a private lifecycle callback
  that preserves current handoff/STT-start/install order, then emit a marked
  public `TurnStarted` without an internal lease. A reserved, order-guarded
  internal pre-handler no-ops marked observations and routes unmarked hand-
  built events through the same callback before user handlers. Text events are
  observation-only. Removing hand-built command compatibility would be an
  explicit behavior change.
- **Use two owners and exact clears.** Replacing/clearing `Session._turn` bumps
  identity. Manager reset/IDLE bumps activity, but does not stale identity when
  replay retains the Session turn. Callers take the lease(s) matching the
  semantic question rather than one catch-all counter.
- **Guard placement across awaits.** `guard()` checks at entry; an epoch
  can bump during any await inside the block. The primitive therefore does
  not promise atomicity across awaits — it promises a standard spelling
  plus one exhaustively tested implementation. Scope binding requests prompt
  unwinding, but cancellation may be caught, shielded, or resisted. Every
  liveness-sensitive commit therefore re-guards immediately before its effect
  even after scope binding.
- **Threading.** `guard()` is loop-only. Off-loop use (VAD/STT provider
  threads) is capture-then-reverify-on-loop; `is_current()` from another
  thread is inherently advisory. The memory model is stated explicitly in
  the primitive's docstring, not by analogy to `CancelToken`.

Migration order is semantic. The writer/phase inventory, canonical owners,
private publication compatibility, predicate conversion, phase-latch
classification, and commit guards land in Tier A.
`_cancel_cleanup_owns_turn` remains until the WS2.1 foundation is complete and
every turn child belongs to the bound scope. Replacement preserves
manager-started, session-only,
application, VAD, push-to-talk, replay, hand-built-event, reset/clear, and
successor-during-cleanup behavior. Scope cancellation does not remove commit
guards. Only then may the old identity generation carrier be removed.

Enforcement: L1 for new code (collaborator constructors take leases), L3
for the existing sites — a ratchet on `generation`/`_epoch`-named integer
fields declared outside the primitive module (pattern scoped to exclude the
STT watermarks, or the baseline is polluted from day one).

## 6. Mechanism 3 — engine/adapter for the peer families (Class C)

Apply the STT shape (`WebSocketSTTBase`) to the families that lack it —
with an honesty gate where the analogy is weakest.

**Precondition: the peer-set decision.** The critique's T1 recommends
demoting several bridges and deleting WebTransport; none of those cuts has
been decided. Building engines under peers whose continued existence is an
open question either wastes the migration or silently forecloses the
decision. Therefore: the demotion/deletion decision is recorded **before any
per-peer implementation migration begins** — transport epochs, transport or
integration scope adoption, the LangChain/LangGraph extraction, and transport
or bridge engines — and migration scope is the retained set. Peer-neutral
primitives and contract-suite rows may proceed; urgent product fixes do not
wait.
(An earlier draft claimed neutrality here; that was a sequencing decision
in disguise, and the review was right to call it.)

The decision is a checked-in ADR, not an informal consensus: it names every
transport and bridge, its disposition, compatibility/migration obligations,
and the owner/date for deferred removal. An undecided peer remains excluded.

### 6.1 Agent bridges: shared core first, generalization gated

One engine owning the choreography that is today implemented 7×: driving
the stream (`invoke` / `_drive_stream` / `_finalize_done` and the
GeneratorExit-vs-CancelledError arms), recorder-cursor lifecycle
(`turn_cursor` becomes engine-internal and thus universal), the
interruption protocol (`_plan_interruption` / `_apply_planned_mutation` /
`_serialize_framework_state` collapse to one implementation over an
adapter-supplied state codec), malformed-event tolerance, cancellation
drain (absorbing `_agent_runner.py`'s `_BridgeToolDrain`, used today by no
bridge), and close semantics.

Staged to control the heterogeneity risk (§2.3's caveat):

1. Keep the public `AgentBridgeContractSuite` portable; add only universally
   observable rows. Put fault injection/tool gates/close probes/history
   projection in an internal capability-driven scenario suite whose matrix
   requires every applicable retained built-in row without silent skips.
2. Sketch disposable adapters for **every** retained bridge before freezing an
   engine interface. Record family-neutral hooks and named SDK requirements
   such as llama's concurrent wait.
3. If both are retained, extract the LangChain/LangGraph shared core within the
   interface supported by those sketches.
4. Stop generalization only when a migration needs a framework-specific hook
   not pre-registered by the sketches. A peer-neutral refinement seen across
   sketches is not confused with an escape hatch.

Shared rows run against an unmarked offline fake on every PR. Real-SDK drivers
remain external; a Tier gate requires required extras checks or a fresh nightly
artifact at the exact candidate SHA. This is capability wiring as well as rows.

### 6.2 Transports: compose a connection-lifecycle controller

`ServerTransportBase` remains the WebSocket listener host it is today.
Retained WebSocket, Twilio, WebRTC, WebTransport, and local objects instead
compose a transport-neutral `TransportLifecycleController` where applicable.
Separate PRs add the FSM/reentrancy core, owned receive-task/event handling,
rollback/finalization with adapter-preserved exception policy, and connection-
epoch integration. Queue and bind policy stay in their own mechanisms.

Extraction preserves each transport's current lock-block versus task-join
behavior and cleanup exception type through adapter policy. Any convergence or
`ExceptionGroup` adoption is a later explicit behavior-change decision.

The public `TransportContractSuite` gains only portable observable rows. An
internal capability-driver suite injects races, rollback, late frames, and
queue faults into every retained built-in; a model fake tests the controller,
and a checked-in matrix prevents built-ins from passing through fake-only
coverage.

### 6.3 TTS residue

Fold the duplicated adapter callback set (`_get_mgr`, `_route_key`,
`_on_global_frame`, `_replay_request` ×3, …) into `_multi_context_ws.py`.
Small, mechanical, worth doing while touching the family.

## 7. Mechanism 4 — ratchets: adoption is closed, not optional

§2.4 shows the failure mode this repo actually has: the helper gets built,
adoption stays voluntary, fixes land in non-adopters. Every extraction
above therefore ships with enforcement. The pragmatist review reshaped the
mechanism — grep matches comments/docstrings, file-wide waivers permit growth
inside grandfathered files, TID251 cannot reliably infer an arbitrary loop
variable, and path counts conflict under parallel branches:

- **AST call-site fingerprints are the no-growth mechanism.** Production
  source only (`src/easycat/`) is fingerprinted by relative path, enclosing
  qualname, callee/construct shape, and a location-free normalized surrounding-
  AST hash (occurrence only distinguishes identical subtrees). This covers raw task spawns
  (including aliases and `loop.create_task`), `Task.cancelling`/`uncancel`,
  `except CancelledError`, module-global task sets,
  `gather(return_exceptions=True)` suppressing cancellation, shield-loops,
  and epoch fields. A new call in an already-grandfathered function fails;
  tests remain free to spawn raw tasks for race orchestration. Lexical import
  and assignment aliases are resolved; reflection is not guessed. Baselines
  update only through an explicit reviewed mode, and delete-plus-add swaps are
  regression-tested.
- **Ruff becomes a hard ban only at zero.** TID251 is used for a qualified
  API after its repository-wide source baseline reaches zero. There are no
  file-wide spawn waivers. `_concurrency.py` and
  `runtime/scope.py` are the sanctioned raw-spawn implementation modules;
  AST coverage remains for dynamic loop calls Ruff cannot prove.
- The existing C901/PLR list stays in Ruff, but its test fingerprints current
  violations so a new complex function in an already ignored file fails; an
  ignore-entry count alone is insufficient.
- Baselines are re-measured at freeze time, not copied from this document.
- One nav-coverage guard for `mkdocs.yml` (verified still missing; the
  critique's #91), same mechanism.

The ratchet counts become the plan's progress inventory; the *outcome*
metric is separate (§10).

## 8. Mechanism 5 — boundary primitives and effect-tested defaults

Covered in depth by critique T6/T7; restated here only as mechanisms:

- **Authorized bind capabilities**: inventory `websockets.serve`, aiohttp
  `TCPSite`, aioquic/WebTransport, direct socket binds, and intentional
  embedding/test exceptions. Typed backend wrappers (or an
  `authorized_bind(policy, binder)` capability) apply `enforce_bind_guard`
  while preserving backend errors. Ruff covers zero-baseline qualified APIs;
  the AST ratchet covers dynamic `sock.bind` aliases.
- **Secret hygiene as a test, not a habit**: extend the existing
  `tests/config/test_secret_reprs.py` discovery rather than duplicating it.
  Current main already covers provider catalogs, a curated config list, and
  nested named-provider parameters, but
  `WebTransportTransportConfig.auth_token` still leaks through repr. Fix it
  immediately, independent of peer disposition. Combine a source-level
  dataclass/secret-field inventory with runtime sentinel constructors from
  provider catalogs and an authoritative public-config registry; drift-check
  that registry against exports. Avoid recursive optional-SDK imports and do
  not treat an internal transport union as the whole public config surface.
- **Effect-tested defaults**: a default that *selects a behavior* ships with
  a test asserting the behavior's effect, not the value (`style="ssml"`
  passing a value-assert while being inert with all four TTS providers is
  the canonical failure).

## 9. What this does to churn specifically

- Class C fixes drop from N commits + N test files to 1 + 1 (the engine and
  its suite). The estimated ceiling is ~1,800-2,400 removable lines across
  transports + bridges (derived from the choreography table, discounting
  genuinely-specific bodies), but the commit-count effect is larger than
  the LOC effect — see the six-commit reconnect sequence.
- Class A/B fixes stop being whack-a-mole because the invariant lives in a
  primitive with its own exhaustive tests; the "harden slice N" audit shape
  (eight commits walking files) becomes structurally unnecessary.
- The meta-layer tax (19% coupling) shrinks via critique T5's cuts; this
  plan adds only: any new guard must assert values, never prose, and any
  new generated output must have exactly one generator.
- Peer-count is a churn multiplier, which is why §6 gates every per-peer
  implementation migration on the T1 peer-set decision instead of building
  under peers that may be deleted.

## 10. Sequencing, cost, and risk

The repo's own history is the cautionary tale for how *not* to do this:
`e99841a7` ("harden runtime lifecycle boundaries") touched 52 source files
(+4,370/-661 in `src/`; 100 files, +11,616/-743 with tests) and was
followed three days later by a 46-file review-feedback pass (`11cdd9d5`)
that re-touched the same provider set — including the identical edit
applied three times to three TTS files. Big-bang hardening sweeps are the
disease, not the cure.

The work is therefore tiered (severable, each tier valuable if the next
never ships), strangler-style, one-concern PRs, each independently
revertible. Honest total: summing the refactor plan's own items gives
roughly 45-60 PR-days of engineering effort, not the 4-8 an earlier draft
implied. Review bandwidth of one maintainer is the binding constraint. Work
advances on verified code and dependency readiness rather than elapsed time.

- **Tier A — ship unconditionally (~3 engineering weeks).** Foundations:
  the `_concurrency.py` helpers plus one ownership-proving vertical adoption,
  the teardown-budget manifest/defaults, the ratchet mechanism,
  journaling of discarded teardown-gather failures, the **websocket
  resampler-tail fence landed immediately as a plain bug fix** (it needs
  only the existing `_connection_generation` field, not the epoch
  primitive), the Epoch/Lease primitive with turn-writer inventory and
  session predicate adoption (but not old-carrier deletion), the
  stop()-ordering observation tests, the missing bridge contract rows, and
  the extension/fix of the existing secret-repr test. Every item is valuable
  standalone.
- **Tier B — structurally gated.** When all Tier-A contracts, parity tests,
  source ratchets, inventories, and normal PR checks are green, the
  peer-neutral scope-tree vertical slice and its dependent slices may proceed
  in reviewable order. Tier B then covers the remaining package-by-package
  scope adoption, the
  `Session.stop()` rewrite, remaining epoch conversions
  (transports/telephony), `bind()`, TTS residue.
- **Tier C — conditional on the T1 peer-set decision and named Tier-B code/test
  prerequisites.** The retained bridge and transport lifecycle engines. The
  peer-set decision also precedes the Tier-B transport/integration/epoch
  migrations, not merely Tier C.

**Falsifiability.** Before treatment, checked-in per-cohort manifests freeze
members, controls, minimum exposure, severity/adjudication rubrics, tolerance,
and invalidation rules. Each cohort later records its own completion SHA/date
and exact `[D-60d,D)` / `[D,D+60d)` windows. Reports persist adjudicated
multi-member recurrence and fix density (fix commits / all cohort-touching
commits), plus `post-pre` deltas against controls. Zero denominators,
underexposure, and treated controls are `insufficient_data`, not success;
healthy zero-fix baselines use zero-post/non-inferiority instead of impossible
strict decrease. Tier A measures Session lifecycle/staleness members; peer
families use their retained peers. The exact formulas live in the refactor
plan. These fixed-window outcomes and raw rolling-90-day share remain
observational context, never sequencing gates.

## 11. Non-goals and rejected alternatives

- **No rewrite, no framework switch.** The pipeline, event bus, provider
  protocols, and public API are untouched; the firm decisions in
  docs/architecture.md are inputs, not outputs, of this plan.
- **Not `asyncio.TaskGroup` wholesale — but not for the reason an earlier
  draft gave.** A *cancelled* child does not abort TaskGroup siblings
  (only a non-cancel child exception triggers abort), so token-driven
  barge-in is actually compatible with TaskGroup semantics, and
  `run_streaming_agent`'s `except BaseException: cancel-and-drain; raise`
  is a hand-rolled `TaskGroup.__aexit__`. The valid rejections are the
  *unstructured lifetimes*: named replaceable slots, self-detach,
  cross-turn ownership handoff, sync-context spawns. The refined position:
  **TaskGroup (or an equivalent structured block) for per-turn sibling
  sets inside a scope-owned task; RuntimeScope for free-lifetime tasks** —
  the scope-tree work should adopt or explicitly decline that hybrid for
  `run_streaming_agent`.
- **Not anyio/trio.** Structured concurrency via a new runtime dependency
  would be a larger migration with the same endpoint and worse ecosystem
  fit for the aiohttp/aiortc/websockets stack already in use.
- **Not a new event system.** Event-bus semantics (tokens, handler-error
  policy, slow-callback accounting) landed recently and are not implicated
  by the fix history at anything like the rate of Classes A-C.
- **Not freezing features.** Tiers are one-concern PRs interleavable with
  normal work; the ratchets keep new code on the new paths meanwhile.
