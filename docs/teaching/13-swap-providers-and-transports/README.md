# Chapter 13 — Swap Providers AND Transports

<!-- BEGIN auto:navigation -->
**Progress: 14 of 16** · [← Chapter 12 — Evals + the Latency Budget](../12-evals-and-latency/) · [Ladder index](../) · [Progress worksheet](../PROGRESS.md) · [Exercises](./EXERCISES.md) · [Chapter 14 — Bring your own agent →](../14-bring-your-own-agent/)
<!-- END auto:navigation -->

> The same `Session`, run with **two orthogonal axes of choice**:
> the providers (STT/agent/TTS) *and* the transport (Local /
> WebRTC / Twilio). With eval numbers from chapter 12 in hand,
> every swap is a measured decision.

<!-- BEGIN auto:spaced-retrieval -->
## Recall before reading

> **Following the ladder? Spaced retrieval — Chapter 11 — The Journal as Mental Model**
>
> Close earlier chapters and answer from memory before reading further. If this
> chapter is your starting point, skip this block.
>
> **Answer from memory:**
>
> How can marginal query counts distinguish an empty filter intersection from a misspelled
> turn?
>
> After recording your answer, explain one way `journal query coverage` changes how you reason
> about `provider × transport matrix`. Keep the first answer visible.
>
> **Check only after answering:**
>
> ```bash
> uv run python docs/teaching/11-journal/query_coverage_probe.py
> ```
>
> Cite one observed field, measurement, or behavior; repair only the part your
> evidence disproved.
<!-- END auto:spaced-retrieval -->

<!-- BEGIN auto:offline-checkpoint -->
> **Hardware-free checkpoint:** prove `provider × transport matrix` without a microphone,
> speakers, or provider credentials:
>
> **Predict first:** How many cells result from two provider mixes × three transports, and which
> values stay fixed along each axis?
>
> ```bash
> uv run python docs/teaching/13-swap-providers-and-transports/matrix_probe.py
> ```
>
> **Evidence to find:** two provider mixes cross three transport configs into six cells without
> changing axes.
>
> **Explain the result:** Pick one row and column; name what changes on each axis and what must
> remain constant.
>
> [See all 16 checkpoints](../#hardware-free-checkpoint-spine).
<!-- END auto:offline-checkpoint -->

This is the first chapter on the production wiring
(`create_session()` + `EasyConfig`). For the app-builder version of
the same graduation — lifecycle, event subscriptions, text turns, and
debug bundles — see the
[from-EasyConfig-to-Session guide](../../from-easyconfig-to-session.md).

## Prerequisites

- [Chapters 0-12.](../)
- `uv sync --extra quickstart --group dev` always.
- Add `--extra deepgram --extra elevenlabs` for the `deepgram-eleven`
  provider mix.
- `--extra webrtc` for the WebRTC transport.
- `--extra telephony` for the Twilio transport.
- `OPENAI_API_KEY` always; `DEEPGRAM_API_KEY` + `ELEVENLABS_API_KEY`
  for the `deepgram-eleven` mix.
- Running this chapter makes live provider calls that may incur charges.
  Review your provider billing and usage limits first.
- Provider-backed scripts may send audio, transcripts, or prompts to configured
  services. Use non-sensitive test content and review provider data-handling
  policies first.
- After setting provider keys, run `uv run easycat doctor` from the repo root; if keys live in `.env`, run `uv run easycat doctor --env-file .env`. Use `uv run easycat doctor --env-file .env --json` for parseable checks.
- If keys live in `.env`, also add `--env-file .env` after `uv run`
  in the chapter command you run.

> **Minimum to skip the ladder:** chapter 6 (you need a streaming
> pipeline to swap) plus chapter 12 (so you can measure the
> tradeoffs). You can skip the operate-movement (chs 10-11) if
> you only want to see the Protocol payoff.

## Diff from chapter 12

- **Added:** `create_session()` + `EasyConfig` end-to-end (the
  first chapter that uses the production wiring); WebRTC and
  Twilio transport options; `--provider-mix
  {openai,deepgram-eleven}` and `--transport {local,webrtc,twilio}`
  CLI matrix; `matrix_probe.py` for all six provider-free config cells;
  `event_bus_probe.py` for the provider observability contract; bundle-shape
  note explaining the teaching → production journal-shape transition.
- **Removed:** every hand-rolled coroutine from chapters 6-10.
  `Session` orchestrates the pipeline now.

## The 2×3 matrix

|                  | Local (mic) | WebRTC (browser) | Twilio (phone) |
|------------------|:-----------:|:----------------:|:--------------:|
| **`openai`**         | ✓ runnable  | needs browser    | needs a call   |
| **`deepgram-eleven`**| ✓ runnable  | needs browser    | needs a call   |

Materialize all six configuration cells without keys, clients, or provider
calls:

```bash
uv run python docs/teaching/13-swap-providers-and-transports/matrix_probe.py
```

The output keeps the provider mapping constant across three transports and
the transport config constant across two provider mixes—the Protocol payoff
in data rather than a live demo.

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

The script prints the exact follow-up commands for that path. You
can also replace `PATH` below yourself:

```bash
uv run easycat latency PATH
uv run easycat latency PATH --json
```

> **Bundle shape note.** Ch 13 uses `create_session()`, so its
> bundles carry the **production** journal shape (`stage_start` /
> `stage_complete`, plus turn-scoped `stt_final`, `agent_delta`, and
> `tts_frame` records). The `easycat latency` command reads that shape
> directly and reports per-turn critical paths plus p50/p90/p95/p99;
> no translator is required. Chapter 12's small `evals.py` still keys
> on its denser *teaching* fixture shape, so use it for that chapter's
> WER and barge-in exercise rather than as the production-bundle reader.

`easycat latency` ends at the first synthesized TTS byte on the
server. That is the right provider-pipeline comparison, but it does
**not** include browser/PSTN delivery, jitter, device buffering, or
speaker playback. Pair it with WebRTC client stats or telephony
provider metrics before claiming one transport is faster end to end.

## The production session boundary

This chapter replaces the manual resource stacks with the public
session scope:

```python
async with session:
    await wait_for_shutdown_signal(session)

export_debug_bundle(session, path, overwrite=True)
```

Entering starts the session. On a normal SIGINT/SIGTERM path,
`wait_for_shutdown_signal` first calls graceful `session.stop()`;
context exit then calls `stop(force=True)`, which is an idempotent
no-op because the session is already closed. If an outer coroutine
cancels the block instead, context exit supplies the force-cancel path
that the signal helper cannot.

The export intentionally happens after the block. A clean stop closes
providers, transport, and writable backends but preserves a read-only
postmortem journal view, so bundle inspection does not require keeping
runtime resources alive.

Run the provider-free ordering probe:

```bash
uv run python \
  docs/teaching/13-swap-providers-and-transports/session_scope_probe.py
```

The two traces make the distinction observable. On the graceful path,
the signal helper's `stop(force=False)` closes the session and context
exit's `stop(force=True)` reports an idempotent no-op. On the cancelled
path, the graceful call never happens, so context exit's force-stop does
the cleanup. Both traces export only after the session scope has exited
and before the caller-owned client closes.

The probe also previews Chapter 14's second boundary: a dependency
created by your custom workflow remains caller-owned and gets its own
outer scope. In a real application, either complete postmortem work
before re-raising cancellation or shield/bound that work according to
the owner's shutdown policy.

## Architecture

```
  ┌─────────────────────┐        ┌─────────┐
  │  EasyConfig(...)    │──────► │ Session │ ──► the agent never
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
EasyConfig(
    agent=agent,  # ← same across every cell
    transport=LocalTransportConfig(),  # ← axis 2 switch
    stt="deepgram/nova-2",  # ← axis 1 switch
    tts="elevenlabs",  # ← axis 1 switch
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
| First-audio latency         | Provider mix — compare `easycat latency` on repeated, matched turns |
| Jitter + packet loss        | Transport — inspect WebRTC's selected ICE path and client stats; TURN can relay media over UDP or TCP |
| Codec quality               | Transport — Local uses 24 kHz PCM; WebRTC uses 48 kHz media frames with Opus around a 16 kHz pipeline; Twilio uses μ-law at 8 kHz on the wire |
| Cost per turn               | Provider mix — usually the dominant cost driver |
| Offline / on-device         | Provider mix — use a custom local/self-hosted provider; the bundled STT/TTS providers are hosted |
| Reach a regular phone       | Transport — Twilio only |

Measure the production bundles with `easycat latency`; choose with
those numbers. Treat the transport claims as hypotheses until you
also have client/PSTN delivery measurements.

## Why some providers need an `EventBus`

Inspect `create_stt_provider_from_config` and
`create_tts_provider_from_config` in the two provider factories. They do not
keep a hand-written list of WebSocket providers. Instead, a provider config
opts into the session `EventBus` by declaring an `event_bus` dataclass field;
the factory detects that field and injects the bus when it is still `None`.

Run the provider-free catalog probe to see that structural contract:

```bash
uv run python \
    docs/teaching/13-swap-providers-and-transports/event_bus_probe.py
```

OpenAI's batch STT and HTTP TTS configs both print `yes`, so the bus is not
synonymous with WebSockets. It carries **provider observability**:

- WebSocket providers use it for provider errors and reconnect lifecycle
  (`ReconnectAttempt`, `ReconnectSuccess`, `ReconnectFailure`).
- HTTP STT/TTS providers use it for provider `Error` events, but cannot emit
  reconnect lifecycle because they have no persistent socket.
- `STTEvent` and `TTSEvent` data still flow from provider async iterators; the
  session bus is not the audio/transcript stream.

When a journal shows a mysterious latency spike, reconnect records can explain
it—the same pattern as chapter 11's bug 2. When an HTTP TTS request fails, its
provider `Error` record supplies a different but equally important trail.

## A decision matrix

Pick any three columns, then replace these starting hypotheses with
numbers from your own environment:

| Use case                      | Latency | Quality | Reach | Cost | Suggested cell |
|-------------------------------|:-------:|:-------:|:-----:|:----:|----------------|
| In-browser product demo       |   ⭐⭐⭐  |  ⭐⭐   |  ⭐⭐  |  —   | `openai` on WebRTC |
| Phone IVR                     |   ⭐    |  ⭐    |  ⭐⭐⭐ |  ⭐   | `openai` on Twilio |
| Retail kiosk (noisy)          |   ⭐⭐   |  ⭐⭐⭐  |  ⭐   |  ⭐   | `deepgram-eleven` on Local |
| Multilingual hotline          |   ⭐    |  ⭐⭐⭐  |  ⭐⭐⭐ |  ⭐⭐  | `deepgram-eleven` on Twilio |
| Offline embedded device       |   ⭐⭐⭐  |  ⭐⭐   |  ⭐   |  ⭐⭐⭐ | (future: local models) |

Cost is not a measured axis in this chapter — it's an annotation
from provider pricing pages. Chapter 12 deliberately stops short
of cost.

## Try breaking it

1. Add a `--provider-mix cartesia` preset (both STT and TTS via
   Cartesia's WebSocket API). What's the minimum diff from
   `deepgram-eleven`?
2. Run the expanded nine-cell matrix on the same short prompt.
   Which cell has the tightest server-side P95/P50 ratio in
   `easycat latency`? What client or provider evidence would you
   need to rank transports?
3. Wire `SendDTMFAction` from chapter 7 into the agent (the user
   asks for "press 1 to continue"). What does the journal show
   on the Twilio preset? What does a user on the phone hear?

<!-- BEGIN auto:practice-handoff -->
## Practice and self-check

Work through [the chapter exercises](./EXERCISES.md), then try their closing
self-check from memory. If an answer is weak, rerun the hardware-free
checkpoint or revisit the section that owns the gap.
<!-- END auto:practice-handoff -->

## What's next

You have swapped both halves of the STT / agent / TTS / transport
abstraction and measured the result. Two chapters remain:

- [Chapter 14 — Bring your own agent](../14-bring-your-own-agent/)
  drops the OpenAI Agents SDK itself and shows the bridge layer
  that sits under every `agent=` value. Also session actions and
  the pronunciation pipeline.
- [Chapter 15 — Operate in production](../15-operate-in-production/)
  takes the single-session demo you've been running since chapter
  0 and shows `SessionManager` / lifecycle / debugger UI / CLI —
  what it takes to run N of these at once.
