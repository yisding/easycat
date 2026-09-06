# Chapter 0 — Exercises

<!-- BEGIN auto:navigation -->
[← Back to chapter](./README.md) · [Ladder index](../) · [Progress worksheet](../PROGRESS.md) · [Chapter 1 — Echo →](../01-echo/)
<!-- END auto:navigation -->

One exercise from the chapter README, plus hints if you get stuck.
No worked solutions checked in — the point is that you take a swing
and form your own answer before peeking.

<!-- BEGIN auto:exercise-protocol -->
> **Completion evidence for every task**
>
> 1. **Before hints:** keep your initial prediction or plan.
> 2. **After the attempt:** keep the exact command or change and one observed field,
>    measurement, or behavior.
> 3. **Before moving on:** explain in one sentence why the evidence supports or changes
>    your model.
>
> A task is complete when all three are present. Keep a wrong first answer visible;
> it is evidence to explain after revealing hints, not an answer to rewrite.
<!-- END auto:exercise-protocol -->

## 1. Sweep the sample rate

**Task.** Change `SAMPLE_RATE` at the top of `main.py` to `8000`,
re-record, and play back. Is speech still intelligible? What about
music? (Try humming a song while the recording window is open.)

Then repeat the same utterance at `4000` and `44100`, keeping a note of
what you hear at each rate and the byte count each run reports. The
self-check asks you to compare all four rates — 4 kHz, 8 kHz, the 16 kHz
default, and 44.1 kHz — so record the two extremes here rather than
reasoning about them from memory later.

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 4</summary>

For an ideally band-limited signal, an 8 kHz sample rate can represent
   frequencies below the 4 kHz Nyquist boundary. A real anti-alias filter
   must attenuate frequencies before that boundary; it is not a brick wall.

</details>

<details markdown="1">
<summary>Hint 2 of 4</summary>

Narrowband G.711 telephony intentionally filters speech to roughly
   300–3400 Hz before sampling at 8 kHz. Speech can remain intelligible
   while losing high-frequency detail and cues; “intelligible” does not
   mean “unchanged” or “optimal for every STT model.”

</details>

<details markdown="1">
<summary>Hint 3 of 4</summary>

Listen for lost brightness, “air,” consonant detail, and sibilance.
   Content the front end removes cannot be recovered by later upsampling.

</details>

<details markdown="1">
<summary>Hint 4 of 4</summary>

The byte math also changed: `3 s × 8000 × 2 × 1 = 48_000 B`.
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

</details>
<!-- END auto:exercise-hints -->

## 2. Trace every format boundary

**Task.** Run the provider-free format catalog and find two resampling
boundaries whose input and output are both mono PCM:

```bash
uv run python docs/teaching/00-hello-audio/format_boundaries.py
```

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 5</summary>

`LocalTransport` defaults its capture/playback pipeline to 24 kHz, while
   this chapter's separate raw-`sounddevice` demo explicitly records at 16 kHz.
   Deepgram's streaming STT target also defaults to 16 kHz, and the provider
   adapter resamples at that input boundary when its upstream format differs.

</details>

<details markdown="1">
<summary>Hint 2 of 5</summary>

WebRTC receives and sends 48 kHz media frames, but its default pipeline
   target is 16 kHz. Those are two boundaries of one transport, not a
   contradiction.

</details>

<details markdown="1">
<summary>Hint 3 of 5</summary>

OpenAI returns provider-native 24 kHz PCM. A default WebRTC session first
   normalizes that to its resolved 16 kHz TTS output, then resamples to 48 kHz
   media; a Local session already has a matching 24 kHz target.

</details>

<details markdown="1">
<summary>Hint 4 of 5</summary>

Twilio's wire is 8 kHz μ-law while EasyCat's default internal pipeline
   target is 16 kHz PCM. Decoding and upsampling make the representation
   compatible but do not restore telephone-band frequencies.

</details>

<details markdown="1">
<summary>Hint 5 of 5</summary>

Change `DeepgramSTTConfig(sample_rate=...)` in a scratch copy of the
   probe. You changed one provider-input boundary, not the capture, TTS, or
   transport wire formats.

</details>
<!-- END auto:exercise-hints -->

## 3. Compare raw and resolved TTS formats

**Task.** Run the transport-alignment probe. Explain why “OpenAI TTS
defaults to 24 kHz” and “Twilio resolves OpenAI TTS transport output to
8 kHz” are both true:

```bash
uv run python docs/teaching/00-hello-audio/tts_alignment_probe.py
```

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 6</summary>

A provider config default describes the object before `EasyConfig`
   resolves the whole session. It is not the final transport boundary.

</details>

<details markdown="1">
<summary>Hint 2 of 6</summary>

With alignment enabled, untouched defaults follow the transport:
   Local 24 kHz, WebSocket/WebRTC 16 kHz, and Twilio 8 kHz output.

</details>

<details markdown="1">
<summary>Hint 3 of 6</summary>

ElevenLabs cannot request 8 kHz PCM directly. Its Twilio row therefore
   requests 16 kHz from the provider and exposes 8 kHz transport output
   after the adapter's final resample.

</details>

<details markdown="1">
<summary>Hint 4 of 6</summary>

OpenAI returns fixed 24 kHz PCM even when EasyCat's resolved output target
   is 8 or 16 kHz. `TTSBase` performs that post-provider normalization.

</details>

<details markdown="1">
<summary>Hint 5 of 6</summary>

The `twilio_explicit_16k_preserved` control proves explicit caller intent
   wins over automatic default alignment. Twilio still converts that PCM to
   its 8 kHz μ-law wire format later.

</details>

<details markdown="1">
<summary>Hint 6 of 6</summary>

The `twilio_auto_align_disabled` control keeps the raw 24 kHz default.
   Disable alignment only when you deliberately own the downstream format
   conversion or need a provider-specific output.

</details>
<!-- END auto:exercise-hints -->

## Self-check

<!-- BEGIN auto:self-check-protocol -->
> **Closed-book retrieval gate**
>
> 1. Close the chapter narrative and every hint disclosure.
> 2. Answer every numbered question below from memory, aloud or in writing.
> 3. Support each answer with at least one observed field, measurement, or behavior
>    from your attempt record.
> 4. Mark each answer **pass** or **retry** in your progress record.
>
> If an answer needs notes, reopen only the section that owns the weak concept,
> correct your explanation, close it, and retry. Continue only when every answer
> passes without looking.
<!-- END auto:self-check-protocol -->

1. For the same utterance, how do waveform representation, available bandwidth,
   and transcription behavior differ at 4 kHz, 8 kHz, 16 kHz, and 44.1 kHz,
   and what evidence from both the 4 kHz and 44.1 kHz runs anchors your comparison?
2. When you quote a sample rate, which boundary—wire, provider input, config
   default, pipeline, or media—do you mean, and where is conversion required?
3. Which observed fields distinguish a raw provider config default from the
   transport-resolved session output?

<!-- BEGIN auto:exercise-completion -->
---
Self-check complete? Prepare the cumulative spine, then replay it through this chapter:

```bash
uv sync --extra local --group dev
uv run python docs/teaching/offline_spine.py --run --through 0 --jobs 4 --show-evidence
```

- [Review the chapter narrative](./README.md)
- [Update the progress worksheet](../PROGRESS.md)
- [Continue to Chapter 1 — Echo →](../01-echo/)
<!-- END auto:exercise-completion -->
