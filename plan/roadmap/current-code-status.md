# Current Code Status

Status: current snapshot.

Snapshot date: 2026-06-06.

This is a static inspection snapshot used to keep the planning folder aligned
with the codebase. Counts below come from tracked files and exclude
`__pycache__` files.

## Inventory

- `src/easycat/` contains 171 tracked Python files.
- `tests/` contains 187 tracked `test_*.py` files.
- `docs/teaching/` now contains shipped chapters `00` through `15`.
- CI exists in `.github/workflows/ci.yml` with lint, typecheck, quick
  validation, coverage, socket validation, build smoke, and manual
  live-provider tests. Quick validation runs on Python 3.11, 3.12, and 3.14;
  socket validation runs on Python 3.12.
- CLI support includes `init`, `doctor`, `explain`, `bundles list/show`,
  `inspect`, and `replay`; `easycat validate` exposes `quick`, `socket`,
  `stress`, `contracts`, `latency`, `live`, `release`, and `report`;
  `python -m easycat` is wired through `src/easycat/__main__.py`.

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
  `SessionJournalSink`, with `SessionDebugBackends` owning debug backend
  finalization and post-stop preservation.
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
- Validation has landed as a public CLI and reusable runner:
  `scripts/validate.py` is a compatibility shim, validation reports live in
  `easycat.validation.report`, and CI/nightly/release workflows upload
  validation artifacts.
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

- Validation still has backlog around deeper protocol cassette coverage and
  browser-automated WebRTC validation. The dedicated `easycat validate release`
  wrapper now exists and aggregates installed-wheel release gates; socket
  validation now exposes a first-class optional WebRTC browser stats artifact
  path for `RTCPeerConnection.getStats()` snapshots.
- `Session` is reduced from the older cleanup note but still large at roughly
  1,356 lines.
- `src/easycat/__init__.py` is smaller than the older cleanup note but still a
  broad public surface at 280 lines and 85 lazy top-level exports. The surface
  is now pinned by a golden snapshot and documented in
  `docs/public-api.md`.
- A root `LICENSE` remains active release-bar work. Project metadata now
  includes author, keywords, classifiers, and project URLs, and wheel packaging
  tests guard against cache/workspace artifacts leaking into release wheels.
  CI now runs `uv build`, and release validation exercises an installed
  package through the public CLI.
- Broader connection-policy hardening and deeper live/cassette provider
  validation remain cleanup backlog items. Provider WebSocket reconnect policy
  now rejects unsafe retry/backoff values before they can create busy loops or
  confusing wait behavior. TTS now has a typed `TTSInputPolicy` surface with
  legacy `supports_ssml` compatibility, and provider capability reports
  serialize it while distinguishing SSML input support from native
  marker/alignment event support. EventBus subscription tokens, handler
  failure/slow-callback accounting, and configurable handler-error policy now
  exist for the current inline dispatch model.
- Full redaction policy and OTel/cost exports remain planned rather than
  implemented.
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
- Treat [../validation/](../validation/README.md) as the active validation
  status and backlog; do not use older cleanup notes for validation current
  state without re-checking the code and workflows.
