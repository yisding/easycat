# Chapter 0 — Exercises

One exercise from the chapter README, plus hints if you get stuck.
No worked solutions checked in — the point is that you take a swing
and form your own answer before peeking.

## 1. Drop the sample rate to 8 kHz

**Task.** Change `SAMPLE_RATE` at the top of `main.py` to `8000`,
re-record, and play back. Is speech still intelligible? What about
music? (Try humming a song while the recording window is open.)

**Hints**

1. Nyquist says you can reconstruct any frequency below
   `SAMPLE_RATE / 2`. At 8 kHz sample rate, that's a 4 kHz ceiling.
2. Human-speech *intelligibility* energy stops around 4 kHz —
   telephony has used 8 kHz sampling for decades for exactly this
   reason. Music goes much higher (cymbals, violins).
3. Listen for what's *missing* in the music recording. The
   missing energy is everything above 4 kHz: brightness, "air",
   sibilance.
4. The byte math also changed: `3 s × 8000 × 2 × 1 = 48_000 B`.
   You just cut your bandwidth in half and your speech is still
   fine.

**Wider points to check yourself on**

- Why does this matter for a voice pipeline? Phones still use
  8 kHz. Twilio gives you μ-law 8 kHz. Knowing the ceiling helps
  you debug "my STT is fine on my laptop but worse on the phone."
- A higher sample rate is not always better — it's pure bandwidth
  cost for no intelligibility gain on speech.

## Self-check

You should now be able to predict — without running the code —
roughly how an utterance will sound at 4 kHz, 8 kHz, 16 kHz, and
44.1 kHz, and explain the difference in one sentence each.
