# Debug-first runtime redesign: workstream record

> **Status: historical record.** Archived 2026-08-03, replacing the eight
> documents that were `plan/workstreams/`. Current source of truth: the code
> and its tests, [../roadmap/current-code-status.md](../roadmap/current-code-status.md)
> for source-tree status, and [../roadmap/open-backlog.md](../roadmap/open-backlog.md)
> for the gaps these records left open. Nothing in this file is actionable.

Seven workstreams delivered the debug-first runtime redesign. The retired
records carried 227 checked boxes; the paragraphs below say what each
workstream actually produced, and name the commit evidence where a checked box
was wrong.

**WS1 — Journal foundation.** Made the journal the single source of truth for
observability. Shipped: `src/easycat/runtime/journal.py`, `journal_sql.py`,
`journal_memory.py`, `journal_views.py`, `journal_retention.py`,
`journal_factory.py`, `_journal_codec.py`, `_journal_lock.py`, plus
`artifacts.py`, `safe_defaults.py`, and `crash_sweep.py`. The durability
contract is maintained at `src/easycat/runtime/DURABILITY.md` and the
acceptance criteria are executable under `tests/runtime/`. The surfaces it
replaced are gone: `EventTraceLogger`, `SpanManager`, and `InMemoryMetrics`
return no hits in `src/` or `tests/`. 94 of 94 boxes checked, and every
substantive claim re-verified.

**WS2a — Agent bridges.** One `ExternalAgentBridge` protocol behind every
framework. Shipped: `src/easycat/integrations/agents/` with `openai_agents.py`,
`pydantic_ai.py`, `langchain.py`, `langgraph.py`, `llama_agents.py`,
`responses_api.py`, `generic_workflow.py`, and the executable `template.py` —
three more bridges than the plan anticipated, which is itself evidence the
plan ran behind the code. Lifecycle acceptance now lives in the six
`tests/integrations/agents/test_bridge_lifecycle_*.py` suites. The record's
486-line appendix of worked bridge examples is superseded three times over by
`docs/extending/agent-bridge.md`, `docs/teaching/14-bring-your-own-agent/`, and
`template.py`.

**WS2b — Interruption and MCP.** Shipped: the four-step interruption protocol
and MCP config plumbing (`mcp_servers` at
`src/easycat/integrations/agents/base.py:273`). Not shipped, despite 41 of 41
boxes checked: `DRAIN_CURRENT_UNIT` and `DRAIN_TO_COMMIT_POINT` exist only as
enum members at `base.py:40-41` while `src/easycat/session/interruption.py:56`
hard-codes `CancellationMode.IMMEDIATE_STOP`; `shallow_mode_downgrade` returns
no hits in `src/` or `tests/`; and the MCP round-trip test only asserts
attribute storage. Those three gaps and their design specs were lifted into
[../roadmap/open-backlog.md](../roadmap/open-backlog.md) before this record was
written.

**WS2c — Remote bridge.** Shipped: `src/easycat/integrations/agents/`
`responses_api.py`. Not shipped, despite 48 of 48 boxes checked: T2C.4
capability discovery. `supports_interruption`, `supports_drain`, and
`easycat.framework` return no hits in `src/` or `tests/`, and
`responses_api.py:527-535` pushes `easycat.*` turn metadata to every Responses
API server unconditionally — the exact plain-server case the plan said to
detect and stay quiet for. Lifted to the open backlog alongside the WS2b drain
gap; they are two halves of one negotiation story.

**WS3 — Stage refactor.** Shipped: `src/easycat/stages/` (`agent.py`,
`audio.py`, `stt.py`, `transport.py`, `tts.py`, `turn.py`, `vad.py`,
`base.py`). Three checkboxes were false and all three are adjudicated. The
"missing" modules were not renamed — they were deliberately deleted as dead
code by `f0090412` (2026-04-23), whose commit message records the replacement
paths: `session/_interruption_controller.py` was unused and the real path is
`session/interruption.py::estimate_and_notify_interruption`;
`session/_voice_delivery_ledger.py` was unused because `TurnContext` already
held the state; `stages/telephony.py` was a no-op passthrough. The `<500`-line
`Session` target was formally retracted by the workstream's own AC3.10a.
`perf/ws3-final.json` is byte-identical to `perf/baseline.json` (both
`md5 44fd1433c29185c64ca3db08a8abfb01`); the critique ruled that a housekeeping
nit rather than a finding, and the delete-or-regenerate task is in the open
backlog.

**WS4 — Replay and bundle.** Shipped: `src/easycat/runtime/replay.py`,
`src/easycat/debug/bundle.py`, `export.py` and the `_bundle_*.py` helpers, the
stage replay hooks at `src/easycat/stages/vad.py:244` and
`src/easycat/stages/turn.py:153`, `runtime/crash_sweep.py`, and the
`inspect` / `replay` / `diff` / `latency` / `tail` / `bundles` / `debugger` /
`journal` CLI surface under `src/easycat/cli/debug/`. Unusually, its
forward-looking residue had already been promoted correctly into peripheral
documents while it was still active — the operating model's promotion flow
working as designed, which is why nothing needed to survive here beyond this
paragraph.

**WS5 — Legacy removal.** The most verifiable work in the redesign: every file
named for removal is gone. `src/easycat/event_logging.py`,
`src/easycat/metrics.py`, `src/easycat/agent_runner.py`, and the
`src/easycat/agents/` package all fail `ls`, and `EventTraceLogger`, `Tracer`,
`SpanManager`, and `InMemoryMetrics` grep clean in `src/` and `tests/`.
`AgentRunner` survives only as
`src/easycat/integrations/agents/_agent_runner.py`. Two checkboxes are false
but moot rather than live: the T5.9 migration guide for external consumers
never shipped and has no audience, because `git tag` is empty and
`pyproject.toml` pins `version = "0.1.0"` — there is no released version to
migrate from. Four other workstream records cross-referenced that guide. Stop
hunting for it.

## Corrected drift ledger

The retired `plan/workstreams/README.md` carried a drift ledger that had
itself drifted. These are the corrections.

| Claim in the retired records | Current fact (2026-08-03) |
|---|---|
| `InterruptionController`, `VoiceDeliveryLedger`, and `stages/telephony.py` "are not present as current source files" | Correct but incomplete: they were deliberately **deleted as dead code** by `f0090412`, not renamed or lost. Replacement paths are in that commit message. |
| "`Session` is reduced but still roughly 1,440 lines" | `wc -l src/easycat/session/_session.py` is **2,301**. The `<500` target was retracted by WS3's own AC3.10a. |
| `stages/` "is still mostly journaling wrapper code, at 1,573 lines" | `src/easycat/stages/*.py` totals **2,853** lines. |
| The old root `agent_runner.py` and `agents/` are gone | Still true. `easycat.integrations.agents._agent_runner.AgentRunner` remains active. |
| `easycat inspect`, `replay`, `validate`, and `python -m easycat` exist | Still true. |
