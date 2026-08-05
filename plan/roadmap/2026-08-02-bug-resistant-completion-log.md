# Bug-resistant refactor: completion log

Status: historical record.

The completed-work record for
[the bug-resistant refactor plan](2026-08-02-bug-resistant-refactor-plan.md).
Each section is the verbatim result paragraph for one delivered slice, moved
out of the plan so the plan reads as a forward backlog. Sections are in slice
order; the plan links to each by anchor.

This file is append-only in practice: a slice's result is written once, when it
lands. To see what remains, read the plan, not this log.


## WS0.2a

Freeze result after WS0.3b: 159 sites, comprising 32 timeout-bearing defaults,
61 bounded calls in lifecycle closures, and 66 named declarations. Their
reviewed classifications are 71 `lifecycle_budget`, 41 `protocol_local`, 17
`configurable`, and 30 `not_teardown`.

## WS0.2b2

Migration result: eight concrete values move to the canonical module. The
manifest grows from 159 to 165 sites because six inline or non-timeout-named
values now have explicit timeout declarations; the reviewed classification
totals become 77 `lifecycle_budget`, 41 `protocol_local`, 17 `configurable`,
and 30 `not_teardown` without adding runtime budget enforcement.

## WS0.2b3

Migration result: six concrete values — four journal process/thread joins and
two WebRTC cleanup bounds. Naming four formerly inline journal values and
retaining the overridable WebRTC audio class alias grows the reviewed manifest
to 170 sites, including 82 `lifecycle_budget` entries.

## WS0.2b4

Migration result: three canonical values now supply thirteen public server
configuration/default sites without changing their `30.0`, `10.0`, and `5.0`
values. The reviewed manifest grows from 170 to 173 sites, with 20
`configurable`, 82 `lifecycle_budget`, 41 `protocol_local`, and 30
`not_teardown` entries. WS0.2 is complete without changing runtime enforcement.

## WS0.3a

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

## WS1.1

Implementation result: the leaf primitive starts at generation zero,
atomically publishes replacements through `bump(value)`, and returns immutable
leases from `capture()`. A lease holds its captured generation and payload,
offers a thread-safe advisory `is_current()`, and makes loop-only `guard()`
either return `False` or raise `Stale`. The module-level contract documents the
mutex memory model and the mandatory re-guard immediately before an effect;
domain adoption remains in WS1.2+.

## WS1.2a

WS1.2a freeze result: 43 classified sites — six direct identity-pointer
assignments, six old-carrier assignments, three identity publish calls, ten
identity clear calls, three activity-state assignments, seven canonical
transition calls, three reset calls, three `TurnStarted` producers, and two
subscriptions. The manifest distinguishes the one command producer/subscriber
pair from the two observational application/text producers and browser
observer. A behavior contract freezes a correlated hand-built `TurnStarted`
installing Session identity before a later public type subscriber observes it.

## WS1.2b1

WS1.2b1 migration result: `TurnLifecycle` now owns the Session identity Epoch;
all production publications and clears use its synchronous seams, while the
private `_turn` compatibility property routes focused harnesses through the
same owner. The refreshed 44-site manifest has zero direct production
turn-pointer assignments, one identity-owner initialization, six publication
calls, fifteen clear calls, and four legacy-carrier assignments. The only new
carrier write is the asserted `TurnContext.generation` dual-write. Manager
activity ownership remains isolated to WS1.2b2.

## WS1.2b2

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

## WS1.2c

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

## WS1.2d1

WS1.2d1 inventory result: 73 synchronous predicate sites are frozen across the
three turn collaborators — eight identity-pointer checks, nine legacy identity-
generation checks, nine activity-state checks, fourteen cancellation checks,
three token-owner checks, twenty-nine null/no-turn checks, and one explicit
preemptive phase latch. The location-free AST ratchet distinguishes all seven
semantics and rejects structural replacement drift. WS1.2d2 must remove the
pointer/generation/activity liveness mechanisms through lease adoption while
leaving cancellation, token ownership, null-object semantics, and the one-way
phase latch independently visible.

## WS1.2d2a

WS1.2d2a migration result: the Session wiring now exposes atomic
identity capture; `TurnRunner` carries the captured lease through end-of-speech,
streaming TTS, application prompts, and preemptive attempts; and `TTSScheduler`
re-guards that same lease before every post-await stop, drain, mark, or clear
effect. Same-object republication contracts prove an epoch bump fences stale
work even when pointer identity is unchanged. The refreshed 55-site predicate
manifest has zero identity-pointer and zero legacy identity-generation checks;
the nine activity checks remain deliberately frozen for WS1.2d2b, alongside
the independent cancellation, token-owner, null-object, and phase-latch
semantics.

## WS1.2d2b

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

## WS1.2e1

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

## WS1.2e2

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

## WS1.3b

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

## WS2.7a

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

## WS2.7b

WS2.7b maps the complete `Session.stop()` symbol to the WS2.1 scope model.
The inventory boundary is the whole method, from its public contract and entry
checks through the last ownership-only `finally` action; it is not a selected
teardown-body line range. The source snippets below are the durable anchors if
line numbers move.

### Scope phase plan

The rewrite uses the following phase order. Before the force path awaits any
member, it synchronously signals every scoped task whose force policy requires
cancellation, across all cohorts. That root-wide barrier preserves the current
ordering for pipeline, barge-in, greeting, STT, heartbeat, and other migrated
work; phase order controls the subsequent drains and finalizers, not which
force-cancel members observe the initial broadcast. A task may still select a
different cohort in graceful and force mode. Finalizer factories may select the
existing graceful/force branch, but do not introduce a new timeout or error
policy.

| phase | scope members or finalizer | preserved edge |
|---|---|---|
| `application-prompt` | active application-prompt task | Graceful waits without token cancellation or a deadline; force signals its token, cancels it, and uses the existing prompt drain bound. A prompt that is itself calling `stop()` remains excluded. |
| `turn-token` | finalizer wrapping the guarded active-turn token signal | Runs only after the graceful prompt has finished and skips the prompt-owned reentrant case. |
| `text-turn` | active text-turn task | Signal the text token, cancel the task, then drain it before later teardown. No new deadline is added. |
| `preemptive` | speculative agent-generation task | Cancel and drain before the wrapped agent can close. |
| `pipeline` | audio ingress, active voice/TTS turn, outbound pump, and child work assigned to this cohort | Its force-cancel members participate in the root-wide synchronous signal barrier before any cohort is drained. Graceful mode keeps each member's reviewed action rather than inheriting the force row. |
| `barge-in-cleanup` | detached barge-in cleanup task | Graceful finish keeps providers, transport, and journal live. Its force row participates in the initial root-wide signal barrier, then cancels and drains in this phase. |
| `greeting` | call-answered greeting task | Preserves the current cancel/drain point before STT cleanup. |
| `stt-runtime` | pause commit, segment commit, concurrent final close, and event consumer | Drains before the STT provider finalizer; provider-local timeout/error handling stays inside `STTCommitter`. |
| `stt-finalize` | finalizer wrapping `STTCommitter.cancel()` and handle clearing | Runs while providers, transport, and journal are live. |
| `stt-receive` | provider WebSocket receive loops | Drains only after `stt-finalize` has sent provider finalization and closed or released the socket; force may still cancel it through the earlier whole-root broadcast used by the current stop path. |
| `tts-finalize` | mode-aware finalizer wrapping the current graceful scheduler cancellation and handle clearing | Preserves the graceful-only provider cleanup; force has already applied its task policy. |
| `tts-runtime` | active voice/TTS turn after the graceful scheduler finalizer | Accounts for the task the scheduler has drained; its force row instead belongs to the earlier `pipeline` barrier. |
| `ingress-stop` | finalizer wrapping `AudioRouter.stop_ingress()` | Completes before health checkers and all externally visible resource finalizers. |
| `health-stop` | one composite finalizer over the current checker snapshot | Preserves list order and clears the list only after successful completion. |
| `helpers-stop` | finalizer wrapping `_stop_helpers()` | Keeps its existing per-helper log-and-continue error policy. |
| `queue-close` | ownership-aware finalizer | Closes only the Session-owned outbound queue. |
| `outbound` | outbound pump, AEC-degraded emit, and the audio-router inline-send child scope | Drains before transport disconnect. Cancellation-resistant inline sends keep their supervisor reservation and use the reviewed owned-task hard-deadline/parking path. |
| `heartbeat` | pipeline heartbeat task | Settles after outbound shutdown and before transport disconnect. |
| `transport-disconnect` | finalizer wrapping `transport.disconnect()` | Transport remains connected until outbound and inline writes settle or park. |
| `transport-events` | transport diagnostic and delivery event tasks | Finishes best-effort event dispatch after disconnect has stopped new transport work and before manager shutdown. |
| `supervisor-events` | supervisor listener audit event tasks | Finishes Session-attached audit dispatch before manager shutdown; standalone broadcaster drains keep the same boundary. |
| `manager-shutdown` | finalizer wrapping `TurnManager.shutdown()` | Follows transport disconnect. |
| `agent-close` | finalizer wrapping `aclose_if_supported(agent)` | Follows manager shutdown and retains the existing log-and-continue policy. |
| `audio-providers-close` | one composite finalizer over deduplicated STT/TTS/VAD/NR/AEC providers | Follows agent close; provider siblings retain no contractual total order and keep per-provider log-and-continue handling. |
| `tts-socket-close` | retryable persistent TTS manager close finalizer | Explicit provider close invokes this shared transaction from `audio-providers-close`; the named phase lets root close observe the same retained result without spawning or repeating cleanup. |
| `tts-receive` | persistent provider WebSocket receive loops | Drains after `audio-providers-close` has released the persistent socket; force may still cancel it through the earlier whole-root broadcast used by the current stop path. |
| `identity-clear` | finalizer wrapping `TurnLifecycle.clear_identity()` | Runs after every provider sibling and before debug backend finalization. |
| `debug-finalize` | finalizer wrapping `_finalize_debug_backends()` | Preserves the read-only journal/artifact postmortem view. |
| `closed-publish` | finalizer wrapping `_mark_closed()` | Wakes `wait_closed()` only after live backends are finalized. |
| `emergency-export-release` | ownership-aware finalizer for the optional unregister hook | Runs only after clean closed publication, matching the current lifecycle. |

The task policies required by that plan are frozen as follows. `finish` and
`cancel` are `RuntimeTaskAction` values; `none` means the current unbounded
behavior remains unbounded.

| task member | graceful row | force row |
|---|---|---|
| `application_prompt` | `application-prompt`, no token, `finish`, no deadlines | `application-prompt`, signal token, `cancel`, existing application-prompt hard bound; park if cancellation-resistant |
| `text_turn` | `text-turn`, signal token, `cancel`, no new deadline | same |
| `preemptive_agent_generation` | `preemptive`, no token, `cancel`, no new deadline | same |
| `audio_ingress_pipeline` | `pipeline`, no token, `cancel`, no new deadline | `pipeline`, no token, `cancel`, no new deadline |
| `on_turn_ended` / active voice-TTS turn | `tts-runtime`, no member-local token signal, `finish`, no new deadline; `tts-finalize` performs the current scheduler cancel/drain | `pipeline`, no member-local token signal, `cancel`, no new deadline |
| `audio_outbound_drain` | `outbound`, no token, `cancel`, no new deadline | `pipeline`, no token, `cancel`, no new deadline |
| `audio_inline_send` child | `outbound`, no token, `finish`, reviewed owned hard deadline | `pipeline`, no token, `cancel`, reviewed owned hard deadline |
| `aec_reference_degraded_emit` | `outbound`, no token, `cancel`, no new deadline | `pipeline`, no token, `cancel`, no new deadline |
| `barge_in_cleanup` | `barge-in-cleanup`, no token, `finish`, no new deadline | `barge-in-cleanup`, no token, `cancel`, no new deadline |
| `call_answered_greeting` | `greeting`, no token, `cancel`, no new deadline | `greeting`, no token, `cancel`, no new deadline |
| STT pause/segment/final-close/event-loop tasks | `stt-runtime`, no member-local token signal, current action and provider-local bounds | same cohort with the reviewed force action; the earlier `turn-token` phase supplies cooperative cancellation and no scope-level bound replaces a provider bound |
| provider WebSocket receive loop | `stt-receive`, no token, `finish`, no scope deadline; `stt-finalize` closes the socket first | same; the current force path's earlier whole-root cancellation remains preserved until the Session rewrite |
| persistent TTS WebSocket receive loop | `tts-receive`, no token, `finish`, no scope deadline; `audio-providers-close` closes the socket first | same; the current force path's earlier whole-root cancellation remains preserved until the Session rewrite |
| `pipeline_heartbeat` | `heartbeat`, no token, `cancel`, no new deadline | same |
| transport diagnostic and delivery event tasks | `transport-events`, no token, `finish`, no new deadline | same |
| supervisor listener audit event tasks | `supervisor-events`, no token, `finish`, no new deadline | same |

The inline-send hard deadline is the one planned **[behavior change]** in this
mapping: after the existing cancellation grace and transport-termination
attempt, a write that still ignores cancellation is parked as an owned
survivor instead of keeping `Session.stop()` pending forever. It reuses the
reviewed inline-send cancellation budget; it does not add a new timeout value.
The graceful application prompt remains unbounded, and every other unbounded
wait above remains unbounded.

### Complete current-symbol mapping

| current `Session.stop()` span | target home | retained behavior |
|---|---|---|
| Public docstring and `force` argument | Session policy | The single public `stop(force=)` verb and postmortem contract stay source-compatible. |
| `current_task` validation, start-task reentrancy rejection, and the pending-cancellation checkpoint | Session policy | Entry errors and caller-cancellation discrimination are unchanged. |
| Force/graceful `_start_lock` fencing, `_stopping` publication, startup-task cancellation, and `SESSION_FORCE_START_LOCK_TIMEOUT_S` | Session policy before root close | Startup admission closes before force cancellation; graceful still serializes with startup; the existing force bound is not replaced. |
| `_stop_task` / `_stop_force` ownership loop, self-owner return, shielded join, and force takeover | Session policy plus `RuntimeScope.close()` controller | Session retains the bookkeeping owner; the root controller supplies phase-aware graceful-to-force supersession and joiner cancellation discrimination. |
| `_is_running = False` and `stop_error` initialization | Session policy | Publication and failure precedence remain outside task mechanics. |
| `superseded_task` bounded unwind and warning | Root close supersede budget, surfaced through Session policy | Reuses `SESSION_SUPERSEDED_STOP_TIMEOUT_S`; no merge SHA, date, or new timeout participates. |
| `_closed` idempotence check | Session policy | An already cleanly stopped Session returns without reopening the scope. |
| Graceful active-prompt wait and `prompt_is_current` exclusion | `application-prompt` task policy | Unbounded, uncancelled graceful completion and reentrant self-exclusion are preserved. |
| Active-turn token cancellation | `turn-token` finalizer / cooperative member signal | Still follows graceful prompt completion and skips a prompt that owns the current stack. |
| Text token signal, task cancellation, drain, and expected error suppression | `text-turn` cohort | Signal precedes task cancel; current suppression and lack of a new bound remain. |
| Force-only prompt cancellation | force row of `application-prompt` | Uses the existing cooperative signal, task cancel, and drain bound. |
| Preemptive-generation cancel/drain | `preemptive` cohort | Completes before agent finalization in both modes. |
| Force pipeline/TTS/outbound collection, synchronous cancel barrier, and task awaits | force row of `pipeline` | Every pipeline member is signalled before the first drain; siblings gain no invented total order. |
| Force STT cleanup, remaining scoped drain, and task-handle clearing | `stt-runtime`, `stt-finalize`, and retained-result inspection | STT/runtime work finishes while providers live; handle clearing follows settlement; suppressed cleanup failures remain inspectable when policy requires it. |
| Graceful pipeline cancellation and expected cancellation logging | graceful row of `pipeline` | Keeps the existing graceful action and diagnostic. |
| Graceful barge-in, greeting, STT, and TTS cleanup chain | `barge-in-cleanup`, `greeting`, `stt-runtime`, `stt-finalize`, `stt-receive`, and `tts-finalize` | Preserves every WS2.7a partial-order edge without asserting sibling order. |
| `stop_ingress`, checker loop/list reset, helper stop, and conditional queue close | `ingress-stop` through `queue-close` finalizers | Exact ownership checks and error policies stay inside their wrappers. |
| `stop_outbound`, inline-send drain, heartbeat drain, and handle clearing | `outbound` and `heartbeat` cohorts plus their small handle-clear finalizers | All outbound work precedes transport disconnect; owned survivors remain anchored and observable. |
| Transport disconnect and diagnostic drain, supervisor audit drain, manager shutdown, suppressed agent close, deduplicated audio-provider closes, and persistent TTS reader drain | ordered resource finalizers plus `transport-events` and `supervisor-events`, followed by `tts-socket-close` and `tts-receive` | Existing propagation/suppression rules remain local; provider siblings remain unordered by contract, the explicit socket-close phase reuses the provider-triggered finalizer result, and the socket closes before its reader is joined. |
| Identity clear, debug backend destruction/postmortem swap, closed publication, and optional emergency-export unregister | finalizers `identity-clear` through `emergency-export-release` | Journal readability and close notification retain their current order. |
| `except BaseException` conversion into `stop_error` followed by re-raise | Session policy | Caller outcome remains primary; cancellation is stored as the existing cleanup error shape. |
| `owns_stop` check and failed-stop EventBus poisoning | Session policy in `finally` | A superseded graceful caller cannot release the force owner's resources. |
| EventBus unsubscription and `_stop_task` / `_stop_force` reset | Session policy in `finally` | Owned subscriptions are always released by the eventual owner. |
| Success/failure `_lifecycle_cleanup_error` and `_stopping` update | Session policy in `finally` | Clean stop reopens no admission; failed stop keeps startup blocked until retry. |
| Observability release, log-context reset, and debug-bundle record | Session policy in `finally` | These run once for the owner after either success or failure. |

**Completeness verdict:** every statement in the current symbol has one home,
and the WS2.1 API needs no additional lifecycle primitive before adoption.
WS2.7c is therefore unlocked by the structural package-adoption dependency,
not by a calendar window, merge SHA, or observation date.

## WS3.1a

WS3.1a inventory result: the checked-in 7-by-5 matrix freezes 35 cells. Thirty-
three are required and two are not applicable: generic workflows have no
provider event taxonomy, while Llama interruption metadata does not rewrite
assistant history. Existing focused tests cover twenty required cells; thirteen
capability-level gaps remain for WS3.1d. The matrix records reset isolation and
JSON-safe snapshotting as postconditions of every applicable scenario. This
inventory slice changes no production behavior; later WS3.1 slices must evolve
the same matrix until no required cell remains `missing`.

## WS3.1b

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

## WS3.1c

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

## WS3.1d1

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

## WS3.1d2 prerequisite fix

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

## WS3.1d2

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

## WS3.1d3 prerequisite fix

WS3.1d3 prerequisite fix result: the OpenAI Agents history-isolation probe
reproduced an empty-current-turn corruption: both interruption rewriting and
post-processing scanned backward past the latest user entry and edited the
prior assistant reply. Both reverse scans now treat that user entry as the
current-turn boundary. Reaching it before an assistant message makes the
rewrite a no-op, preserving both prior history and an active response-id chain;
normal current-turn assistant rewrites retain their existing behavior. WS3.1d3
driver wiring remains pending in the next PR.

## WS3.1d3

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

## WS3.1d4

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

## WS3.1d5

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

## WS3.1d6

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

## WS4.1a

WS4.1a inventory result: the checked-in 5-by-8 matrix freezes forty required
cells across Local, WebSocket, Twilio, WebRTC, and WebTransport. Thirty-six
cells have exact pre-harness tests and four remain named gaps: Local connect
leadership, Local disconnect-during-connect, Local interrupted-disconnect
publication, and Twilio queue overflow. All five capability drivers remain
explicitly pending. This inventory changes no transport behavior; later WS4.1
slices must evolve the same registry until every required row is wired and no
pending driver remains.

## WS4.1b

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

## WS4.1c

WS4.1c result: the source-compatible public `TransportContractSuite` now
checks repeated and concurrent connect callers, repeated disconnect, and
disconnect-driven termination of an already-active inbound iterator. These
rows use only the existing public factory and transport methods. Backend
leadership counts, gated races, rollback resources, late-frame injection,
queue pressure, degraded events, and lifecycle publication remain internal
capability-driver responsibilities rather than new third-party hooks.

## WS4.1d1

WS4.1d1 result: Local and WebSocket now run all eight shared lifecycle rows
through credential-free capability drivers. The Local driver replaces only
module resolution with deterministic input/output stream resources while the
real transport owns rollback, callback-generation fencing, queue policy,
receiver termination, and cleanup. The WebSocket driver covers both shipped
lifecycle models: listener leadership/serialization on `WebSocketTransport`
and accepted-socket rollback/retained cleanup on
`WebSocketConnectionTransport`. Internal class policy values preserve the
reviewed difference between cancellation and lock-queued startup, and between
public disconnect publication and retained cleanup, without weakening shared
resource, generation, queue, or quiescence assertions. The execution registry
now has two complete and three pending drivers; thirty-nine of forty
pre-harness cells have exact evidence, with only Twilio queue overflow still
missing.

## WS4.1d2

WS4.1d2 result: `TwilioConnectionTransport` now runs every shared lifecycle
row through a credential-free accepted-socket driver. Deferred-start gating
proves single-flight connect leadership and disconnect invalidation outside
the lifecycle lock; the explicit socket-close ledger proves interrupted
cleanup and retry; the real Media Streams SID filters prove late-frame
fencing; and the inherited bounded ingress queue proves the missing overflow
drop plus canonical degraded event. Three drivers are complete and two remain
pending. All forty pre-harness matrix cells now have exact evidence.

## WS4.1d3

WS4.1d3 result: `WebRTCTransport` now runs every shared lifecycle row without
network sockets or optional aiortc/aiohttp installations. The capability
driver separates signaling-stack leadership and rollback from peer-offer
cancellation and generation fencing, while using the real inbound/outbound
queues, degraded publication, receiver boundary, consumer reaping, and
retained cleanup ledger. Four drivers are complete; only WebTransport remains
pending. The forty-cell pre-harness evidence inventory remains closed.

## WS4.1d4

WS4.1d4 result: WebTransport now runs every shared lifecycle row without
network sockets or the optional aioquic installation. The capability driver
uses the outer transport for serialized QUIC-server startup and the real
accepted-session transport for writer cancellation, rollback, bounded queues,
and receive termination. Its generation row admits an active session through
the real server dispatch path, closes admission during stop, and proves a late
session is force-closed without spawning a handler. All five drivers are now
complete and all forty pre-harness evidence cells remain closed; WS4.1d5 owns
the separate zero-pending execution ratchet.

## WS4.1d5

WS4.1d5 result: the execution registry is now closed rather than merely
progress-bearing: all five shipped transports must remain `complete`, and the
ratchet rejects any reintroduced `pending` state. The nightly extras matrix
selects the deterministic real aiortc/PyAV audio tests for the `webrtc` cell
and the real aioquic protocol/server tests for the `webtransport` cell. Each
exact node rejects zero tests, skips, failures, errors, or a checkout SHA that
differs from the candidate SHA, then uploads its JUnit result and JSON
attestation under an artifact name containing that SHA. Local PortAudio
device behavior is not claimed as portable CI evidence: the local extra keeps
its install/import smoke, while the credential-free capability driver remains
the deterministic lifecycle proof. WS4.1 is complete as the Tier-A transport
safety net.
