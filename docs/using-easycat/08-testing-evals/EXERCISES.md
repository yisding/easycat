# Chapter 8 Exercises

All exercises begin offline and use no provider credentials.

## 1. Run the passing suite

```bash
uv run python docs/using-easycat/08-testing-evals/main.py
```

For each case, identify the input, oracle, returned `TurnResult`, journal-backed
completion check, and latency sample.

## 2. Read a useful failure

Run the impossible latency budget:

```bash
uv run python docs/using-easycat/08-testing-evals/main.py --max-ms 0
```

Find the observed percentile, budget, sample count, and supporting P50/P95/P99
values in the assertion message. A useful gate says what regressed; it does not
return only `False`.

Restore the default 5000ms budget for later exercises.

## 3. Add one case without teaching the agent the answer

Add an `EvalCase` for an unsupported question. Its oracle should require the
fallback response with either exact match or regex.

Do not add a one-off production branch that checks the complete test prompt.
The agent should implement a general routing rule; the case should only observe
it.

## 4. Make the content oracle fail

Change the refund regex from `order number` to `tracking number` and run the
suite. Read the actual `agent_final` text in the failure. Restore the original
pattern.

Then remove `assert_turn_completed` and explain which hang/regression class the
remaining content assertion would fail to detect.

## 5. Turn the cases into pytest

Create a temporary `test_support_eval.py` outside this chapter and parametrize
`CASES`. Use `asyncio.run(run_text_turn(...))` as the scaffold templates do, or
use your project's async pytest convention.

Run:

```bash
uv run pytest PATH_TO_TEST
```

Add stable case IDs so CI output names the failed behavior rather than only a
tuple index.

## 6. Promote a bundle regression

Generate chapter 7's baseline if needed, then load it:

```python
from easycat.debug.testing import assert_no_error, load_bundle

bundle = load_bundle(".easycat/tutorial/ch07/baseline.bundle")
assert_no_error(bundle)
```

Add an assertion over the first `agent_final` record. Before checking a bundle
into a repository, review transcript/tool/artifact contents and minimize PII.

## 7. Replace the judge stub thoughtfully

Modify `deterministic_judge` to return `relevance=3` for the refund transcript.
Confirm `assert_llm_judge` names the failing dimension.

For a real judge experiment, use a fixed rubric/model on a reviewed dataset and
compare its verdicts with human labels. Do not hide nondeterministic live judge
calls inside the fast offline unit lane.

## 8. Separate latency reporting from gating

Report a chapter-7 bundle:

```bash
uv run easycat latency .easycat/tutorial/ch07/baseline.bundle --json
```

Then compare the roles of:

- reporting a bundle percentile;
- failing `assert_latency` in a deterministic test;
- probing one live smoke sample;
- enforcing tail budgets and a compatible baseline in a live sweep.

Choose where each belongs in pull-request, scheduled, and release workflows.

## Done when

You can explain:

- which real EasyCat machinery an offline text turn covers;
- why behavior cases keep prompts and oracles separate;
- how the same assertions work on `TurnResult` and `RunBundle`;
- why a judge complements rather than replaces deterministic/human checks;
- which latency surfaces report and which ones fail CI.
