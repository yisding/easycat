# Chapter 13 — Exercises

## 1. Add a Cartesia provider preset

**Task.** Add a `--provider-mix cartesia` preset (both STT and TTS
via Cartesia's WebSocket API). What's the minimum diff from
`deepgram-eleven`?

**Hints**

1. The diff is *only* the three configuration lines from the
   chapter README — `stt`, `tts`, and the credentials. The
   `Agent`, `Session`, event bus, journal, smart-turn — none of
   that moves. That's the Protocol payoff.
2. Check `src/easycat/stt/factory.py` and `src/easycat/tts/factory.py`
   for the registry entries. If Cartesia isn't already wired,
   adding it is one entry in `_PROVIDER_TO_CONFIG` per side.
3. The bundle shape will be the same as the other ch 13 bundles
   — production shape (`stage_start`/`stage_complete` pairs). The
   `evals.py` translator from chapter 15 will work on it
   unchanged.

## 2. Tightest P95/P50 ratio

**Task.** Run all six cells on the same short prompt ("What time
is it?"). Which cell has the tightest P95/P50 ratio in chapter
12's eval output? Why?

**Hints**

1. P95/P50 ratio measures *consistency*, not absolute speed. A
   slow-but-consistent pipeline beats a fast-but-jittery one for
   user experience.
2. WebSocket-based providers (Deepgram, ElevenLabs) tend to have
   tighter ratios *once warm* because they hold an open
   connection — no TLS handshake per call. The HTTP OpenAI batch
   STT shows higher variance from cold-start.
3. Transports also affect this. WebRTC has the tightest jitter
   profile (UDP, optimized for live). Twilio has the widest
   (μ-law over PSTN, public-internet variance). Local has the
   lowest absolute latency but is dominated by audio-device
   buffer choices.
4. With only a few turns per cell, P95 is a single bundle's
   slowest run — noisy. Re-run each cell ~20 times for a
   meaningful number.

## 3. SendDTMFAction on a real call

**Task.** Wire `SendDTMFAction` from chapter 7 into the agent (the
user asks for "press 1 to continue"). What does the journal show
on the Twilio preset? What does a user on the phone hear?

**Hints**

1. `SendDTMFAction(digits="1")` is dispatched to
   `TwilioSessionActionExecutor`, which calls Twilio's REST API
   to inject DTMF tones into the active call. The journal records
   `session_action.dispatched` with the action class.
2. The user on the phone hears a brief "beep" — Twilio plays the
   tone into the call leg before yielding back to the bot's
   audio.
3. On the `local` transport, the same action would be a no-op:
   `CoreSessionActionExecutor` doesn't claim DTMF. The journal
   would record `session_action.unhandled`. This is by design —
   the same agent code works across transports, but each
   transport claims what it can execute.
4. Don't test this on a production phone number. Use Twilio's
   test credentials.

## Self-check

You should be able to: (a) name two axes the matrix attacks, (b)
draw the "one code change per axis" diagram from memory, and (c)
explain why some providers receive an `EventBus` at construction
and others don't.
