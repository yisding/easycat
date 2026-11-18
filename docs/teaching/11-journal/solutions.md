# Chapter 11 — Solutions

[← Back to chapter](./README.md) · [Ladder index](../)

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

- `turn.started` — VAD opened a turn, so someone spoke.
- `stt.partial` with `text=""`
- `stt.input` with `frames_sent=0`, `bytes_sent=0` (!)
- `stt.final` with `text=""`
- `turn.state_changed: SPEAKING → PROCESSING`
- No `stage.agent.execute`, no `stage.tts.execute`, no `turn.gap`

### Root cause

No audio reached STT. VAD opened the turn, but `stt.input` shows
that neither the cached pre-roll nor the live speech frames were
forwarded to the STT stream, so STT had nothing to transcribe and
committed `text=""`. The agent stage then short-circuited on empty
input (see `run_turn`'s `if not final_text.strip(): return` in
chapters 5-10).

Rule out the tempting alternative: a lost speech onset (a pre-roll
bug) makes STT *mishear* the leading sound — chapter 4's "Hello"
becoming "Elo" — but it still returns words. An empty final with
zero frames sent points at the routing between VAD and STT, for
example frames forwarded before the stream was open and silently
dropped by a `stt is not None` guard like the one in chapter 4's
`parrot`.

### Fix

Restore chapter 4's order: at VAD-on, open the STT stream, flush
all cached pre-roll frames in order, then forward live frames until
VAD-off. Then make the failure loud: count the frames and bytes sent
to STT per turn, and treat zero at commit as a pipeline bug rather
than "the user said nothing".

## Bug 2 — TTS stutter

### Evidence in the journal

- Three `stage.tts.execute` records.
- Sentence 1: `elapsed_ms = 420`.
- Sentence 2: `elapsed_ms = 2100` — **5× sentence 1**.
- Sentence 3: `elapsed_ms = 390`.
- Between sentences 1 and 2: `ws.reconnect.attempt` ×2,
  `ws.reconnect.failure` ×1, `ws.reconnect.success` ×1.
- The reconnect's own `elapsed_ms = 1400` accounts for most of
  the extra ~1.7 s on sentence 2; the remainder is the stream
  restarting on the new socket.

### Root cause

The TTS provider's WebSocket (`provider: "cartesia_tts"`) dropped
mid-session. The reconnect logic recovered silently — correct
behaviour — but the synth for the second sentence paused until the
socket was back up. The first attempt failed; the second succeeded.

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
