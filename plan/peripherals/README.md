# Peripheral Plans

Status: active backlog index with landed historical records.

These plans are valuable follow-ups but are not required to land the core
debug-first runtime redesign. Keep them separable unless a workstream
explicitly promotes part of the work.

Status from static inspection on 2026-06-06:

- Mostly landed: CLI scaffold/doctor/explain/bundles/inspect, Cartesia
  STT/TTS, LangChain/LangGraph bridges, debugger UI, `record_to`, provider
  README drift fixes, and Deepgram Flux parsing/plumbing.
- Partially landed: telephony-aware TTS output-format alignment including
  Twilio's 8 kHz PCM16 outbound preference, safe bundle defaults, testing
  helpers, Docker deployment docs, and provider capability reports.
- Still mostly planned: full redaction policy, cost/OTel export, forked replay,
  persona simulator/judge, and the remaining validation backlog.

| Plan | Status | Notes |
|---|---|---|
| [peripheral-cli.md](peripheral-cli.md) | Partially landed | `init`, `doctor`, `explain`, `bundles list/show/export`, `inspect`, `replay`, all planned scaffold templates, and template line budgets exist; raw export and full redaction-policy integration remain planned. |
| [peripheral-dx-onboarding.md](peripheral-dx-onboarding.md) | Partially landed | `run`, string-keyed providers, config presets, error codes, `record_to`, log-level/env renderer support, and canonical example line budgets exist; `EasyConfig` remains at 22 top-level fields and meets the ≤22 flattening target. |
| [onramp-zen-dx-plan.md](../archive/onramp-zen-dx-plan.md) | Mostly landed implementation record | Preserves the verifier-backed DX onramp decisions that shaped the canonical hello-world, error-teaching, lifecycle, and scaffold signposting work; keep here instead of a separate top-level DX tree. |
| [peripheral-redaction.md](peripheral-redaction.md) | Mostly planned | Safe default snapshots exist; full `RedactionPolicy` and export policies remain planned. |
| [peripheral-observability-and-cost.md](../archive/peripheral-observability-and-cost.md) | Mostly planned | Startup `warmup()` hook execution and turn-level latency *reporting* (`turn_total_latency_ms` / `text_turn_latency_ms` journal metrics) exist. The runtime cost-monitoring and latency-budget features (debugger `/api/cost` endpoint, `max_session_cost_usd`, `cost_budget_*` records, stage-record latency-budget tags) were removed as undercooked and duplicative with the journal. OTel export and provider-native warmup coverage remain planned. |
| [peripheral-eval-and-debugger-ui.md](peripheral-eval-and-debugger-ui.md) | Partially landed | Debugger UI, replay endpoint, bundle pytest helpers, and checkpoint ids exist; simulator/judge and forked replay remain planned. |
| [peripheral-deployment.md](peripheral-deployment.md) | Partially landed | Docker docs exist under `docs/deployment/`; broader platform runbooks remain planned. |
| [peripheral-provider-ecosystem.md](peripheral-provider-ecosystem.md) | Partially landed | Deepgram Flux and Smart Turn v3.2 support exist; backchannel filtering and some capability reports remain planned. |
| [peripheral-cartesia-provider.md](../archive/peripheral-cartesia-provider.md) | Landed | Cartesia STT/TTS providers, factory registration, CLI env handling, and tests exist. |
| [peripheral-telephony-tts-output.md](peripheral-telephony-tts-output.md) | Partially landed | TTS configs can align to transport-preferred outbound audio; Twilio advertises 8 kHz PCM16 and still performs the final PCM16-to-mulaw encode at the transport boundary. |
| [peripheral-langchain-langgraph-bridge.md](../archive/peripheral-langchain-langgraph-bridge.md) | Landed | LangChain/LangGraph bridges, event translator, auto-adapt dispatch, and tests exist. |
