# ADR: peer-set disposition for transports and agent bridges

Status: accepted.
Date: 2026-08-03.
Decider: Yi Ding.
Supersedes: nothing. This is the first record of the peer-set decision.

This ADR discharges the blocking decision named in
[the bug-resistant refactor plan](2026-08-02-bug-resistant-refactor-plan.md)
and in §6 of
[the bug-resistant architecture proposal](2026-08-02-bug-resistant-architecture.md).
It exists to answer one question before per-peer implementation migrations
start: which transports and agent bridges will still be in this repository when
those migrations finish.

## Decision

**Every shipped transport and agent bridge is retained in-tree. Nothing is
demoted to out-of-tree, and nothing is deleted.**

All twelve peers below are retained. There are therefore **no deferred
removals**, and consequently no owner/date rows to record for one — the plan's
fourth required field is discharged by the decision being uniform, not by
omission.

## Context

The critique's T1 recommended demoting several agent bridges to out-of-tree
status and deleting WebTransport. Its mechanism argument was specific and, at
the time it was written (2026-07-26), correct: roughly 9,900 LOC of agent
bridges were coupled to six pre-1.0 SDKs with *no* automated compatibility
signal. CI installed none of them, the nightly job with SDKs present pointed at
a directory containing zero `importorskip` gates, and nearly every capability
probe was a soft `getattr` or `except ImportError: pass`, so upstream drift
degraded silently. T1's conclusion followed from that: "until an SDK executes in
CI, adding a bridge is adding liability, not capability."

That premise no longer holds. WS3.1 and WS4.1 — both Tier-A safety nets,
completed 2026-08-03 — built precisely the missing signal:

- All seven bridges and all five transports run every required lifecycle
  scenario through credential-free capability drivers **on every PR**. Both
  execution registries are closed by ratchet: a reintroduced `pending` driver
  fails CI.
- The nightly extras matrix installs each real SDK in isolation, reruns the
  exact public contract class, verifies that `git rev-parse HEAD` matches the
  candidate SHA, and uploads a JUnit result plus a JSON attestation. Zero
  tests, skips, failures, or errors are all rejected.
- The harness found and fixed three real defects during construction: the
  LangChain/LangGraph pending-tool drain, the OpenAI Agents prior-turn history
  corruption, and the Twilio queue-overflow gap.

Two further T1-adjacent premises are also stale. WebTransport had "zero
occurrences of `token` or `auth`" when the critique was written; it now
enforces bearer auth on the HTTP/3 CONNECT request before allocating a session,
with `allow_query_token` and `unsafe_allow_no_auth` as explicit opt-ins. And
`mkdocs.yml` nav coverage, T5's companion complaint, is now guarded.

So the question this ADR actually answers is not T1's "cut what we cannot
test." It is "we can test them — is each worth its maintenance and review
bandwidth?" The answer is yes for all twelve, because the per-peer marginal
cost fell sharply when the harness landed: a peer's lifecycle behavior is now
proven mechanically rather than by maintainer attention.

### What this decision does not claim

The scenario suites prove **lifecycle** behavior against controlled drivers and
**contract/import** behavior against real SDKs nightly. They do not prove
end-to-end conversational semantics per bridge, and they do not prove real
device behavior for the local transport — WS4.1d5 deliberately declined to
claim PortAudio as portable CI evidence. Retention is a judgment that the
remaining exposure is acceptable, not an assertion that it is zero.

## Dispositions

### Transports

| peer | path | disposition | obligations |
|---|---|---|---|
| Local | `transports/local.py` | retained in-tree | Real device behavior stays outside portable CI; the credential-free capability driver remains the deterministic lifecycle proof and the `local` extra keeps its install/import smoke. |
| WebSocket | `transports/websocket.py` | retained in-tree | None beyond the standard migration set. |
| Twilio Media Streams | `transports/twilio_media.py` | retained in-tree | Each semantic clause of its disconnect predicate stays covered as the scenario suite evolves. |
| WebRTC | `transports/webrtc.py` | retained in-tree | Its two-field retire case (`_peer_generation` / `_retiring_peer_generation`) is a named WS1.4 conversion, not a mechanical rename. |
| WebTransport | `transports/webtransport.py` | retained in-tree | Explicitly reverses T1's deletion recommendation on the grounds recorded above. Its aioquic listener is an in-scope backend for WS5.1's `authorized_bind` migration. |

### Agent bridges

| peer | path | disposition | obligations |
|---|---|---|---|
| Generic Workflow | `integrations/agents/generic_workflow.py` | retained in-tree | Retained partly *as* the extension seam: with `template.py` it is the shape third parties copy, so it migrates last in WS3.3 and must keep showcasing the adapter form. |
| Remote Responses API | `integrations/agents/responses_api.py` | retained in-tree | None beyond the standard migration set. |
| OpenAI Agents | `integrations/agents/openai_agents.py` | retained in-tree | WS3.3's designated first migration. Deleting its private `_resolve_model_id` requires the shared helper to preserve exactly the old accepted shapes; widening remains a separate `[behavior change]` PR. |
| LangChain | `integrations/agents/langchain.py` | retained in-tree | Retained together with LangGraph, which is what makes WS3.2's shared-core extraction a live slice. |
| LangGraph | `integrations/agents/langgraph.py` | retained in-tree | Same. The near-fork choreography around `_langchain_events.py` is to be deleted by WS3.2, not deprecated. |
| PydanticAI | `integrations/agents/pydantic_ai.py` | retained in-tree | Both supported SDK generations stay in the nightly extras matrix; dropping one is a separate decision. |
| Llama Agents | `integrations/agents/llama_agents.py` | retained in-tree | **Pre-registered exemption:** its SDK requires concurrent waiting, so a concurrency hook for it does not trip WS3.3's generalization gate. Recording it here is what makes it an anticipated requirement rather than a mid-migration escape hatch. See below. |

## Consequences

### Scope of what this discharges

This ADR satisfies the peer-set precondition for WS1.4, WS1.5, WS2.2-2.6,
WS2.7b-c, WS3.2+, WS4.2+, WS5.1, and WS5.3. All of those now migrate the full
shipped set rather than a subset — more total work than a demotion would have
required, and that is the accepted cost of the decision.

It does **not** waive their named implementation prerequisites. Tier-B slices
require the deterministic Tier-A structural exit; Tier-C engines require the
relevant workstream prerequisites. The cohort member arrays remain locked so
the optional longitudinal measurement stays comparable, but no measurement
window delays implementation.

### Measurement membership must be locked before treatment begins

**Done on 2026-08-04**, against this ADR's merge SHA
`df517aeca8409b9cd5eab3b0767d837ec41b0afe`. Both Tier-B cohorts are now
`status: preregistered` / `anchor.status: pending` with all twelve peers as
members — seven agent bridges, five transports — and `candidate_members`
removed. `tests/test_refactor_metrics.py` pins the exact member ids, so a later
addition or removal fails loudly instead of silently changing what a
peer-family outcome means. The rest of this section records why it was done
this way.

`plan/metrics/refactor-families.json` froze both Tier-B cohorts at
`status: blocked_peer_decision` with empty `members` arrays. Before the first
production commit of WS1.4, WS2.3, WS2.6, or WS5.1, both cohorts had to move to
`status: preregistered` with `anchor.status: pending`, and every
`candidate_members` entry copied verbatim into `members` — twelve peers, no
additions and no omissions.

Two mechanics matter here. First, `member_selection.decision_sha` records this
ADR's merge SHA, so the lock lands in a commit *after* this document merges;
that is compatible with the pre-registration rule, which requires only that the
lock precede the first treatment commit. Second, the current ratchet in
`tests/test_refactor_metrics.py` forbids `member_selection` on a cohort whose
status is `preregistered` — a shape written for the Tier-A cohort, which never
had a selection rule. The lock therefore also extends that test to accept a
retained `member_selection` carrying non-null `decision_sha` and `locked_at`,
because `plan/metrics/README.md` requires the ADR SHA to stay recorded.

After the lock, a peer may not be added to either cohort. Removing one
invalidates that cohort rather than silently changing the denominator — which
is the concrete cost of reversing this ADR mid-treatment, and the reason the
reversal triggers below are deliberately narrow.

### The generalization gate is unchanged

WS3.3's gate still stops a migration that needs a framework-specific engine
hook not pre-registered by the retained-peer adapter sketches. Retaining every
bridge widens the set of sketches required before an engine interface may be
frozen — all seven, not a survivor subset. The Llama Agents concurrency
requirement above is pre-registered *now*, so it counts as a named SDK
requirement rather than a gate failure. No other exemption is pre-registered;
one discovered later is a gate failure and must be argued on its own.

### Revisit triggers

This is a decision, not a deferral: these peers are in-tree, and downstream
work may build on that. It is revisited only if one of the following occurs,
and a revisit produces a superseding ADR rather than an informal reversal:

1. A retained peer's upstream SDK is abandoned, or ships a breaking change its
   nightly extras cell cannot be made to pass within one release cycle.
2. A retained peer's required scenario rows cannot be kept green without a
   silent skip — i.e. the compatibility signal this decision rests on stops
   being real for that peer.
3. Review bandwidth, measured by the plan's own binding constraint rather than
   by impression, makes the full-set migration undeliverable.

Trigger 2 is the important one: this ADR is downstream of the harness, so if
the harness stops covering a peer, the basis for retaining it is gone.
