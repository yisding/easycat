# Chapter 13 — Swap Providers AND Transports

> The same `Session`, run with **two orthogonal axes of choice**:
> the providers (STT/agent/TTS) *and* the transport (Local /
> WebRTC / Twilio). With eval numbers from chapter 12 in hand,
> every swap is a measured decision.

## Prerequisites

- [Chapters 0-12.](../)
- `uv sync --extra quickstart --group dev` always.
- `--extra webrtc` for the WebRTC transport.
- `--extra telephony` for the Twilio transport.
- `OPENAI_API_KEY` always; `DEEPGRAM_API_KEY` + `ELEVENLABS_API_KEY`
  for the `deepgram-eleven` mix.

## The 2×3 matrix

|                  | Local (mic) | WebRTC (browser) | Twilio (phone) |
|------------------|:-----------:|:----------------:|:--------------:|
| **`openai`**         | ✓ runnable  | needs browser    | needs a call   |
| **`deepgram-eleven`**| ✓ runnable  | needs browser    | needs a call   |

## Run two axes

```bash
# Axis 1 — provider swap (same transport)
uv run python docs/teaching/13-swap-providers-and-transports/main.py \
    --provider-mix openai --transport local

uv run python docs/teaching/13-swap-providers-and-transports/main.py \
    --provider-mix deepgram-eleven --transport local

# Axis 2 — transport swap (same providers)
uv run python docs/teaching/13-swap-providers-and-transports/main.py \
    --provider-mix openai --transport webrtc   # see examples/webrtc_server.py
uv run python docs/teaching/13-swap-providers-and-transports/main.py \
    --provider-mix openai --transport twilio   # see examples/twilio_app.py
```

Each run drops a bundle in `runs/ch13-<mix>-<transport>-*.bundle`.

> **Bundle shape note.** Ch 13 uses `create_session()`, so its
> bundles carry the **production** journal shape (`stage_start` +
> `stage_complete` pairs, per chapter 11's teaching-vs-production
> sidebar). Chapter 12's scripts key on the *teaching* shape
> (`stage.tts.execute`, `turn.gap`, `stt.final` with a `t_ms`
> field). To run ch 12's evals on a ch 13 bundle, you will need a
> small translator — pair each `stage_start` with its matching
> `stage_complete` by `op_id` and synthesise the composite records
> ch 12 expects. Writing that translator is a productive exercise
> and the natural first task of `peripheral-eval-and-debugger-ui.md`.

## Architecture

```
  ┌─────────────────────┐        ┌─────────┐
  │  EasyCatConfig(...) │──────► │ Session │ ──► the agent never
  │    stt=...          │        │ (same   │     knows which stt,
  │    tts=...          │        │  code   │     tts, or transport
  │    transport=...    │        │  every  │     is wired
  │    agent=...        │        │  cell)  │
  └─────────────────────┘        └─────────┘
            ▲
            │ the only thing that changes
     between cells is three config lines
```

## The one code change per axis

```python
EasyCatConfig(
    openai_api_key=...,
    agent=agent,                       # ← same across every cell
    transport=LocalTransportConfig(),  # ← axis 2 switch
    stt="deepgram/nova-2",             # ← axis 1 switch
    tts="elevenlabs",                  # ← axis 1 switch
)
```

Three lines of configuration define each of the six cells. The
`Agent`, the `Session` orchestration, the event bus, the journal,
the smart-turn classifier, the NR/AEC stages — none of that code
moves. That is the whole point of twelve chapters of Protocol
discipline.

## Why the matrix exists

Provider choice and transport choice optimise **different axes**:

| Axis you care about         | Choose this |
|-----------------------------|-------------|
| First-audio latency         | Provider mix — Deepgram STT cuts partial-latency by ~150 ms |
| Jitter + packet loss        | Transport — WebRTC preserves UDP end-to-end |
| Codec quality               | Transport — Local / WebRTC (24 kHz) vs Twilio (μ-law 8 kHz) |
| Cost per turn               | Provider mix — usually the dominant cost driver |
| Offline / on-device         | Provider mix — (future: Cartesia / local models) |
| Reach a regular phone       | Transport — Twilio only |

Measure with chapter 12's scripts; choose with those numbers.

## Why some providers need an `EventBus`

Inspect `src/easycat/stt/factory.py::create_stt_provider_from_config`
(wired from `easycat.config.create_session`). The WebSocket-based
providers (Deepgram, ElevenLabs, OpenAI Realtime, Cartesia)
receive an `EventBus` at construction. The
HTTP batch OpenAI provider does not. The bus isn't used for
`STTEvent` or `TTSEvent` — those flow out of every provider's
async iterator regardless. It's for **reconnect telemetry**: the
WebSocket providers wrap `ReconnectingWebSocket`, which emits
`ReconnectAttempt` / `ReconnectSuccess` / `ReconnectFailure`
events whenever the long-lived socket drops. HTTP providers have
no socket to drop, so no telemetry to emit.

When your journal shows a mysterious latency spike, those three
events are the record that usually explains it — the same pattern
you saw in chapter 11's bug 2.

## A decision matrix

Pick any three columns, defend with numbers:

| Use case                      | Latency | Quality | Reach | Cost | Suggested cell |
|-------------------------------|:-------:|:-------:|:-----:|:----:|----------------|
| In-browser product demo       |   ⭐⭐⭐  |  ⭐⭐   |  ⭐⭐  |  —   | `openai` on WebRTC |
| Phone IVR                     |   ⭐    |  ⭐    |  ⭐⭐⭐ |  ⭐   | `openai` on Twilio |
| Retail kiosk (noisy)          |   ⭐⭐   |  ⭐⭐⭐  |  ⭐   |  ⭐   | `deepgram-eleven` on Local |
| Multilingual hotline          |   ⭐    |  ⭐⭐⭐  |  ⭐⭐⭐ |  ⭐⭐  | `deepgram-eleven` on Twilio |
| Offline embedded device       |   ⭐⭐⭐  |  ⭐⭐   |  ⭐   |  ⭐⭐⭐ | (future: local models) |

Cost is not a measured axis in this chapter — it's an annotation
from provider pricing pages. Chapter 12 deliberately stops short
of cost; the plan for `peripheral-observability-and-cost.md`
promotes it to a measured column.

## Try breaking it

1. Add a `--provider-mix cartesia` preset (both STT and TTS via
   Cartesia's WebSocket API). What's the minimum diff from
   `deepgram-eleven`?
2. Run all six cells on the same short prompt. Which cell has the
   tightest P95/P50 ratio in chapter 12's eval output? Why?
3. Wire `SendDTMFAction` from chapter 7 into the agent (the user
   asks for "press 1 to continue"). What does the journal show
   on the Twilio preset? What does a user on the phone hear?

## The ladder, complete

You have:

- Built each pipeline stage from scratch (chapters 0-9).
- Operated the pipeline with real signal hygiene, observability,
  and evaluation (chapters 10-12).
- Swapped both halves of the abstraction and measured the result
  (this chapter).

Every remaining EasyCat surface — telephony deep-cuts, MCP
integration, LangGraph bridges, advanced redaction, CLI/DX — is
plugging another provider into the same Protocols. The map is no
longer the territory; you have walked the territory.

## Suggested next reading

- `plan/peripheral-cli.md` — the CLI surface
- `plan/peripheral-deployment.md` — production hosting and ops
- `plan/peripheral-provider-ecosystem.md` — adding a new provider
- `plan/peripheral-langchain-langgraph-bridge.md` — richer agent bridges
- `plan/peripheral-eval-and-debugger-ui.md` — what chapter 12 prototypes, productionised
- `plan/peripheral-redaction.md` — what to keep out of journals
- `plan/peripheral-observability-and-cost.md` — cost-per-turn in production
