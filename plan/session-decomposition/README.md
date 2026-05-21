# Session Decomposition

Status: historical record index with residual cleanup guidance.

Historical phase plans for splitting `Session` into smaller runtime
collaborators. Use the overview first, then the phase plans when you need
implementation rationale or follow-up cleanup context.

Status from static inspection on 2026-05-21: substantial decomposition has
landed. The current code has `AudioRouter`, `STTCommitter`, `TTSScheduler`,
`CancelOrchestrator`, `TurnRunner`, and `SessionJournalSink`; `Session`
remains the public lifecycle owner at roughly 1,773 lines, so this folder is
still useful for cleanup work that shrinks or clarifies residual ownership.

Do not treat the unchecked phase task lists as the current backlog without
checking `src/easycat/session/` first. They are retained as design records.

| Doc | Current use |
|---|---|
| [session-decomp-overview.md](session-decomp-overview.md) | Current summary plus as-landed notes. |
| [session-decomp-phase-0-turn-context.md](session-decomp-phase-0-turn-context.md) | Historical TurnContext and RuntimeScope extraction checklist. |
| [session-decomp-phase-1-stt-committer.md](session-decomp-phase-1-stt-committer.md) | Historical STTCommitter extraction checklist. |
| [session-decomp-phase-2-audio-router.md](session-decomp-phase-2-audio-router.md) | Historical AudioRouter extraction checklist. |
| [session-decomp-phase-3-tts-scheduler.md](session-decomp-phase-3-tts-scheduler.md) | Historical TTSScheduler extraction checklist. |
| [session-decomp-phase-4-cancel-orchestrator.md](session-decomp-phase-4-cancel-orchestrator.md) | Historical CancelOrchestrator extraction checklist. |
| [session-decomp-phase-5-turn-runner.md](session-decomp-phase-5-turn-runner.md) | Historical TurnRunner extraction checklist and as-landed variance notes. |
