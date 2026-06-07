# Workstreams

Status: historical record index.

Operational slices for the debug-first runtime redesign. These are ordered
roughly by dependency.

Current note: the 2026-06-07 snapshot in
[../roadmap/current-code-status.md](../roadmap/current-code-status.md) shows
many workstream outcomes have landed, but the detailed checklists are not
authoritative source truth. Use that snapshot before turning any item into new
work.

Known drift from the records:

- `InterruptionController`, `VoiceDeliveryLedger`, and
  `src/easycat/stages/telephony.py` are not present as current source files.
- The old root `src/easycat/agent_runner.py` and `src/easycat/agents/` are
  gone, but `easycat.integrations.agents._agent_runner.AgentRunner` remains
  active.
- `Session` is reduced but still roughly 1,433 lines, so the historical
  `<500` target was not met.
- `easycat inspect`, `easycat replay`, `easycat validate`, and
  `python -m easycat` exist.

1. [workstream-1-journal-foundation.md](workstream-1-journal-foundation.md)
2. [workstream-2a-agent-bridges.md](workstream-2a-agent-bridges.md)
3. [workstream-2b-interruption-and-mcp.md](workstream-2b-interruption-and-mcp.md)
4. [workstream-2c-remote-bridge.md](workstream-2c-remote-bridge.md)
5. [workstream-3-stage-refactor.md](workstream-3-stage-refactor.md)
6. [workstream-4-replay-and-bundle.md](workstream-4-replay-and-bundle.md)
7. [workstream-5-legacy-removal.md](workstream-5-legacy-removal.md)

Use these files as implementation rationale and acceptance history. Use the
roadmap status snapshot and the codebase for current planning.
