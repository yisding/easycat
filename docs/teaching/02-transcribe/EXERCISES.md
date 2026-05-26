# Chapter 2 — Exercises

## 1. Find the moment STT committed to the wrong guess

**Task.** Say a word the STT consistently mishears ("bass" vs
"base", "pear" vs "pair", "their" vs "there"). Re-run
`streaming.py`, then read the bundle and find the exact partial
where the wrong guess stuck. Compare it to the final.

**Hints**

1. The bundle is `runs/*.bundle`. Open it with
   `easycat.debug.testing.load_bundle`.
2. Filter records by `name == "stt.partial"` and
   `name == "stt.final"`. You should see the sequence of guesses
   converging.
3. The interesting case is when the *final* commits to a wrong
   guess — meaning the provider had a better partial at some
   earlier point and threw it away. That's a recall failure (the
   right hypothesis was on the table; the LM-prior overruled it).
4. The opposite case is also interesting: the final is *right* but
   the user heard wrong-looking partials flap by. That's why
   chapter 6 reinforces "never act on partials."

**Wider points to check yourself on**

- Does the OpenAI batch-then-stream pattern make this exercise
  easier or harder? (Easier: all partials cluster at the end so
  the sequence is dense. Harder: the timing is misleading — the
  partials don't reflect when the *audio* was uttered.)
- Try the same exercise on Deepgram (`provider="deepgram"`,
  `DEEPGRAM_API_KEY` required). Mid-speech partials change the
  feel completely.

## 2. Open a bundle in two ways

**Task.** Read the same bundle two ways:

```python
# Way 1: linear iteration
for r in b.records():
    if r["name"] == "stt.partial":
        print(r["data"]["text"])

# Way 2: structured query
view = b.view  # JournalView
for r in view.filter_by_stage("stt"):
    print(r["sequence"], r["data"].get("text"))
```

When does each shape pay off?

**Hints**

1. Linear iteration is good for "I want to see what happened in
   order." Structured query is good for "I want all records of
   one kind, ordered correctly."
2. Chapter 11 leans entirely on the structured query shape because
   real debugging is "all the TTS spans in this turn" not "every
   record from t=0."

## Self-check

You should be able to read any bundle from any chapter from now on
without consulting the README of the chapter that produced it.
