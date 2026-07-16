# Chapter 11 — Exercises

<!-- BEGIN auto:navigation -->
[← Back to chapter](./README.md) · [Ladder index](../) · [Progress worksheet](../PROGRESS.md) · [Chapter 12 — Evals + the Latency Budget →](../12-evals-and-latency/)
<!-- END auto:navigation -->

The README's three planted-bug investigations *are* the chapter's
core exercises. This file adds two follow-ups for once you've
worked through them.

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

## 1. Three "what could go wrong" hypotheses

**Task.** Pick any bundle from your own `runs/` directories
(chapter 9 is the richest). Without looking at the source code,
write down **three** things that *could* go wrong on a flaky day,
and for each one, name the record you'd query first.

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 5</summary>

"Agent stalled" → compare `stt.final.data.t_ms` with
   `agent.first_token.data.t_ms`. Anything > 1 s is suspicious; the
   later first-token → audio interval belongs to downstream sentence
   accumulation and TTS.

</details>

<details markdown="1">
<summary>Hint 2 of 5</summary>

"STT misheard" → look for short or empty `stt.final` text
   followed by a confused next turn. Pair with `stt.partial`
   sequence to see if the model wavered.

</details>

<details markdown="1">
<summary>Hint 3 of 5</summary>

"Network blip during streaming" → look for `ws.reconnect.*`
   records (chapter 11's fixture-only events; production emits
   real ones). A filtered sequence gap is expected, and an unfiltered
   gap can reflect bounded retention or incomplete export; verify
   coverage before treating it as an in-flight failure.

</details>

<details markdown="1">
<summary>Hint 4 of 5</summary>

"Smart-turn fired wrong" → look for `smart_turn.classify`
   records where `confirmed=True` but the next event sequence
   shows the user continued speaking immediately after.

</details>

<details markdown="1">
<summary>Hint 5 of 5</summary>

"Memory pressure" → look for gaps in `t_ms` that don't line up
   with anything in the audio. GC pauses or thread-pool stalls
   show up as record-to-record gaps with no work between.

</details>
<!-- END auto:exercise-hints -->

## 2. Plant your own bug

**Task.** Modify chapter 9c's `estimate.py` to introduce a real
bug — your choice. Possible bugs:

- Forget to call `await transport.clear_audio()` on barge-in.
- Ignore `send_audio()`'s return value and increment
  `bytes_accepted` for rejected chunks too.
- Set the chars-per-second constant to 50 (way too high).
- Skip the `cancel.cancel()` call so the agent keeps streaming
  text the user never hears.

Dump the bundle. Then: can a classmate (or your future self
tomorrow morning) find the bug by reading only the bundle?

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 3</summary>

The best planted bugs are the ones where the *output looks
   wrong but the journal looks "fine"*. That's the hardest debug
   shape and the one the journal is built for.

</details>

<details markdown="1">
<summary>Hint 2 of 3</summary>

After the planted bug, also write a one-paragraph
   "investigation guide" that points at the records you'd query
   first. Compare with your classmate's actual investigation
   path.

</details>

<details markdown="1">
<summary>Hint 3 of 3</summary>

Production bugs in voice pipelines almost always look like
   this: the audio sounds off, the logs are silent, only the
   journal tells you what actually happened.

</details>
<!-- END auto:exercise-hints -->

## 3. Compare bundle and live-journal queries

**Task.** Use `investigate.py --turn ch11-bug03-turn-1`, then write
the equivalent query for a live session's `JournalView`. This is an
API-shape comparison on paper; the chapter remains fully offline.

```python
# Offline: load_bundle(...) returns a RunBundle; records are dicts.
for r in bundle.filter_by_turn("ch11-bug03-turn-1"):
    print(r["name"])

# Live: session.journal is a JournalView; records are JournalRecord objects.
for r in session.journal.filter_by_turn(turn_id):
    print(r.name)
```

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 3</summary>

`filter_by_stage` is convenient for "show me everything in one
   stage". `filter_by_turn` groups records causally — important
   on multi-turn bundles.

</details>

<details markdown="1">
<summary>Hint 2 of 3</summary>

`lookup_by_sequence(N)` is the bounded random-access primitive
   on a live journal backend — useful when one record references
   another by sequence number.

</details>

<details markdown="1">
<summary>Hint 3 of 3</summary>

`filter_by_stage` and `filter_by_turn` return materialized lists.
   Stage filtering scans the journal because `stage` lives inside
   record data; do not mistake the read-only surface for a lazy one.

</details>
<!-- END auto:exercise-hints -->

## 4. Prove an empty query means what you think

**Task.** Run the query coverage probe, then compare a misspelled turn
with a valid-but-impossible filter intersection:

```bash
uv run python docs/teaching/11-journal/query_coverage_probe.py

uv run python docs/teaching/11-journal/investigate.py \
  docs/teaching/11-journal/bundles/bug_03_ghost_interruption.bundle \
  --turn typo --require-match
```

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 4</summary>

A zero marginal count means that one filter is invalid for the
   bundle. Non-zero marginals plus zero combined matches mean the
   intersection—not the individual values—is empty.

</details>

<details markdown="1">
<summary>Hint 2 of 4</summary>

Leave off `--require-match` when absence itself is a legitimate
   interactive finding. Add it in automation so a typo cannot pass.

</details>

<details markdown="1">
<summary>Hint 3 of 4</summary>

`--limit` changes presentation, not match coverage. The CLI reports
   the full match count and only prints a truncation line when hidden
   matches really exist.

</details>

<details markdown="1">
<summary>Hint 4 of 4</summary>

Do not infer dropped journal records from a filtered sequence gap;
   inspect the unfiltered source and its retention/export contract.

</details>
<!-- END auto:exercise-hints -->

## 5. Find the payload-schema boundary

**Task.** Run the provider-free payload probe, then explain which types
belong to the journal envelope and which belong to one record emitter:

```bash
uv run python docs/teaching/11-journal/payload_schema_probe.py
```

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 4</summary>

`JournalRecord` declares stable envelope fields, including integer
   `sequence`, string `session_id` / `name`, `JournalRecordKind`, and
   `data: dict[str, Any]`.

</details>

<details markdown="1">
<summary>Hint 2 of 4</summary>

`dict[str, Any]` does not promise that `data["t_ms"]` is numeric. The
   journal preserves the malformed string exactly as it was appended.

</details>

<details markdown="1">
<summary>Hint 3 of 4</summary>

Record emitters own payload schemas. A consumer that drives automation
   must validate the fields and domain constraints it depends on—for this
   metric, reject strings, booleans, infinity, and NaN.

</details>

<details markdown="1">
<summary>Hint 4 of 4</summary>

JSON avoids regex parsing, but serialization alone is not schema
   validation. Use one stable schema per record name and version intentional
   changes when downstream consumers share that contract.

</details>
<!-- END auto:exercise-hints -->

## 6. Recover a session cause hidden by a turn query

**Task.** Run the session-context probe, then reproduce its audio-only
query with the CLI:

```bash
uv run python docs/teaching/11-journal/session_context_probe.py

uv run python docs/teaching/11-journal/investigate.py \
  docs/teaching/11-journal/bundles/bug_03_ghost_interruption.bundle \
  --turn ch11-bug03-turn-2 --stage audio --include-session-context
```

Explain why sequence 1 belongs in the diagnostic context but not in the
strict turn reconstruction.

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 5</summary>

`filter_by_turn` returns sequences 8–13 for turn 2. Sequence 1 has
   `turn_id=None`, so strict isolation correctly excludes it.

</details>

<details markdown="1">
<summary>Hint 2 of 5</summary>

`audio.config.data["aec"] == "off"` is session configuration that
   explains false barge-in events across both turns.

</details>

<details markdown="1">
<summary>Hint 3 of 5</summary>

The context join uses session IDs discovered from the target turn.
   “Unscoped” does not mean “global”: records from another session must
   stay excluded.

</details>

<details markdown="1">
<summary>Hint 4 of 5</summary>

`--include-session-context` requires `--turn`; otherwise there is no
   target session to join safely.

</details>

<details markdown="1">
<summary>Hint 5 of 5</summary>

Use strict turn scope to reconstruct causality. Add same-session
   context only when investigating configuration or lifecycle causes.

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

1. Which records let you identify an unfamiliar bundle's dominant time cost
   without its README?
2. What causal defect produced each of the three planted-bug symptoms?
3. Which `JournalView` query would you use first on a multi-turn bundle, and
   which result from your attempt justifies that choice?
4. How do marginal counts distinguish a real absence, a typo, and an empty
   filter intersection?
5. Which fields belong to the typed journal envelope and which remain
   emitter-defined payload schema?
6. When does a strict turn query need same-session unscoped context, and which
   joined record supplies it?

<!-- BEGIN auto:exercise-completion -->
---
Self-check complete? Prepare the cumulative spine, then replay it through this chapter:

```bash
uv sync --extra quickstart --group dev
uv run python docs/teaching/offline_spine.py --run --through 11 --jobs 4 --show-evidence
```

- [Review the chapter narrative](./README.md)
- [Update the progress worksheet](../PROGRESS.md)
- [Continue to Chapter 12 — Evals + the Latency Budget →](../12-evals-and-latency/)
<!-- END auto:exercise-completion -->
