# Peripheral Plans

Status: active backlog index with landed historical records.

These plans are valuable follow-ups but are not required to land the core
debug-first runtime redesign. Keep them separable unless a workstream
explicitly promotes part of the work.

Status from static inspection on 2026-05-21:

- Mostly landed: CLI scaffold/doctor/explain/bundles/inspect, Cartesia
  STT/TTS, LangChain/LangGraph bridges, debugger UI, `record_to`, provider
  README drift fixes, and Deepgram Flux parsing/plumbing.
- Partially landed: telephony-aware TTS output-format alignment, safe bundle
  defaults, testing helpers, Docker deployment docs, and provider capability
  reports.
- Still mostly planned: full redaction policy, cost/OTel export, `easycat
  replay`, `easycat bundles export`, forked replay, persona simulator/judge,
  and validation command surface.

| Plan | Status | Notes |
|---|---|---|
| [peripheral-cli.md](peripheral-cli.md) | Partially landed | `init`, `doctor`, `explain`, `bundles list/show`, and `inspect` exist; replay/export remain planned. |
| [peripheral-dx-onboarding.md](peripheral-dx-onboarding.md) | Partially landed | `run`, string-keyed providers, config presets, error codes, `record_to`, and log-level env support exist; line-budget/config cleanup remains. |
| [peripheral-redaction.md](peripheral-redaction.md) | Mostly planned | Safe default snapshots exist; full `RedactionPolicy` and export policies remain planned. |
| [peripheral-observability-and-cost.md](peripheral-observability-and-cost.md) | Mostly planned | Debugger cost endpoint degrades to zero; real `CostRecord`, OTel export, and latency-budget objects remain planned. |
| [peripheral-eval-and-debugger-ui.md](peripheral-eval-and-debugger-ui.md) | Partially landed | Debugger UI, replay endpoint, bundle pytest helpers, and checkpoint ids exist; simulator/judge and forked replay remain planned. |
| [peripheral-deployment.md](peripheral-deployment.md) | Partially landed | Docker docs exist under `docs/deployment/`; broader platform runbooks remain planned. |
| [peripheral-provider-ecosystem.md](peripheral-provider-ecosystem.md) | Partially landed | Deepgram Flux and Smart Turn v3.2 support exist; backchannel filtering and some capability reports remain planned. |
| [peripheral-cartesia-provider.md](peripheral-cartesia-provider.md) | Landed | Cartesia STT/TTS providers, factory registration, CLI env handling, and tests exist. |
| [peripheral-telephony-tts-output.md](peripheral-telephony-tts-output.md) | Partially landed | TTS configs can align to transport audio format; Twilio still converts PCM16 to mulaw at the transport boundary. |
| [peripheral-langchain-langgraph-bridge.md](peripheral-langchain-langgraph-bridge.md) | Landed | LangChain/LangGraph bridges, event translator, auto-adapt dispatch, and tests exist. |
