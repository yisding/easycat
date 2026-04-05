# Provider Ecosystem — Peripheral

> **This is a peripheral initiative.** It is not essential to the
> debug-first thesis in `essential-debug-first-runtime.md`. It is what
> keeps EasyCat's provider story competitive in 2026 and what makes the
> realtime mode usable in practice.
>
> **Sibling peripheral docs:**
>
> - `peripheral-dx-onboarding.md` — line budgets, CLI, templates,
>   helpers, error diagnostics
> - `peripheral-observability-and-cost.md` — OTel export, cost modeling,
>   latency budgets, warmup stage
> - `peripheral-eval-and-debugger-ui.md` — `easycat.testing`, Simulator +
>   Judge, forked replay, interactive debugger UI, dev waterfall
>
> **In scope (this file):** Deepgram Flux STT adapter (conversational
> STT that collapses VAD + STT + endpointing), Gemini 3.1 Flash Live
> bridge (second one-API-key realtime path alongside OpenAI Realtime),
> Smart Turn v3.1 promotion wrapping Pipecat's
> `LocalSmartTurnAnalyzerV3`, backchannel filter (ML-based
> false-interruption detection), cache-friendly realtime defaults
> (`retention_ratio=0.8`, `CacheBust` detection, never-rewrite-prefix
> invariant).

## Context

The open-source voice peer set (Pipecat, LiveKit Agents, Vocode) and the
provider landscape (Deepgram Flux, Gemini Live, OpenAI Realtime GA)
shifted materially in 2026. Several features went from differentiators
to table stakes. None of these are required to answer the five
debug-first questions, but the debug-first thesis doesn't win on its own
if the provider and realtime story falls behind.

This file owns the provider-side additions plus the realtime
history-management defaults that only make sense once a realtime
provider is in play.

## Provider Additions

### Deepgram Flux STT Adapter

Conversational STT (Q1 2026) that fuses VAD, STT, and endpointing into
one streaming API with `StartOfTurn` / `EagerEndOfTurn` / `TurnResumed` /
`EndOfTurn` events and ~260ms p50 end-of-turn detection. This reshapes
the pipeline: for Flux users, three stages collapse to one and "what is
my turn-detection config?" disappears entirely.

When selected, VAD and Smart Turn become passthrough; the user never
configures them. Auto-selected when `DEEPGRAM_API_KEY` is present and
`stt=` is omitted. Biggest one-shot simplification available in the 2026
provider landscape.

Integration notes:

- Wire Flux's native turn events directly to the turn FSM rather than
  building a translation layer. The whole point of Flux is that
  endpointing decisions come from the provider.
- The capability flag lives on the provider adapter, not as a
  special-case branch in the public API. Any future provider that
  subsumes VAD/endpointing should go through the same passthrough
  mechanism.
- `easycat doctor` should confirm Flux reachability specifically (its
  WebSocket handshake differs from non-Flux Deepgram endpoints).

### Gemini 3.1 Flash Live Bridge

Second viable one-API-key realtime path alongside OpenAI Realtime. 70
languages, native barge-in, proactive audio, affective dialog, tool use
plus Google Search. Zero-friction for users with existing Google Cloud
credentials.

Integration notes:

- Implemented as an `ExternalAgentBridge` variant (`GeminiLiveAgentBridge`),
  reusing the realtime mode plumbing from the essential plan.
- Swapping between OpenAI Realtime and Gemini 3.1 Flash Live should be a
  one-string config change (`realtime_provider="openai"` vs
  `"gemini"`).
- Tool calls bridge through the existing tool-call journal records; no
  new record type.

### Smart Turn v3.1 Promotion

Currently `enabled=False` in EasyCat (`src/easycat/smart_turn.py`) and
buried in nested config. Promote to top-level `smart_turn=True` with a
single `smart_turn_sensitivity` knob. Enable by default where the ONNX
runtime is available.

Wrap Pipecat's `LocalSmartTurnAnalyzerV3` (Pipecat ≥ 0.0.85) rather than
reimplementing — they track upstream model revisions, tokenizer updates,
and sample-rate quirks, and reimplementing loses those fixes silently.
Depend on `pipecat-ai[smart-turn]` as an optional extra. The existing
EasyCat SmartTurn path stays as a fallback for environments where the
Pipecat extra cannot be installed.

Why Smart Turn v3.1 is the 2026 standard:

- Whisper Tiny backbone + linear classifier, ~8M parameters
- 8MB int8 quantized
- 12ms CPU inference on modern hardware (~60ms on budget AWS instances,
  no GPU required)
- 23 languages
- Dramatic accuracy improvements over v3.0 for English and Spanish
- Runs locally via ONNX with no network dependency

Auto-disabled when a conversational STT like Flux is selected (the
provider handles endpointing natively).

**Known gotcha**: Smart Turn v3 silently breaks at
`audio_in_sample_rate=8000` (telephony). The EasyCat wrapper must
resample telephony audio to 16kHz before invoking the model, and emit a
clear warning if a user tries to feed 8kHz audio through.

### Backchannel Filter

Small audio-ML classifier distinguishing genuine interruptions from
backchannels ("mhm", "yeah"), coughs, and sighs. LiveKit Agents 1.5
ships this default-on. Without it, the "works in a noisy café"
comparison goes to LiveKit.

- Ships as part of the `InterruptionController` path established in the
  essential plan.
- Default on behind a single `backchannel_filter=False` escape hatch.
- No user-facing configuration beyond the on/off toggle.

## Cache-Friendly Realtime Defaults

OpenAI gpt-realtime's GA pricing: $32/1M audio-in, $0.40/1M *cached*
audio-in — an 80× discount and the single biggest cost lever in voice AI
2026. Almost nobody gets this right without help: any edit to the
message prefix busts the cache, and naive history truncation busts it
every turn.

Runtime defaults must hit the discount without user intervention:

- **`retention_ratio=0.8`** — truncate framework-owned message history
  in 20% chunks rather than one turn at a time, so the cached prefix
  stays stable for runs of turns before a bust. Default 0.8 means the
  runtime truncates in 20% chunks and never edits the cached prefix,
  which hits the cache discount without the user learning anything. Set
  to `1.0` to disable truncation; lower for shorter effective context.
- **Never rewrite the cached prefix** — interruption patches that would
  edit an already-delivered assistant turn instead append a correction
  as a new turn. The original turn stays byte-identical to what was
  cached. This is a hard invariant the bridge must enforce.
- **`CacheBust` journal record** on the first turn after a bust is
  detected, with the reason (truncation, prefix edit, tool-call
  injection). Users can find and fix whatever is causing the
  regression.
- **`easycat doctor` reports** `cache_hit_ratio` averaged over recent
  sessions and warns if it drops below 50% with specific fix
  suggestions tied to the most recent `CacheBust` reasons.

**Success criterion**: any realtime session lasting more than five
turns hits `cache_hit_ratio ≥ 0.6` without user config.

Dependencies:

- The `retention_ratio` policy lives in the bridge execution cursor path
  (essential Phase 2).
- `CacheBust` is a journal record type that piggybacks on the essential
  Phase 1 schema, but the detection logic lives in the bridge.
- `CostRecord.cache_hit_ratio` is the observability piece and lives in
  `peripheral-observability-and-cost.md`. The two are a coordinated
  pair.

## Dependencies on the Essential Plan

| Item | Depends on |
|---|---|
| Deepgram Flux STT adapter | stage model (essential Phase 3) for clean integration |
| Gemini 3.1 Flash Live bridge | bridge (Phase 2), realtime mode support (Phase 3) |
| Smart Turn v3.1 promotion (Pipecat wrapper) | stage model (Phase 3) |
| Backchannel filter | `InterruptionController` (Phase 3) |
| `retention_ratio=0.8`, never-rewrite-prefix invariant | bridge (Phase 2) |
| `CacheBust` journal record emission | journal records stable (Phase 1), bridge detection logic (Phase 2) |

## Suggested Sequencing

1. **After essential Phase 2**: `retention_ratio=0.8` and the
   never-rewrite-prefix invariant land with the bridge, because they
   are bridge-side behavior. `CacheBust` records start emitting.
2. **In parallel with essential Phase 3**: Deepgram Flux adapter, Smart
   Turn v3.1 promotion, backchannel filter. All three exercise the new
   stage model; landing them together stress-tests it.
3. **After essential Phase 3**: Gemini 3.1 Flash Live bridge, once the
   realtime session mode is known to work with OpenAI Realtime.

## Competitive Context

- **Deepgram Flux** (Q1 2026): conversational STT that fuses VAD + STT +
  endpointing into a single streaming API. ~260ms p50 end-of-turn.
- **Gemini 3.1 Flash Live** (March 2026): 70 languages, native barge-in,
  proactive audio, affective dialog, tool use + Google Search — second
  viable one-API-key speech-to-speech runtime alongside OpenAI Realtime.
- **OpenAI gpt-realtime GA pricing**: $32/1M audio-in, $0.40/1M cached
  audio-in. 80× discount is the single biggest realtime cost lever.
- **Pipecat Smart Turn v3.1** (Apache 2.0): industry-standard endpoint
  detector in 2026. Whisper Tiny + linear classifier, ~8M params, 8MB
  int8, 12ms CPU inference, 23 languages. Pipecat ships the wrapper as
  `LocalSmartTurnAnalyzerV3` (≥ 0.0.85).
- **LiveKit Agents 1.5**: ML-based backchannel / false-interruption
  detection default-on.
- **Cartesia Sonic 3 / Sonic Turbo**: ~90ms TTFA (Sonic 3), ~40ms TTFA
  (Sonic Turbo) — set the 2026 TTS latency bar. Not a new EasyCat
  provider (existing Cartesia adapter covers it), but sets the default
  choice for latency-sensitive templates.
- **AssemblyAI research**: falling pitch at sentence end is a stronger
  signal than silence duration. Smart Turn v3.1 already combines
  prosodic and semantic features, which is why it has displaced older
  alternatives.
