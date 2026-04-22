# Chapter 11 — Solutions

**Do not read this until you have tried the investigations.**

. . .

. . .

. . .

. . .

. . .

. . .

. . .

. . .

. . .

. . .

. . .

. . .

## Bug 1 — empty final

### Evidence in the journal

- `turn.started`
- `stt.partial` with `text=""`
- `stt.final` with `text=""` (!)
- `turn.state_changed: SPEAKING → PROCESSING`
- No `stage.agent.execute`, no `stage.tts.execute`, no `turn.gap`

### Root cause

Pre-roll off-by-one. The first frame of real speech was dropped
before STT saw it. STT's commit threshold triggered on what
looked like trailing silence and emitted `text=""`. The agent
stage then short-circuited on empty input (see `run_turn`'s
`if not final_text.strip(): return` in chapters 5-10).

### Fix

Audit the flush order in `MiniTurnDetector.frames`: the pre-roll
buffer must flush **before** the first `speech_started`-tagged
chunk is yielded, not after. Re-derive the invariant from
chapter 4: the first frame yielded for a new turn must be the
first frame containing speech energy.

## Bug 2 — TTS stutter

### Evidence in the journal

- Three `stage.tts.execute` records.
- Sentence 1: `elapsed_ms = 420`.
- Sentence 2: `elapsed_ms = 2100` — **5× sentence 1**.
- Sentence 3: `elapsed_ms = 390`.
- Between sentences 1 and 2: `ws.reconnect.attempt` ×2,
  `ws.reconnect.failure` ×1, `ws.reconnect.success` ×1.
- The reconnect's own `elapsed_ms = 1400` lines up neatly with
  the extra ~1.7 s on sentence 2.

### Root cause

The TTS provider's WebSocket dropped mid-session. The
`ReconnectingWebSocket` reconnected silently — correct behaviour
— but the synth for the second sentence paused until the socket
was back up. The first attempt failed; the second succeeded.

### Fix

This is not really a bug in *our* code; it's an upstream
network hiccup. Options:

- Pre-warm a second TTS connection and hot-swap on drop.
- Cache the first few sentences of common replies locally.
- Surface reconnect events in the UI so the user sees "still
  thinking…" instead of dead air.

The *teaching lesson* is that a flaky-feeling voice bot doesn't
always have a code bug. Sometimes it has a network-weather bug,
and the journal distinguishes them cleanly.

## Bug 3 — ghost interruption

### Evidence in the journal

- First turn:
  - `audio.config: {nr: "rnnoise", aec: "off"}`
  - `turn.started`
  - `stt.final: "What time is it?"` — real user speech.
  - `agent.first_token`
  - `stage.tts.execute: "It's about three in the afternoon."`
    — bot begins speaking.
  - `interruption.start` — bot cancels itself.
  - `turn.state_changed: BOT_SPEAKING → IDLE`
- **No STT partial or final** near the `interruption.start` event.
  The barge-in fired, but no user speech was transcribed.
- Second turn: identical pattern.

### Root cause

Acoustic self-trigger. The bot's own TTS output leaks back
through the mic on a speakerphone configuration. VAD fires
`VADStartSpeaking` on the bot's voice. The coordinator treats
any `speech_started` during bot speech as a barge-in and
cancels.

The smoking gun is the `audio.config` record at sequence 1:
**`aec: "off"`**. AEC would subtract the bot's voice from the mic
and prevent VAD from firing on it. Without AEC, on a speakerphone,
the bot cuts itself off every time.

### Fix

Enable AEC. If the LiveKit APM extra isn't installed, the factory
silently falls back to passthrough — which is this exact bug. Chapter 10 walks through the fix end to end.
