# EasyCat Features (MVP)

EasyCat is a slim, batteries-included voice bot framework designed to pair with
the OpenAI Agents SDK (Python) or PydanticAI. EasyCat handles audio I/O,
VAD/turn-taking, STT/TTS providers, telephony basics (DTMF + voicemail), and
noise reduction.

## Scope

### In scope (MVP)
- Real-time conversational voice bots (local, web, and phone)
- STT + TTS with OpenAI, Deepgram, ElevenLabs (both directions)
- Best-available commercial VAD, with an open-source fallback
- Noise reduction (commercial + OSS fallback)
- DTMF support (telephony and testing hooks)
- Voicemail / answering machine detection
- Tight integration with OpenAI Agents SDK voice/workflow streaming concepts
- Adapter layer so you keep your Agents SDK or PydanticAI agent code idiomatic

## 1) Core Runtime (Minimal "Voice Loop" Engine)

### Required pipeline
```
Audio In -> (Noise Reduction) -> (VAD / Turn) -> STT -> Agents Workflow -> TTS -> Audio Out
```

### Session lifecycle
- start(), stop(), shutdown()

### Cancellation
- cancel_turn(), cancel_tts_playback(), reset_state()

### Event model (streaming-first)
Audio input
- audio_in(chunk)

VAD / turn-taking
- vad.start_speaking, vad.stop_speaking

STT
- stt.partial(text), stt.final(text)

Agent
- agent.delta(text), agent.final(text) (optional, depending on workflow)

TTS
- tts.audio(chunk), tts.markers(...)

Lifecycle
- bot.started_speaking, bot.stopped_speaking
- turn.started, turn.ended
- interruption(details)

Tools
- tool.call_started(tool_name, call_id)
- tool.call_delta(call_id, delta)
- tool.call_result(call_id, result)

Connectivity
- reconnect.attempt(provider, attempt)
- reconnect.success(provider)
- reconnect.failure(provider, error)

Telephony
- dtmf(digit)
- dtmf.aggregated(sequence)
- voicemail.detected(type=human|machine|unknown)

Errors
- error(exception)

(Design mirrors the Agents SDK voice event style: audio + lifecycle + error.)

## 2) Agent Framework Integration (Python)

### MVP integration points
- Accept an OpenAI Agents SDK or PydanticAI agent/workflow and run it per user turn
- Support streaming where available (text deltas, tool events) using framework primitives
- Pass through (or optionally augment) Agents SDK tracing hooks when present

### What EasyCat does NOT redo
- Tool calling schemas, handoffs, guardrails, agent memory: those stay in the
  agent framework (OpenAI Agents SDK or PydanticAI)

## 3) Speech-to-Text (STT) - OpenAI + Deepgram + ElevenLabs

### Unified STT interface
- start_stream() / send_audio(chunk) / end_stream()
- Emit:
  - partial transcripts (when provider supports it)
  - final transcript per detected turn
- Normalization: timestamps (optional), confidence (optional), language code (optional)

### Providers (MVP)
- OpenAI STT
  - Turn-based transcription via Audio API (transcriptions endpoint; models include gpt-5.2-transcribe, etc.)
  - Use VAD-driven turn segmentation; submit finalized user turns for transcription
- Deepgram Streaming STT
  - Real-time transcription over WebSocket (listen-streaming)
  - Supports continuous partial + final events depending on configuration
- ElevenLabs STT
  - Batch transcription endpoint + realtime speech-to-text WebSocket API

## 4) Text-to-Speech (TTS) - OpenAI + Deepgram + ElevenLabs

### Unified TTS interface
- Input: text chunks (or full text)
- Output: streaming audio frames/chunks, plus optional alignment/markers
- Must support:
  - stop() / cancel() mid-utterance (for barge-in)
  - output format selection (PCM16 preferred internally; convert as needed)

### Providers (MVP)
- OpenAI TTS
  - Audio API (audio/speech) + streaming output support
- Deepgram TTS (Aura)
  - WebSocket streaming TTS (continuous text stream -> audio stream)
- ElevenLabs TTS
  - Streaming TTS via chunked transfer encoding and WebSockets options

## 5) "Best Possible" VAD + Open Source Fallback

### VAD requirements
- Low false-positive rate in noisy environments (critical for barge-in + turn-taking)
- Configurable:
  - min speech duration
  - min silence duration
  - sensitivity/threshold
  - pre-roll / post-roll buffering (to avoid clipping)

### Primary (commercial) VAD option (MVP)
- Krisp VAD (VIVA) integration as the premium/default when configured
  - Krisp documents VAD support as part of its Voice AI / SDK stack.

### Built-in open-source fallback (always available)
- Silero VAD (local, open-source)

### Implementation note (MVP behavior)
- If Krisp is not configured/licensed, EasyCat automatically falls back to Silero
  without changing the application code.

## 6) Noise Reduction / Enhancement (Required)

### Goals
- Improve STT accuracy and turn detection robustness
- Reduce user-perceived "bad audio" on calls

### Primary (commercial) noise reduction (MVP)
- Krisp noise cancellation / voice isolation integration (when configured)

### Built-in open-source fallback
- RNNoise noise suppression (local, open-source)

### Placement (MVP)
- Noise reduction runs before VAD and STT by default (configurable).

## 7) Turn-Taking + Interruption (Barge-In)

### Turn-taking (MVP)
- VAD-based turn start
- Silence-based end-of-turn (configurable timeout)
- Optional "push-to-talk" / manual end-of-turn mode for testing

### Interruption / barge-in (MVP)
If bot is speaking and VAD detects user speech:
- immediately stop local playback / outbound audio stream
- cancel current TTS request
- begin next user turn capture

(Design aligns with Agents SDK voice lifecycle event patterns; EasyCat provides
interruption behavior even when upstream does not.)

## 8) Telephony MVP: Twilio + DTMF + Voicemail

### Telephony transport (MVP)
- Twilio Media Streams (bidirectional WebSocket) transport:
  - Receive inbound call audio
  - Send audio back to caller in real-time
  - TwiML <Stream> / <Connect><Stream> compatible session bootstrap

### DTMF (Required)
DTMF input (MVP)
- Parse and emit DTMF events from Twilio Media Streams dtmf WebSocket messages
  (supported in bidirectional streams)
- Optional fallback path: TwiML <Gather> (digits collection) for non-stream / legacy call flows

DTMF output (MVP)
- Support "send tones" via TwiML:
  - <Play digits="..."> for DTMF tone playback
  - (Optional telephony utility) <Dial><Number sendDigits="..."> for dialing extensions/IVR sequences

DTMF aggregator (MVP)
- Collect digit sequences with:
  - timeout (e.g., 2s idle)
  - terminators (#, *)
  - max length
- Emit a single event (dtmf.aggregated) suitable for agent tool use
  (for example: "enter account number").

### Voicemail / Answering Machine Detection (Required)
Primary detection (MVP, Twilio outbound)
- Support Twilio Answering Machine Detection (AMD) results: human vs machine/fax

Fallback detection (MVP, any audio)
- Heuristic voicemail detection based on:
  - "greeting-like" long monologues without pauses
  - beep detection (simple tone/energy heuristics)

Emit: voicemail.detected(human|machine|unknown) and allow policy:
- hang up / leave message / transfer to agent tool

## 9) Transports (MVP)

### Must-have transports
- Local transport (developer mode)
  - Mic input + speaker output, fast iteration
- WebSocket transport (default for apps)
  - Simple browser/mobile integration
  - Matches telephony stream patterns
- Twilio Media Streams transport
  - Real phone calls with DTMF + voicemail detection

## 10) Audio & Format Handling (MVP)

### Internal audio format contract
- Internal: PCM16 mono (recommended) with timestamps

### Accept and convert
- 8 kHz (telephony), 16 kHz (STT), higher if needed

### Features
- resampling (8k/16k/24k/48k - arbitrary rate support to accommodate noise reduction and provider requirements)
- mono downmix
- chunk sizing utilities (10-30ms frames for VAD)

## 11) Reliability, Retries, and Backpressure (MVP)
- Provider WebSocket reconnect strategy for streaming STT/TTS providers
- Timeouts for:
  - STT response
  - agent run
  - TTS first audio byte
- Backpressure protection:
  - bounded queues for audio in/out
  - drop/skip policy for stale audio when canceled

## 12) Metrics & Observability (MVP)

### Metrics (minimum)
- stt_latency_ms
- agent_latency_ms
- tts_ttfb_ms
- turn_end_to_end_ms
- counts: interruptions, reconnects, errors

### Tracing (MVP)
- Integrate with Agents SDK built-in tracing (pass through trace context / emit custom spans where helpful)
