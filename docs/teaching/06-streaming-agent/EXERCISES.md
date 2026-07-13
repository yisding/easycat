# Chapter 6 — Exercises

<!-- BEGIN auto:navigation -->
[← Chapter narrative](./README.md) · [Teaching ladder](../) · [Chapter 7 — Tools, Mid-stream →](../07-tools/)
<!-- END auto:navigation -->

## 1. Isolate which knob buys you what

**Task.** Change `MODEL = "gpt-4o-mini"` to `"gpt-4o"`. Re-run.
For each bundle, run:

```bash
uv run python docs/teaching/06-streaming-agent/measure_start.py PATH
```

Compare `stt_final_to_first_token_ms`,
`first_token_to_first_audio_ms`, the total
`stt_final_to_first_audio_ms`, and `sentence_tts_ms`.

**Hints**

1. A slower model primarily grows `stt_final_to_first_token_ms`:
   that is provider/model startup before the first non-empty delta.
2. `first_token_to_first_audio_ms` begins **after** model startup. It
   covers accumulating a complete speakable sentence plus synthesising
   and accepting its first audio. Response wording may move it, but the
   model's time-to-first-token is not inside this interval.
3. The total `stt_final_to_first_audio_ms` is the actual software
   start-of-reply metric and should equal the first two intervals when
   all three milestones exist.
4. `sentence_tts_ms` shows downstream synthesis cost per sentence.
   The source's concurrent producer/consumer structure creates overlap;
   these closed composite durations alone do not prove the overlap.

## 2. Break markdown stripping deliberately

**Task.** Remove the `strip_markdown(ready)` call so the raw
markdown reaches TTS. Ask the bot for a *bulleted list of three
things*. Listen.

**Hints**

1. You will hear *"asterisk asterisk bold asterisk asterisk"* or
   *"hyphen item one"*. This is the single most common voice-bot
   shipping bug.
2. The agent's history (`messages`) still contains the original
   markdown text — only the TTS pipe gets stripped. Why does the
   chapter wire it this way? (Because the LLM next turn benefits
   from the structured prior; the user does not.)
3. Production wires this through
   `easycat.llm_output_processing.MarkdownStripProcessor` (chapter
   14) — exact same logic, plumbed through `output_processors`.

## 3. Make the unbounded queue bite

**Task.** Have the bot answer a long question ("explain the entire
history of Rome in detail") on a slow speaker — easiest way:
plug in Bluetooth headphones. Watch the per-sentence latency drift
over the answer.

**Hints**

1. `transport.send_audio` returns as soon as the chunk is
   *queued*, not when it plays. Sentence N+1 finishes synth long
   before sentence N finishes playing.
2. Memory usage of the speaker queue rises linearly during the
   answer. Production uses `BoundedAudioQueue` with `DROP_OLDEST`
   to keep this in check during long sessions; the teaching
   version doesn't.
3. This is exactly the failure mode chapter 9c's interruption
   estimator runs into: "what's in the queue" ≠ "what the user
   heard" because the queue holds future audio.

## 4. Cancel between ownership scopes

**Task.** Run the provider-free lifecycle probe:

```bash
uv run python docs/teaching/06-streaming-agent/voice_stack_cleanup_probe.py
```

Before looking at the JSON, predict the event order for a normal turn,
a cancellation after `stt.start`, and a failure in `tts.close`.

**Hints**

1. The per-turn STT must record `stt.end` before `stt.close` in both
   the normal and cancelled paths. Ending a protocol stream is not the
   same operation as releasing its provider.
2. The process-wide resources unwind in reverse registration order:
   TTS, client, VAD, then transport.
3. `cleanup_failure.events` should still include all four process-wide
   callbacks. Replace `AsyncExitStack` in the probe with four plain
   sequential `await` calls and observe which events disappear when
   `tts.close` raises.

## 5. Follow delivery across sentence boundaries

**Task.** Run the streamed delivery probe:

```bash
uv run python docs/teaching/06-streaming-agent/tts_delivery_probe.py
```

Then change the mixed case from `[False, True]` to `[True, False]` and
predict which fields change before re-running it.

**Hints**

1. Both mixed cases keep the same reply-wide accepted and rejected
   totals, but the per-sentence counts move.
2. With `[False, True]`, the first accepted chunk may arrive in a later
   sentence. The turn gap must still end at that acceptance, not at the
   first rejected offer.
3. Compare `all_chunks_rejected` with `no_chunks_produced`. Both lack a
   first-audio gap, but only one proves that TTS produced audio.

## Self-check

You should be able to: (a) draw the architecture diagram from
memory, (b) explain why sentences (not tokens, not paragraphs) are
the right unit, (c) distinguish per-turn STT ownership from the
process-wide voice stack, (d) distinguish an empty streamed TTS
response from transport rejection, and (e) point at the production
`consume_agent_stream` and name one parameter without re-reading
the README.

<!-- BEGIN auto:exercise-completion -->
---
Self-check complete? Prepare the cumulative spine, then replay it through this chapter:

```bash
uv sync --extra quickstart --group dev
uv run python docs/teaching/offline_spine.py --run --through 6 --jobs 4 --show-evidence
```

- [Review the chapter narrative](./README.md)
- [Continue to Chapter 7 — Tools, Mid-stream →](../07-tools/)
<!-- END auto:exercise-completion -->
