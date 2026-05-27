# Chapter 12 — Exercises

## 1. Find the budget-blower, propose a fix without coding it

**Task.** Run `latency_budget.py` over each of the five turn
bundles. Identify the slowest. Which stage blew its budget?
Propose a fix — model swap, prompt cache, warmer pool, smarter
turn detection — *without implementing it*. The point is
diagnosis.

**Hints**

1. `turn_02_slow_agent.bundle` is the obvious one — `agent` is at
   2100 ms vs 600 ms budget. Possible fixes: smaller model
   (gpt-4o-mini → gpt-4o-nano), prompt caching for the system
   prompt, agent warm-pool (keep one open connection per session
   to avoid TLS handshake).
2. Don't conflate "stage blew its budget" with "stage is the
   biggest absolute cost." A stage at 400 ms vs a 200 ms budget
   is more interesting than a stage at 600 ms vs a 1000 ms
   budget — the first is *drift*, the second is *normal*.
3. The point of a budget isn't to be a timeout. It's a drift
   detector — you set a threshold based on your historical P50
   and alarm when the live number consistently breaks past it.

## 2. Add a `filler_appropriate` rubric dimension

**Task.** Add a `filler_appropriate` dimension to the LLM-judge
rubric. Re-run on the chapter-7 tool-bearing bundles. Does the
judge agree with your ears?

**Hints**

1. The rubric lives in `llm_judge.py`. Extend the system prompt's
   JSON schema with a `filler_appropriate: 1-5` field. The
   judge's evaluation prompt should ask whether filler utterances
   landed at appropriate moments and matched the tool that was
   running.
2. The interesting failure mode is when the bundle is *technically
   correct* (filler played at the right time) but the rubric
   marks it 3/5 because the *text* of the filler is wrong for the
   tool ("Let me check the weather for you" before a *timer* tool
   call).
3. LLM-as-judge is most useful when the rubric dimension is
   text-only — anything that requires audio judgement (prosody,
   pacing, naturalness) is invisible to it.
4. Copy a `tools_*.bundle` from chapter 7's runs/ into chapter
   12's bundles/ for the judge to consume.

## 3. Wire a latency regression test

**Task.** Write a pytest test that fails if any bundle's P95
exceeds 1200 ms. That's the seed of a latency regression suite.

**Hints**

1. `tests/teaching/` doesn't exist yet — start a new test file.
   Use `easycat.debug.testing.load_bundle` to load each fixture
   and the same `turn.gap` extraction as `evals.py`.
2. Hard-coded thresholds are fine for the teaching version. For
   production, you'd compare against a baseline file checked into
   the repo and require N standard deviations of regression
   before failing.
3. The five chapter-12 fixtures include `turn_02_slow_agent`
   which is *deliberately* 2900 ms — your test should flag it.
   That's the right behavior: the test catches the slowdown the
   fixture was built to represent.
4. Bonus: also test that the golden WER bundles produce the
   numbers their filenames advertise. That's a regression test
   for the WER pipeline itself.

## 4. (Bonus) Build a real eval set

**Task.** Record 20 of your own chapter-6 or chapter-10 turns,
hand-type the reference transcripts into a CSV, and run
`evals.py` against the directory.

**Hints**

1. Real numbers feel different. P95 over 5 bundles is noisy; over
   20+ it stabilizes. WER over 20 utterances will produce a
   number you can actually trust to ±2%.
2. Use a mix of clean and adversarial inputs (whisper, fast
   speech, accented speech, background TV) to stress different
   stages.
3. This is the unglamorous part of voice eval: building a
   ground-truth set. Production teams maintain hundreds to
   thousands of these. The five fixtures here are training
   wheels.

## Self-check

You should be able to: (a) read a bundle and within 30 seconds
say "this turn's bottleneck was X", (b) explain why F1 over
TP/FP/FN/TN is the right shape for barge-in (rather than raw
accuracy), and (c) describe one regression each of the four
metrics catches that the others miss.
