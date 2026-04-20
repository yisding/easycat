# Chapter 13 — Swap Providers AND Transports

> The same `Session`, run with **two orthogonal axes of choice**:
> the providers (STT/agent/TTS) *and* the transport (Local /
> WebRTC / Twilio). With evals from chapter 12 in hand, every
> swap is a measured decision, not a guess.

## Prerequisites

- Chapters 0-12. Especially chapter 12 — the eval surface from
  there is what makes the swaps comparable.
- API keys for at least two of: OpenAI, Deepgram, ElevenLabs.
- A Twilio account (optional, for the phone preset). The chapter
  is fully runnable without it.

## Learning objectives

1. Configure `EasyCatConfig` and `SessionConfig` for arbitrary
   provider combinations.
2. Swap the **transport** (Local → WebRTC → Twilio) without
   changing the agent or any provider, and feel the Protocol
   abstraction earn its keep on a second axis.
3. Understand why some providers need an `EventBus` injected at
   construction (Deepgram, ElevenLabs) and others don't (OpenAI).
4. Pick a provider × transport combination for a given constraint
   (latency, cost, quality, offline, telephony) and defend the
   tradeoff using chapter-12 eval numbers.

## What you build

`docs/teaching/13-swap-providers-and-transports/main.py`:

- Driver script that accepts a preset name and runs the same
  utterance through the selected combination.
- **Six presets**, organized as a 2×3 grid (provider mix ×
  transport):

| Provider mix | Local | WebRTC | Twilio (PSTN) |
|---|---|---|---|
| `openai-stack` | ✓ | ✓ | ✓ |
| `deepgram-eleven` | ✓ | ✓ | ✓ |

(The `local-offline` and `pydanticai-mixed` mixes are left as
exercises rather than core presets, to keep the chapter focused.)

- Each preset dumps its own `RunBundle`, all with matching
  timestamps so `latency_budget.py` from chapter 12 can diff them
  directly.

## Narrative arc

1. **Re-read `easycat.providers`.** With twelve chapters of
   context, the Protocols now *feel* like they're earning their
   keep.
2. **Axis 1, swap providers (familiar territory).**
   - `openai-stack` on Local — the baseline, measured with
     chapter 12's `latency_budget.py`.
   - `deepgram-eleven` on Local — same shape, different latency
     distribution. Show the journal diff.
3. **Axis 2, swap transport (the new payoff).**
   - `openai-stack` on **WebRTC** — same agent, browser audio
     instead of OS audio. Walk through `WebRTCTransport`. Mention
     that WebRTC preserves UDP end-to-end, which buys you
     `NetEQ`-style jitter buffering for free.
   - `openai-stack` on **Twilio** — same agent, *phone audio*
     instead of browser audio. Walk through `TwilioTransport` /
     `TwilioConnectionTransport`. Mu-law 8 kHz only; resampling
     happens for free in the transport. DTMF (touch tones) is a
     real input modality here — show how `DTMFAggregated` events
     flow through the same EventBus.
4. **The 2×3 matrix.** Run all six combinations on the same
   prompt. Print the latency, cost-per-turn, and barge-in F1
   for each (using chapter 12 scripts). The shape of the data
   is the lesson: provider choice swings *latency*; transport
   choice swings *jitter and codec quality*.
5. **Why some providers need EventBus.** Walk through
   `easycat.config.create_session` —
   `create_stt_provider_from_config` injects the EventBus for
   Deepgram, ElevenLabs, OpenAIRealtime, and Cartesia; not for
   the non-realtime OpenAI provider. This is *not* how `STTEvent`
   / `TTSEvent` get mapped to EasyCat-level events — those flow
   out of every provider's async iterator (`STTBase.events()` /
   the TTS equivalent) regardless of whether an EventBus was
   injected. The real reason is side-channel telemetry: these
   providers wrap a long-lived WebSocket via
   `ReconnectingWebSocket`, which emits `ReconnectAttempt` /
   `ReconnectSuccess` / `ReconnectFailure` onto the bus so the
   session journal (and any external listener) can see the retry
   timeline. OpenAI's HTTP-based STT/TTS don't have that failure
   mode, so they don't need the bus.
6. **A decision matrix.** Latency / cost / offline / quality /
   telephony — pick any three. Concrete table populated from the
   six bundles' measured numbers.

## Key concepts

- `easycat.config.EasyCatConfig` (simple, auto-wires) vs
  `SessionConfig` (explicit providers)
- `easycat.create_session()` factory wiring
- `easycat.providers.Transport` Protocol — the second axis of
  Protocol payoff
- Transports: `LocalTransport`, `WebRTCTransport`,
  `TwilioTransport` / `TwilioConnectionTransport`,
  `WebSocketTransport`
- `EventBus` injection — why some providers need it and others
  don't
- Telephony specifics: mu-law 8 kHz, DTMF (touch tones), the
  `DTMFAggregator` and `VoicemailDetector` in
  `easycat.telephony`
- WebRTC vs WebSocket: UDP-preserved-end-to-end vs
  TCP-bottlenecked; jitter buffer concept

## Exercises

1. Add an additional preset of your own (e.g.,
   `pydanticai-mixed` or `local-offline`) and document the
   tradeoff using chapter 12's `latency_budget.py`.
2. Measure first-audio latency across all six presets on the same
   recording. Is the ranking stable across short vs long prompts?
3. The base class `_ServerTransportBase` has a `WebSocketTransport`
   subclass that isn't in the 2×3 preset matrix. Wire an additional
   **browser** preset that uses `WebSocketTransport` and serve it
   from `examples/ws_server.py`. What is the minimum diff to swap
   from your laptop's mic to a browser tab? Compare against the
   existing WebRTC preset: same browser endpoint, different
   transport protocol — where does the latency differ? (Note:
   `_ServerTransportBase` is a shared plumbing base class, not a
   `typing.Protocol` — the Protocol abstraction earns its keep at
   the `Transport` surface that both the server-backed transports
   and `LocalTransport` implement.)
4. Add a tool from chapter 7 that calls
   `SendDTMFAction`. Run on the Twilio preset. What does the
   journal record? What does the user hear?

## Journal highlights

- Identical `stage.*` record schemas across all six presets — the
  proof the Protocol abstraction holds on both axes
- Distinct latency distributions per (provider mix, transport)
- Provider-scoped events present only in Deepgram / ElevenLabs
  presets — cross-reference `events.py` to see the event mapping
- Transport-specific events: `DTMFAggregated`,
  `VoicemailDetected` only on Twilio. WebRTC has no corresponding
  transport-specific EventBus or journal events today — ICE
  servers are config-only (`ICEServer`, `RTCConfiguration`), and
  the chapter's WebRTC preset should rely on the shared
  `stage.*` records plus the aiortc peer-connection logs for its
  comparison, not promise ICE journal artifacts that don't exist.

## Files created

- `docs/teaching/13-swap-providers-and-transports/main.py`
- `docs/teaching/13-swap-providers-and-transports/README.md`
- `docs/teaching/13-swap-providers-and-transports/presets/openai_local.py`
- `docs/teaching/13-swap-providers-and-transports/presets/openai_webrtc.py`
- `docs/teaching/13-swap-providers-and-transports/presets/openai_twilio.py`
- `docs/teaching/13-swap-providers-and-transports/presets/deepgram_eleven_local.py`
- `docs/teaching/13-swap-providers-and-transports/presets/deepgram_eleven_webrtc.py`
- `docs/teaching/13-swap-providers-and-transports/presets/deepgram_eleven_twilio.py`

## Success criteria

- The reader has run at least three different combinations on the
  same input — at minimum one provider swap *and* one transport
  swap.
- The reader can articulate when Protocol-over-inheritance earns
  its keep and when it doesn't, with two concrete examples from
  the swaps they ran.
- The reader has read chapter-12 latency reports across the
  combinations and can defend a preset choice for a hypothetical
  use case ("a noisy retail-store kiosk", "a phone IVR", "an
  in-browser product demo").

## The ladder, complete

You now understand every stage in a production voice pipeline,
have written a minimal working version of each (chapters 0-9),
have seen what tools and observability turn it into operatable
software (chapters 7, 11, 12), and have swapped both halves of
the abstraction (chapter 13). The rest of EasyCat — telephony
deep-cuts (IVR, voicemail screening), MCP integration, LangGraph
bridges, advanced redaction, CLI/DX — is plugging another
provider into the same Protocols and reading the plan for it.
The map is no longer the territory; you have walked the
territory.

## Suggested next reading (post-ladder)

- `plan/peripheral-cli.md` — the CLI surface
- `plan/peripheral-deployment.md` — production hosting and ops
- `plan/peripheral-provider-ecosystem.md` — adding a new provider
- `plan/peripheral-langchain-langgraph-bridge.md` — richer agent
  bridges
- `plan/peripheral-eval-and-debugger-ui.md` — what chapter 12
  prototypes, productionised
- `plan/peripheral-redaction.md` — what to keep out of journals
- `plan/peripheral-observability-and-cost.md` — cost-per-turn and
  observability in production
