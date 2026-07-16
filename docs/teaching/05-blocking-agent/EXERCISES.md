# Chapter 5 — Exercises

<!-- BEGIN auto:navigation -->
[← Back to chapter](./README.md) · [Ladder index](../) · [Progress worksheet](../PROGRESS.md) · [Chapter 6 — Streaming Agent + Sentence TTS →](../06-streaming-agent/)
<!-- END auto:navigation -->

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

## 1. Three controlled experiments on the same question

**Task.** Ask the bot *"What is the capital of France?"* under
each condition below, and for each one record which of the three
`turn.gap` sub-spans changed:

| Change                                         | Which span changes? | By how much? |
|------------------------------------------------|---------------------|--------------|
| `MODEL = "gpt-4o-mini"` → `"gpt-4o"`           | ?                   | ?            |
| Add system prompt: *"Answer in one word."*     | ?                   | ?            |
| Insert `await asyncio.sleep(0.5)` inside agent | ?                   | ?            |

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 3</summary>

Switching to `gpt-4o` mostly affects the `agent_ms` span — same
   prompt, slower model. The other two stay put.

</details>

<details markdown="1">
<summary>Hint 2 of 3</summary>

The one-word system prompt shrinks `tts_enqueue_ms` (less text
   to synthesise) and also shrinks `agent_ms` slightly (fewer tokens
   to generate). `tts_ms` may move much less because provider startup
   and the first audio chunk dominate it. `stt_to_agent_ms` is unchanged.

</details>

<details markdown="1">
<summary>Hint 3 of 3</summary>

`asyncio.sleep(0.5)` adds exactly 500 ms to `agent_ms`. Use
   this to verify your understanding of which code lives in which
   span.

</details>
<!-- END auto:exercise-hints -->

## 2. The "uh, what was I going to say" exercise

**Task.** Have a friend (or yourself) ask the bot a 3-second
question, then sit silently for the latency gap, then naturally
continue the conversation. Time the full transaction with a
stopwatch.

Now write down: **how many seconds was the human standing in the
room holding their breath?**

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 3</summary>

3 s question + ~3 s `turn.gap` + however long the bot speaks =
   6+ seconds of awkward silence per turn.

</details>

<details markdown="1">
<summary>Hint 2 of 3</summary>

Voice users budget around 100-300 ms for turn-taking. We're an
   order of magnitude over budget — that's why this feels so off.

</details>

<details markdown="1">
<summary>Hint 3 of 3</summary>

There's no software fix here. The fix is structural: don't make
   the user wait for the entire `agent.complete` event before
   starting to speak. That's chapter 6.

</details>
<!-- END auto:exercise-hints -->

## 3. Decompose somebody else's gap

**Task.** Find a voice product you use (any vendor). Time the gap
between when you stop talking and when it starts talking. Try to
attribute the time to the three sub-spans:

- Stop-talking → STT-final (this is the smart-turn signal of
  chapter 8)
- STT-final → first audio (this is `agent_ms` + `tts_ms` from
  this chapter)
- First audio → end-of-greeting (just speech duration; not in
  scope)

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 2</summary>

The first sub-span is hard to measure from the outside (you
   don't see the STT final). A rough proxy: the moment the
   wave-form indicator (if present) drops.

</details>

<details markdown="1">
<summary>Hint 2 of 2</summary>

Good products run the gap at 600-900 ms total. Bad ones 2-4 s.
   Excellent ones (OpenAI Realtime API, smart-turn-equipped
   pipelines) target 300-500 ms.

</details>
<!-- END auto:exercise-hints -->

## 4. Diagnose a missing first-audio milestone

**Task.** Run the provider-free outcome probe:

```bash
uv run python docs/teaching/05-blocking-agent/tts_outcome_probe.py
```

For each case, explain why `gap_available` has its value. Then delete the
accepted/rejected fields from `stage.tts.execute` and `turn.gap` in a scratch
copy. Which two root causes become observationally identical?

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 5</summary>

`mixed` has an accepted chunk, so `FirstAudioProbe` captures the milestone
   and the software turn gap is measurable.

</details>

<details markdown="1">
<summary>Hint 2 of 5</summary>

`all_rejected` proves TTS produced two chunks and the transport dropped
   both. This is not a TTS-empty response.

</details>

<details markdown="1">
<summary>Hint 3 of 5</summary>

`no_audio` has zero accepted and zero rejected chunks. Only this case
   supports the narrower “TTS produced no audio” diagnosis.

</details>

<details markdown="1">
<summary>Hint 4 of 5</summary>

An accepted count with no first-audio timestamp would indicate broken
   instrumentation, so the runtime prints a fourth defensive outcome even
   though the scripted probe cannot produce it through `FirstAudioProbe`.

</details>

<details markdown="1">
<summary>Hint 5 of 5</summary>

None of these counts proves playback. Chapter 9 adds delivery-progress
   evidence later.

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
>
> If an answer needs notes, reopen only the section that owns the weak concept,
> correct your explanation, close it, and retry. Continue only when you can answer
> without looking.
<!-- END auto:self-check-protocol -->

1. What are the three first-audio sub-gaps in order, and which measured
   milestones bound each one?
2. Which sub-gap does chapter 6 reduce, which does chapter 8 reduce, and what
   evidence shows that both fixes matter?
3. Which accepted/rejected counts distinguish no synthesized audio from audio
   rejected before its first accepted chunk?

<!-- BEGIN auto:exercise-completion -->
---
Self-check complete? Prepare the cumulative spine, then replay it through this chapter:

```bash
uv sync --extra quickstart --group dev
uv run python docs/teaching/offline_spine.py --run --through 5 --jobs 4 --show-evidence
```

- [Review the chapter narrative](./README.md)
- [Update the progress worksheet](../PROGRESS.md)
- [Continue to Chapter 6 — Streaming Agent + Sentence TTS →](../06-streaming-agent/)
<!-- END auto:exercise-completion -->
