# Chapter 9 — Exercises

## 1. Probe the over-/under-shoot of `heard_text`

**Task.** Run `estimate.py`. Interrupt the bot as close as you can
after hearing one word; repeat several times because a human reaction
is not an exact clock. Open each bundle — does `heard_text` end at that
word, or does it over- or under-shoot? Then inspect the production
transport capabilities without opening audio devices:

```bash
uv run python docs/teaching/09-interruption/playback_evidence.py
```

**Hints**

1. The toy estimator multiplies `bytes_accepted` by ~15 chars / 48000
   bytes / second (24 kHz × 2 bytes/sample). The constant is an
   *average* — a fast word like "yes" lasts ~150 ms but the formula
   assigns it ~500 ms; a slow word like "elephant" lasts ~600 ms
   and gets the same ~500 ms.
2. The playback queue also lies: `transport.send_audio=True`
   means accepted, not heard. `clear_audio()` drops queued chunks,
   so `bytes_accepted` *overcounts* by that unplayed backlog. A
   `False` return is different: that chunk was rejected and the
   corrected toy does not count it at all.
3. Net effect: `heard_text` *usually* overshoots by 0-2 words. On
   a *slow* word at the start of a sentence it can undershoot.
4. Production uses the strongest progress evidence each transport
   exposes. `LocalTransport` and WebRTC emit `TransportAudioDelivered`
   when their output callback/track consumes a chunk. Twilio sends
   playback marks and emits `PlaybackMarkAck` when Twilio acknowledges
   reaching them. Other transports fall back to a serial-playout timing
   estimate from the send log.
5. These signals are stronger than `send_audio=True`, but none is
   literal ground truth at the human ear: device, network, and acoustic
   delays can remain after the transport milestone.

## 2. Make markdown break the estimator

**Task.** Have the agent reply with markdown-heavy output (ask it
for a table or a bulleted list). The text fed to TTS is
`strip_markdown(text)` — shorter than the original. How does this
affect `heard_text` vs reality?

**Hints**

1. `sentences_sent` records the *stripped* text (what TTS actually
   spoke). `bytes_accepted` is accepted bytes of the stripped audio.
   So `bytes_accepted → heard_chars` is internally consistent *on
   the stripped text*, before the playback-queue correction.
2. The bug arises when you append `heard_text` back into the
   conversation history — should it be the stripped version (what
   the user heard) or the original (what the LLM produced)? The
   toy uses stripped, which is correct for the *next turn's
   prompt* but loses the markdown structure.
3. Production `interruption.py` keeps both: stripped for the user
   model, original for any tool that wants the structured text.

## 3. Why does AEC fix self-interruption?

**Task.** Run `estimate.py` on speakerphone (no headphones). The
bot interrupts itself. Why does AEC fix this, and why is VAD alone
not enough?

**Hints**

1. VAD's job is "is this frame speech?" — it can't distinguish
   the user's speech from the bot's speech radiated back through
   the speaker. From VAD's perspective, both are equally
   "speech."
2. AEC takes the TTS audio we sent to the speaker as a
   *reference*, and subtracts the echo path's filtered version of
   that reference from the mic. The result is a mic signal that
   no longer contains the bot's voice — only the user's.
3. AEC is *dual-input* (mic + reference); VAD is *single-input*
   (mic only). No amount of better VAD will fix the loop, because
   the information VAD needs isn't in its input.
4. This is the preview of chapter 10.

## 4. Trace the turn that triggers barge-in

**Task.** Run the provider-free continuity probe:

```bash
uv run python docs/teaching/09-interruption/barge_in_turn_probe.py
```

Explain why the triggering `speech_started` event must not be consumed
by the cancellation branch, and why the mic frames are still available
after the coordinator waits for the old bot task.

**Hints**

1. `route_barge_in` returns `event_consumed: false`. The coordinator
   therefore falls through to the same STT-start branch used by an
   ordinary user turn.
2. `mic_producer` is a separate coroutine. It keeps adding pre-roll and
   live frames to `mic_queue` while cooperative bot shutdown finishes.
3. Change the probe's fallthrough condition to act as if the event were
   consumed. The STT lifecycle disappears, which is the old behavior:
   the bot stops, but the user must repeat the interruption.
4. On process shutdown, the coordinator must stop both possible
   in-flight owners—STT and the bot task—before the shared TTS/client/VAD
   stack closes.

## Self-check

You should be able to: (a) name the three differences between
versions A, B, and C, (b) describe why "bytes accepted ≠ bytes heard"
without re-reading the README—including why rejected differs from
accepted-but-queued—and (c) explain why `CancelToken` is a token
and not an exception, and (d) trace the triggering `speech_started`
event into the next STT turn.
