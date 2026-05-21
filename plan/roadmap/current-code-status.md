# Current Code Status

Status: current snapshot.

Snapshot date: 2026-05-21.

This is a static inspection snapshot used to keep the planning folder aligned
with the codebase. No tests were run for this document. Counts below exclude
`__pycache__` files.

## Inventory

- `src/easycat/` contains 148 Python files.
- `tests/` contains 152 `test_*.py` files.
- `docs/teaching/` now contains shipped chapters `00` through `15`.
- CI exists in `.github/workflows/ci.yml` with lint, local tests, socket
  integration tests, and manual live-provider tests. Local and socket jobs run
  on Python 3.12 and 3.14.
- CLI support includes `init`, `doctor`, `explain`, `bundles list/show`, and
  `inspect`; `python -m easycat` is wired through `src/easycat/__main__.py`.

## Implemented Or Mostly Implemented

- Runtime journal, artifact store, replay, debug bundle export, and debugger
  server exist under `src/easycat/runtime/`, `src/easycat/debug/`, and
  `src/easycat/debugger/`.
- The old legacy observability targets from the workstream plans are absent
  from `src/` and `tests/`: `EventTraceLogger`, `Tracer`, `SpanManager`,
  `InMemoryMetrics`, `src/easycat/agent_runner.py`, and `src/easycat/agents/`.
  `easycat.integrations.agents._agent_runner.AgentRunner` is still active and
  should not be confused with the removed legacy root module.
- Agent integrations now include OpenAI Agents, PydanticAI, generic workflow,
  Remote Responses API, LangChain, LangGraph, and Llama Agents under
  `src/easycat/integrations/agents/`.
- Session decomposition has landed substantially. `Session` still exists as
  the orchestrator, but collaborators now include `AudioRouter`,
  `STTCommitter`, `TTSScheduler`, `CancelOrchestrator`, `TurnRunner`, and
  `SessionJournalSink`.
- The exact WS3 class names `InterruptionController` and
  `VoiceDeliveryLedger` are not present as source files. Current interruption
  and delivered-text behavior is split across `CancelOrchestrator`,
  `session/interruption.py`, `TurnContext`, and `TurnRunner`.
- Stage wrappers exist for audio, VAD, STT, turn, agent, TTS, and transport.
  There is no current `src/easycat/stages/telephony.py` source file.
- `RuntimeScope` exists and is used by `Session`, but some lower-level
  collaborators still call `asyncio.create_task()` directly.
- Provider support includes OpenAI, Deepgram, ElevenLabs, and Cartesia for
  STT/TTS. Shared provider helpers, a `ProviderCatalog`, and a shared
  WebSocket STT base now exist.
- The E2E debug-first plans are backed by concrete tests under `tests/e2e/`.
- The WebRTC peer-replacement queue issue called out in older cleanup notes
  appears fixed in `_handle_offer_locked`: it drains the existing queue rather
  than replacing the object that `receive_audio()` may be awaiting.
- VAD/noise-reduction backend typo validation and echo-cancellation fallback
  policy are implemented and tested.
- README provider drift called out in the April cleanup note appears fixed:
  Cartesia is listed, TEN VAD is described as non-permissive, and the
  quickstart extra says it does not include TEN VAD.

## Still Active Gaps

- There is no `easycat validate` command and no `scripts/validate.py`.
  Validation remains the cleanest active planning backlog.
- There is no `easycat replay` CLI wrapper, although `ReplayRunner` and bundle
  loading/export primitives exist.
- Pytest markers are registered only for `integration_local`,
  `integration_socket`, `integration_live`, and `slow`; strict marker config
  and validation-specific markers are not yet present.
- CI does not emit EasyCat validation JSON/JUnit artifacts and still runs the
  socket matrix on both Python 3.12 and 3.14.
- `Session` is reduced from the older cleanup note but still large at roughly
  1,773 lines.
- `src/easycat/__init__.py` is smaller than the older cleanup note but still a
  broad public surface at roughly 249 lines and 75 lazy top-level exports.
- A root `LICENSE`, richer project metadata, wheel/sdist CI smoke tests, and
  package contents denylist remain active release-bar work.
- Connection-policy hardening, EventBus subscription tokens/dispatch policies,
  richer provider capability reports, and a typed TTS input policy remain
  cleanup backlog items.
- Full redaction policy, OTel/cost exports, and validation artifacts remain
  planned rather than implemented.
- Telephony-native TTS output is not fully implemented. Provider
  output-format plumbing exists and `config.py` has a hook for transport
  preferences, but no current transport advertises `preferred_tts_output_format`;
  Twilio still sends by converting PCM16 to mulaw at the transport boundary.

## Planning Implications

- Treat [../workstreams/](../workstreams/README.md) mostly as historical
  acceptance records. Their checked boxes are implementation history, not
  authoritative source truth. Re-open individual items only after checking
  current code.
- Treat [../session-decomposition/](../session-decomposition/README.md) as
  partially implemented cleanup guidance. The remaining target is shrinking
  and clarifying `Session`, not starting decomposition from scratch.
- Treat [combined-cleanup-tasks.md](combined-cleanup-tasks.md) as a backlog
  that needs triage before execution because several April findings are now
  done.
- Treat [../validation/](../validation/README.md) as the active plan with the
  highest signal-to-staleness ratio.
