# Chapter 9 — Exercises

## 1. Probe the over-/under-shoot of `heard_text`

**Task.** Run `estimate.py`. Interrupt the bot **exactly after one
word**. Open the bundle — does `heard_text` end at that word, or
does it over- or under-shoot?

**Hints**

1. The toy estimator multiplies `bytes_sent` by ~15 chars / 48000
   bytes / second (24 kHz × 2 bytes/sample). The constant is an
   *average* — a fast word like "yes" lasts ~150 ms but the formula
   assigns it ~500 ms; a slow word like "elephant" lasts ~600 ms
   and gets the same ~500 ms.
2. The OS playback buffer also lies: `transport.send_audio`
   enqueues PortAudio chunks, which hold ~10-100 ms before the
   speaker driver hands them off. `clear_audio()` drops those, so
   `bytes_sent` *overcounts* by that buffer.
3. Net effect: `heard_text` *usually* overshoots by 0-2 words. On
   a *slow* word at the start of a sentence it can undershoot.
4. Production `easycat.session.interruption` uses playback-ack
   marks (from `LocalTransport`) to ground-truth the buffer
   correction. The toy doesn't — read the production code once
   you understand why.

## 2. Make markdown break the estimator

**Task.** Have the agent reply with markdown-heavy output (ask it
for a table or a bulleted list). The text fed to TTS is
`strip_markdown(text)` — shorter than the original. How does this
affect `heard_text` vs reality?

**Hints**

1. `sentences_sent` records the *stripped* text (what TTS actually
   spoke). `bytes_sent` is real bytes of the stripped audio. So
   `bytes_sent → heard_chars` is correct *on the stripped text*.
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

## Self-check

You should be able to: (a) name the three differences between
versions A, B, and C, (b) describe why "bytes sent ≠ bytes heard"
without re-reading the README, and (c) explain why `CancelToken`
is a token and not an exception.
