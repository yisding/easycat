# Chapter 6 — Exercises

<!-- BEGIN auto:navigation -->
[← Back to chapter](./README.md) · [Ladder index](../) · [Progress worksheet](../PROGRESS.md) · [Chapter 7 — Tools, Mid-stream →](../07-tools/)
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

## 1. Isolate which knob buys you what

**Task.** Change `MODEL = "gpt-5.6-luna"` to `"gpt-5.6-sol"`. Re-run.
For each bundle, run:

```bash
uv run python docs/teaching/06-streaming-agent/measure_start.py PATH
```

Compare `stt_final_to_first_token_ms`,
`first_token_to_first_audio_ms`, the total
`stt_final_to_first_audio_ms`, and `sentence_tts_ms`.

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 4</summary>

A different model tier primarily changes
   `stt_final_to_first_token_ms`: that is provider/model startup
   before the first non-empty delta. Record the direction and size
   instead of assuming either is universal.

</details>

<details markdown="1">
<summary>Hint 2 of 4</summary>

`first_token_to_first_audio_ms` begins **after** model startup. It
   covers accumulating a complete speakable sentence plus synthesising
   and accepting its first audio. Response wording may move it, but the
   model's time-to-first-token is not inside this interval.

</details>

<details markdown="1">
<summary>Hint 3 of 4</summary>

The total `stt_final_to_first_audio_ms` is the actual software
   start-of-reply metric and should equal the first two intervals when
   all three milestones exist.

</details>

<details markdown="1">
<summary>Hint 4 of 4</summary>

`sentence_tts_ms` shows downstream synthesis cost per sentence.
   The source's concurrent producer/consumer structure creates overlap;
   these closed composite durations alone do not prove the overlap.

</details>
<!-- END auto:exercise-hints -->

## 2. Break markdown stripping deliberately

**Task.** Remove the `strip_markdown(ready)` call so the raw
markdown reaches TTS. Ask the bot for a *bulleted list of three
things*. Listen.

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 3</summary>

You will hear *"asterisk asterisk bold asterisk asterisk"* or
   *"hyphen item one"*. This is the single most common voice-bot
   shipping bug.

</details>

<details markdown="1">
<summary>Hint 2 of 3</summary>

This chapter keeps no conversation history: each turn sends only
   the system prompt and the current user text. If you add multi-turn
   history, store the unstripped reply there and strip only the copy
   bound for TTS. Why? (Because the LLM next turn benefits from the
   structured prior; the user does not.)

</details>

<details markdown="1">
<summary>Hint 3 of 3</summary>

Production wires this through
   `easycat.llm_output_processing.MarkdownStripProcessor` (chapter
   14) — exact same logic, plumbed through `output_processors`.

</details>
<!-- END auto:exercise-hints -->

## 3. Find where the audio backlog bites

**Task.** The `asyncio.Queue` between the agent and the drain holds
only text sentences, but audio has to wait somewhere too. Predict what
happens when the bot answers a long question ("explain the entire
history of Rome in detail") whose audio runs well past 10 seconds.
Then run it and check `rejected_chunks` on each `stage.tts.execute`
record and `tts_rejected_chunks` on `turn.gap`.

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 3</summary>

`transport.send_audio` returns as soon as the chunk is
   *queued*, not when it plays. Sentence N+1 finishes synth long
   before sentence N finishes playing.

</details>

<details markdown="1">
<summary>Hint 2 of 3</summary>

TTS synthesizes faster than real time, so the backlog lands in
   `LocalTransport`'s playback queue, which is capped at
   `max_pending_out_chunks` (about 10 s of 20 ms frames). Once it is
   full, `send_audio` drops the frames and returns `False`: memory
   plateaus, and the overflow shows up as rejected chunks and clipped
   audio. Production's outbound `BoundedAudioQueue` uses `DROP_NEWEST`
   so only the tail is trimmed, never the start of the utterance.

</details>

<details markdown="1">
<summary>Hint 3 of 3</summary>

This is exactly the failure mode chapter 9c's interruption
   estimator runs into: "what's in the queue" ≠ "what the user
   heard" because the queue holds future audio.

</details>
<!-- END auto:exercise-hints -->

## 4. Cancel between ownership scopes

**Task.** Run the provider-free lifecycle probe:

```bash
uv run python docs/teaching/06-streaming-agent/voice_stack_cleanup_probe.py
```

Before looking at the JSON, predict the event order for a normal turn,
a cancellation after `stt.start`, and a failure in `tts.close`.

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 3</summary>

The per-turn STT must record `stt.end` before `stt.close` in both
   the normal and cancelled paths. Ending a protocol stream is not the
   same operation as releasing its provider.

</details>

<details markdown="1">
<summary>Hint 2 of 3</summary>

The process-wide resources unwind in reverse registration order:
   TTS, client, VAD, then transport.

</details>

<details markdown="1">
<summary>Hint 3 of 3</summary>

`cleanup_failure.events` should still include all four process-wide
   callbacks. Replace `AsyncExitStack` in the probe with four plain
   sequential `await` calls and observe which events disappear when
   `tts.close` raises.

</details>
<!-- END auto:exercise-hints -->

## 5. Follow delivery across sentence boundaries

**Task.** Run the streamed delivery probe:

```bash
uv run python docs/teaching/06-streaming-agent/tts_delivery_probe.py
```

Then change the mixed case from `[False, True]` to `[True, False]` and
predict which fields change before re-running it.

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 3</summary>

Both mixed cases keep the same reply-wide accepted and rejected
   totals, but the per-sentence counts move.

</details>

<details markdown="1">
<summary>Hint 2 of 3</summary>

With `[False, True]`, the first accepted chunk may arrive in a later
   sentence. The turn gap must still end at that acceptance, not at the
   first rejected offer.

</details>

<details markdown="1">
<summary>Hint 3 of 3</summary>

Compare `all_chunks_rejected` with `no_chunks_produced`. Both lack a
   first-audio gap, but only one proves that TTS produced audio.

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

1. Can you draw the streaming architecture from memory and label the evidence
   boundary between each stage?
2. Why are sentences—not tokens or whole paragraphs—the TTS handoff unit, and
   which latency or prosody observation supports that choice?
3. Which resources belong to one STT turn and which belong to the process-wide
   voice stack, and in what order do they close?
4. Which outcome fields distinguish an empty streamed TTS response from
   transport rejection?
5. Can you name one `consume_agent_stream` parameter and the failure scenario
   the toy ignores that it defends against?

<!-- BEGIN auto:exercise-completion -->
---
Self-check complete? Prepare the cumulative spine, then replay it through this chapter:

```bash
uv sync --extra quickstart --group dev
uv run python docs/teaching/offline_spine.py --run --through 6 --jobs 4 --show-evidence
```

- [Review the chapter narrative](./README.md)
- [Update the progress worksheet](../PROGRESS.md)
- [Continue to Chapter 7 — Tools, Mid-stream →](../07-tools/)
<!-- END auto:exercise-completion -->
