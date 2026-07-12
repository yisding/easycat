# Chapter 0 — Exercises

One exercise from the chapter README, plus hints if you get stuck.
No worked solutions checked in — the point is that you take a swing
and form your own answer before peeking.

## 1. Drop the sample rate to 8 kHz

**Task.** Change `SAMPLE_RATE` at the top of `main.py` to `8000`,
re-record, and play back. Is speech still intelligible? What about
music? (Try humming a song while the recording window is open.)

**Hints**

1. For an ideally band-limited signal, an 8 kHz sample rate can represent
   frequencies below the 4 kHz Nyquist boundary. A real anti-alias filter
   must attenuate frequencies before that boundary; it is not a brick wall.
2. Narrowband G.711 telephony intentionally filters speech to roughly
   300–3400 Hz before sampling at 8 kHz. Speech can remain intelligible
   while losing high-frequency detail and cues; “intelligible” does not
   mean “unchanged” or “optimal for every STT model.”
3. Listen for lost brightness, “air,” consonant detail, and sibilance.
   Content the front end removes cannot be recovered by later upsampling.
4. The byte math also changed: `3 s × 8000 × 2 × 1 = 48_000 B`.
   You cut the raw PCM byte rate in half; only listening and downstream
   measurements can tell you whether the quality tradeoff is acceptable.

**Wider points to check yourself on**

- Why does this matter for a voice pipeline? Phones still use
  8 kHz. Twilio gives you μ-law 8 kHz. Knowing the ceiling helps
  you debug "my STT is fine on my laptop but worse on the phone."
- A higher sample rate is not automatically better. It raises raw byte
  rate and the theoretical frequency ceiling, but the microphone,
  filters, codec, provider, and model determine whether extra samples
  preserve useful information.
- Why does the script call the first timing
  `time-to-first-write-return`, not `time-to-first-sound`? Returning
  from `OutputStream.write()` proves that the host accepted the buffer,
  not that a physical speaker played it. Later transports preserve the
  same acceptance-versus-delivery distinction.
- Why is `OutputStream` inside a `with` block? The context starts the
  stream on entry and guarantees stop + close on normal exit, a failed
  write, or Ctrl-C.

## 2. Trace every format boundary

**Task.** Run the provider-free format catalog and find two resampling
boundaries whose input and output are both mono PCM:

```bash
uv run python docs/teaching/00-hello-audio/format_boundaries.py
```

**Hints**

1. Local capture defaults to 24 kHz, while Deepgram's streaming STT target
   defaults to 16 kHz. The provider adapter resamples at that input boundary.
2. WebRTC receives and sends 48 kHz media frames, but its default pipeline
   target is 16 kHz. Those are two boundaries of one transport, not a
   contradiction.
3. OpenAI TTS defaults to 24 kHz. A WebRTC session resamples that output to
   48 kHz for media; a Local session already has a matching 24 kHz target.
4. Twilio's wire is 8 kHz μ-law while EasyCat's default internal pipeline
   target is 16 kHz PCM. Decoding and upsampling make the representation
   compatible but do not restore telephone-band frequencies.
5. Change `DeepgramSTTConfig(sample_rate=...)` in a scratch copy of the
   probe. You changed one provider-input boundary, not the capture, TTS, or
   transport wire formats.

## Self-check

You should now be able to predict — without running the code —
roughly how an utterance will sound at 4 kHz, 8 kHz, 16 kHz, and
44.1 kHz, explain the difference in one sentence each, and identify
the boundary meant by any sample rate you quote.
