# Provider Ecosystem — Peripheral

> **This is a peripheral initiative.** It is not essential to the
> debug-first thesis in `essential-debug-first-runtime.md`. It is what
> keeps EasyCat's chained-pipeline provider story competitive in 2026.
>
> **Sibling peripheral docs:**
>
> - `peripheral-dx-onboarding.md` — line budgets, library helpers,
>   template content, error diagnostics
> - `peripheral-cli.md` — `easycat` CLI, including `doctor` which
>   probes providers listed in this file
> - `peripheral-redaction.md` — `RedactionPolicy` write filter, safe
>   snapshots, export-time redaction pass, ready-to-use policies
> - `peripheral-observability-and-cost.md` — OTel export, cost modeling,
>   latency budgets, warmup stage
> - `peripheral-eval-and-debugger-ui.md` — `easycat.testing`, Simulator +
>   Judge, forked replay, interactive debugger UI, dev waterfall
>
> **In scope (this file):** Deepgram Flux STT adapter (conversational
> STT that collapses VAD + STT + endpointing), Smart Turn v3.1 promotion
> wrapping Pipecat's `LocalSmartTurnAnalyzerV3`, backchannel filter
> (ML-based false-interruption detection).
>
> **Permanently out of scope (guardrail, see essential plan):**
> voice-to-voice / realtime speech-to-speech providers (OpenAI
> Realtime, Gemini Live, Kyutai, etc.). EasyCat is a chained voice
> runtime. Users who want voice-to-voice should use the provider SDK
> directly.

## Context

The open-source voice peer set (Pipecat, LiveKit Agents, Vocode) and the
chained-pipeline provider landscape (Deepgram Flux, Cartesia Sonic 3)
shifted materially in 2026. Several features went from differentiators
to table stakes. None of these are required to answer the five
debug-first questions, but the debug-first thesis doesn't win on its own
if the chained-pipeline provider story falls behind.

This file owns the provider-side additions for the chained pipeline.
Voice-to-voice and realtime speech-to-speech are not part of EasyCat's
scope (see the "Chained Only" rationale and Explicit Guardrails in
`essential-debug-first-runtime.md`).

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
chained-pipeline provider landscape.

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
- Flux's native endpointing events flow through the same
  `TurnStage.snapshot_state()` reproducibility contract as Silero VAD
  and Smart Turn (see essential-plan "Voice Stage Decisions Must Be
  Reconstructable" principle). The snapshot records the Flux event
  stream by artifact ref plus the decision the stage emitted, so a
  captured session replays deterministically even though the decision
  itself came from the provider.

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

## Dependencies on the Essential Plan

| Item | Depends on |
|---|---|
| Deepgram Flux STT adapter | stage model (essential Phase 3) for clean integration |
| Smart Turn v3.1 promotion (Pipecat wrapper) | stage model (Phase 3) |
| Backchannel filter | `InterruptionController` (Phase 3) |

## Suggested Sequencing

1. **In parallel with essential Phase 3**: Deepgram Flux adapter, Smart
   Turn v3.1 promotion, backchannel filter. All three exercise the new
   stage model; landing them together stress-tests it.

## Competitive Context

- **Deepgram Flux** (Q1 2026): conversational STT that fuses VAD + STT +
  endpointing into a single streaming API. ~260ms p50 end-of-turn.
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
- **OpenAI Realtime and Gemini Live** (2026): voice-to-voice /
  speech-to-speech realtime APIs. Out of EasyCat's scope by design —
  see the "Chained Only" rationale in
  `essential-debug-first-runtime.md`. Users who need realtime use the
  provider SDK directly.
