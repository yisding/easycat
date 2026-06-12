# Session Decomposition

Status: historical record index with residual cleanup guidance.

Historical notes for splitting `Session` into smaller runtime collaborators.
Use the overview for implementation rationale and follow-up cleanup context.

Status from the 2026-06-07 current-code snapshot: substantial decomposition
has landed. The current code has `AudioRouter`, `STTCommitter`,
`TTSScheduler`, `CancelOrchestrator`, `TurnRunner`, and `SessionJournalSink`;
`Session` remains the public lifecycle owner at roughly 1,440 lines, so this
folder is still useful for cleanup work that shrinks or clarifies residual
ownership.

The old per-phase checklists have been removed; they were unchecked historical
task lists and had drifted behind the current `src/easycat/session/` package.

| Doc | Current use |
|---|---|
| [session-decomp-overview.md](session-decomp-overview.md) | Current summary plus as-landed notes. |
