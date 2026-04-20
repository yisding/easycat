# Chapter 11 — Swap Providers

> The same `Session`, four different backend combinations. Feel
> the Protocol design earning its keep.

## Prerequisites

- Chapters 0-10
- API keys for at least two of: OpenAI, Deepgram, ElevenLabs

## Learning objectives

1. Configure `EasyCatConfig` and `SessionConfig` for arbitrary
   backend combinations.
2. Understand why some providers require an `EventBus` injected
   at construction (Deepgram, ElevenLabs) and others don't (OpenAI).
3. Select a provider mix for a given constraint — latency, cost,
   quality, offline — and defend the tradeoff.

## What you build

`docs/teaching/11-swap-providers/main.py`:

- Starts from a copy of `docs/teaching/09-noise-reduction/main.py`.
- Driver script that accepts a preset name and runs the same
  utterance through the selected backend combination.
- Four presets, each as its own file under
  `docs/teaching/11-swap-providers/presets/`:

| Preset | STT | Agent | TTS | NR / VAD |
|---|---|---|---|---|
| `openai-only` | OpenAI | OpenAI Agents | OpenAI | passthrough / Silero |
| `deepgram-eleven` | Deepgram | OpenAI Agents | ElevenLabs | passthrough / Silero |
| `pydanticai-mixed` | OpenAI | PydanticAI | OpenAI | passthrough / Silero |
| `local-offline` | local STT | local LLM | local TTS | passthrough / Silero |

- Each preset dumps its own `RunBundle`, all with matching
  timestamps so the reader can diff latencies directly.

## Narrative arc

1. **Re-read `src/easycat/providers.py`.** With ten chapters of
   context under their belt, Protocols now *feel* like they're
   earning their keep.
2. **Preset 1 — openai-only.** Familiar; this is chapter 10's
   baseline.
3. **Preset 2 — swap the endpoints.** Zero consumer-code changes.
   Dump the bundle — shape identical, latencies different. Lead
   with this to make the payoff concrete before going further.
4. **Preset 3 — agent bridge.** Walk through
   `src/easycat/integrations/agents/` and `BridgeAdapterShim`. Why
   bridges exist: different agent frameworks have different
   streaming shapes, and the shim adapts them to one surface.
5. **Preset 4 — offline as far as it goes.** Passthrough NR,
   Silero VAD, local STT and TTS. Quality is lower; the reader
   hears what commercial providers actually buy you.
6. **A decision matrix.** Latency / cost / offline / quality —
   pick any three. Include a concrete table filled from the four
   bundles' measured numbers.

## Key concepts

- `src/easycat/config.py::EasyCatConfig` (simple, auto-wiring) vs
  `SessionConfig` (explicit providers)
- `create_session()` factory wiring
- `src/easycat/events.py::EventBus` — why Deepgram and ElevenLabs
  providers need it (they emit provider-scoped events like
  `STTEvent`, `TTSEvent` that Session maps to EasyCat-level
  events) and OpenAI providers don't
- `src/easycat/integrations/agents/` bridges: `OpenAIAgentsBridge`,
  `PydanticAIBridge`, `GenericWorkflowBridge`,
  `RemoteResponsesAPIBridge`

## Exercises

1. Create a fifth preset of your own design and add it to
   `docs/teaching/11-swap-providers/presets/`. Measure and
   document the tradeoff.
2. Measure first-audio latency across all four presets on the same
   recording. Rank them. Are the rankings stable across different
   prompt types (short vs long, Q&A vs conversational)?
3. The Protocol `_ServerTransportBase` has a WebSocket subclass.
   Swap the transport too — move the bot from local audio to
   browser audio. What is the minimum diff needed?

## Journal highlights

- Identical `stage.*` record schemas across presets — proof the
  Protocol abstraction holds
- Distinct latency distributions per preset
- Provider-scoped events present only in Deepgram / ElevenLabs
  presets — cross-reference `events.py` to see the event mapping

## Files created

- `docs/teaching/11-swap-providers/main.py`
- `docs/teaching/11-swap-providers/README.md`
- `docs/teaching/11-swap-providers/presets/openai_only.py`
- `docs/teaching/11-swap-providers/presets/deepgram_eleven.py`
- `docs/teaching/11-swap-providers/presets/pydanticai_mixed.py`
- `docs/teaching/11-swap-providers/presets/local_offline.py`

## Success criteria

- The reader has run at least two different provider combinations
  on the same input.
- The reader can articulate when Protocol-over-inheritance earns
  its keep and when it doesn't — i.e., when the "cost" of the
  abstraction is worth the flexibility.

## The ladder, complete

You now understand every stage in a production voice pipeline,
have written a minimal working version of each, and have debugged
a planted bug by reading journal evidence alone. The rest of
EasyCat — telephony, MCP integration, LangGraph bridges, advanced
redaction, CLI/DX — is a matter of plugging in another provider
and reading the plan for it. The map is no longer the territory;
you have walked the territory.

## Suggested next reading (post-ladder)

- `plan/peripheral-cli.md` — the CLI surface
- `plan/peripheral-provider-ecosystem.md` — adding a new provider
- `plan/peripheral-langchain-langgraph-bridge.md` — richer agent
  bridges
- `plan/peripheral-eval-and-debugger-ui.md` — the replay/eval UI
  (deeply complementary to chapter 10)
