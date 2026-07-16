# Chapter 11 — Exercises

[← Back to chapter](./README.md) · [Ladder index](../)

The README's three planted-bug investigations *are* the chapter's
core exercises. This file adds two follow-ups for once you've
worked through them.

## 1. Three "what could go wrong" hypotheses

**Task.** Pick any bundle from your own `runs/` directories
(chapter 9 is the richest). Without looking at the source code,
write down **three** things that *could* go wrong on a flaky day,
and for each one, name the record you'd query first.

**Hints**

1. "Agent stalled" → look for the gap between `agent.first_token`
   and the first `stage.tts.execute`. Anything > 1 s is
   suspicious.
2. "STT misheard" → look for short or empty `stt.final` text
   followed by a confused next turn. Pair with `stt.partial`
   sequence to see if the model wavered.
3. "Network blip during streaming" → look for `ws.reconnect.*`
   records (chapter 11's fixture-only events; production emits
   real ones). Gaps in record sequence numbers also flag
   in-flight failures.
4. "Smart-turn fired wrong" → look for `smart_turn.classify`
   records where `confirmed=True` but the next event sequence
   shows the user continued speaking immediately after.
5. "Memory pressure" → look for gaps in `t_ms` that don't line up
   with anything in the audio. GC pauses or thread-pool stalls
   show up as record-to-record gaps with no work between.

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

**Hints**

1. The best planted bugs are the ones where the *output looks
   wrong but the journal looks "fine"*. That's the hardest debug
   shape and the one the journal is built for.
2. After the planted bug, also write a one-paragraph
   "investigation guide" that points at the records you'd query
   first. Compare with your classmate's actual investigation
   path.
3. Production bugs in voice pipelines almost always look like
   this: the audio sounds off, the logs are silent, only the
   journal tells you what actually happened.

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

**Hints**

1. `filter_by_stage` is convenient for "show me everything in one
   stage". `filter_by_turn` groups records causally — important
   on multi-turn bundles.
2. `lookup_by_sequence(N)` is the bounded random-access primitive
   on a live journal backend — useful when one record references
   another by sequence number.
3. `filter_by_stage` and `filter_by_turn` return materialized lists.
   Stage filtering scans the journal because `stage` lives inside
   record data; do not mistake the read-only surface for a lazy one.

## Self-check

You should be able to: (a) open a bundle from any chapter and
identify the dominant time-cost without reading the chapter's
README, (b) describe in one sentence what each of the three
planted bugs was about, and (c) name the `JournalView` query you'd
reach for first on a multi-turn bundle.
