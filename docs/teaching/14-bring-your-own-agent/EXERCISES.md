# Chapter 14 — Exercises

<!-- BEGIN auto:navigation -->
[← Back to chapter](./README.md) · [Ladder index](../) · [Progress worksheet](../PROGRESS.md) · [Chapter 15 — Operate in production →](../15-operate-in-production/)
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

## 1. Demote yourself to shallow mode

**Task.** Change `on_user_turn` to `async def on_user_turn(self,
text)` — drop `recorder` and `cancel_token`. You've just demoted
to shallow mode. Temporarily comment out `apply_interruption` too;
that method is an explicit shallow-mode opt-in. Run the script and
try to interrupt the bot mid-sentence. What stops, and what can no
longer be reconciled?

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 4</summary>

The bridge still receives the session's cancel token in shallow
   mode. For a streaming workflow it stops forwarding chunks once it
   observes cancellation, and the session cancels queued TTS audio.
   Your workflow cannot see that token itself, though, so a blocking or
   non-cooperative operation may keep doing work until the runtime task
   is cancelled.

</details>

<details markdown="1">
<summary>Hint 2 of 4</summary>

Look for `assistant_interruption_notified` with `notified: false`.
   That means audio cancellation happened, but the bridge could not
   reconcile the opaque workflow's state. The original barge-in remains
   visible as `control_signal_cause` with `cause: barge_in`; there is no
   separate shallow-mode control signal.

</details>

<details markdown="1">
<summary>Hint 3 of 4</summary>

Inspect `MyWorkflow._history` after the interruption. Without an
   interruption hook it can retain generated text the caller did not
   hear, even though playback stopped. That state mismatch — not a
   promise that the full sentence stays audible — is shallow mode's
   important limitation.

</details>

<details markdown="1">
<summary>Hint 4 of 4</summary>

Add `recorder` back to select deep mode and keep `cancel_token` so
   the workflow can stop its upstream LLM stream. A stateful workflow
   should also implement `apply_interruption(...)` (as this chapter's
   `MyWorkflow` does) to rewrite private history to the delivered text.
   Alternatively, a shallow workflow may keep that hook as an explicit
   promise that it knows how to reconcile its own opaque state.

</details>
<!-- END auto:exercise-hints -->

## 2. Custom action with a custom executor

**Task.** Add a `CustomAction(name="play_chime", payload={"freq":
440})` and a small executor that prints the action. Trigger it from
the workflow. How does the journal record the action's lifecycle?

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 3</summary>

Here is the complete executor contract. Merge the two config fields
   into the chapter's existing `EasyConfig` rather than creating a second
   config:

   ```python
   from typing import Any

   from easycat import EasyConfig
   from easycat.session.actions import (
       CustomAction,
       SessionAction,
       SessionActionResult,
       SessionActions,
   )


   class ChimeExecutor:
       def supports(self, action: SessionAction) -> bool:
           return isinstance(action, CustomAction) and action.name == "play_chime"

       async def execute(
           self, _session: Any, action: SessionAction
       ) -> SessionActionResult:
           assert isinstance(action, CustomAction)
           frequency = action.payload["freq"]
           print(f"BEEP at {frequency} Hz")
           return SessionActionResult(metadata={"frequency": frequency})


   actions = SessionActions()
   actions.enqueue(CustomAction(name="play_chime", payload={"freq": 440}))
   config = EasyConfig(
       session_actions=actions,
       action_executors=(ChimeExecutor(),),
   )
   ```

</details>

<details markdown="1">
<summary>Hint 2 of 3</summary>

Enqueueing only changes the in-memory queue. When the session drains
   that queue, the journal records `session_action_requested`, then
   `session_action_started`, then `session_action_completed`. If
   `execute(...)` raises, the last record is `session_action_failed`
   instead. The completed record includes the result metadata.

</details>

<details markdown="1">
<summary>Hint 3 of 3</summary>

The session dispatches to the *first* executor that returns
   `True` from `supports()`. Configured executors run before EasyCat's
   built-in `CoreSessionActionExecutor`, so keep each custom executor's
   `supports()` check narrow. The example claims only `CustomAction`
   objects named `play_chime`.

</details>
<!-- END auto:exercise-hints -->

## 3. Watch the pronunciation pipeline at work

**Task.** The chapter now builds its stack with
`default_pronunciation_processors()`. Say *"Call me at
555-867-5309."* After the bundle is written, run the printed
`easycat journal grep ... --query tts_payload_prepared --json`
command. Which transformation survived into the provider-ready
payload, and which guarantee was lost?

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 4</summary>

`default_pronunciation_processors()` always adds a
   `PauseProcessor` for phone-number-shaped digit groups. It adds a
   `PhoneticReplacementProcessor` only when you pass a non-empty
   `name_pronunciations` mapping, as `build_output_processors()` does.

</details>

<details markdown="1">
<summary>Hint 2 of 4</summary>

The scheduler writes one `tts_payload_prepared` record per payload,
   not a family of `output_processor.*` records. Its `processors` list
   tells you which processors were configured; `changed`,
   `original_format`, `prepared_format`, and `ssml_downgraded` describe
   the combined result. It does not retain every intermediate string or
   attribute a change to one processor.

</details>

<details markdown="1">
<summary>Hint 3 of 4</summary>

None of the four bundled TTS providers currently accepts SSML.
   `PauseProcessor` first inserts exact 120 ms `<break>` tags between
   the phone-number digits, then the scheduler strips those tags before
   calling the provider. The provider-ready plain text keeps the digits
   separated by spaces, so pronunciation may change, but the exact
   120 ms timing guarantee is gone. Do not expect identical audio or a
   precise pause from that fallback.

</details>

<details markdown="1">
<summary>Hint 4 of 4</summary>

For provider-neutral pause cues, try
   `PauseProcessor(..., style="ellipsis")`; it stays plain text, but the
   provider still decides the timing. Exact break duration requires a
   provider whose `input_policy` is `TTSInputPolicy.native_ssml()`.
   With such a provider, the record would show `prepared_format: ssml`
   and `ssml_downgraded: false`.

</details>
<!-- END auto:exercise-hints -->

## 4. Move the custom client across the ownership boundary

**Task.** Remove the outer `async with AsyncOpenAI()` block and create
the client with `client = AsyncOpenAI()` instead. Which owner now closes
that caller-owned `AsyncOpenAI` object?

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 4</summary>

The session closes providers and transports it constructs from
   `EasyConfig`; the client captured by `MyWorkflow` was created by
   caller code.

</details>

<details markdown="1">
<summary>Hint 2 of 4</summary>

`GenericWorkflowBridge` delegates turn behavior and state hooks. It
   does not infer ownership from `MyWorkflow.__dict__` or close every
   reachable object.

</details>

<details markdown="1">
<summary>Hint 3 of 4</summary>

Without an explicit `await client.close()` or async context, there
   is no owner. Restore the outer client scope rather than teaching the
   session to reach into custom workflow internals.

</details>

<details markdown="1">
<summary>Hint 4 of 4</summary>

Keep postmortem export after the inner session scope. The read-only
   journal survives session teardown, while the outer client remains
   open until the export finishes.

</details>
<!-- END auto:exercise-hints -->

## 5. Define the workflow-state artifact boundary

**Task.** Run the provider-free state probe:

```bash
uv run python docs/teaching/14-bring-your-own-agent/workflow_state_probe.py
```

Compare `bridge_snapshot.workflow_state` with `artifact_payload`. Why do
neither contain `_client`, `_actions`, prompt text, or user text even though
all four are reachable from `MyWorkflow`?

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 4</summary>

`GenericWorkflowBridge.snapshot_state()` exposes generic bridge metadata
   plus the dictionary returned by `MyWorkflow.snapshot_state()`.

</details>

<details markdown="1">
<summary>Hint 2 of 4</summary>

Interruption artifacts use that explicit workflow dictionary as their
   payload. If the hook is absent, the generic bridge falls back to a much
   broader `workflow.__dict__` serialization.

</details>

<details markdown="1">
<summary>Hint 3 of 4</summary>

The chapter's hook deliberately reports counts, roles, and booleans. Those
   values are enough to see history growth and a pending action without
   copying conversation content or caller-owned object representations.

</details>

<details markdown="1">
<summary>Hint 4 of 4</summary>

Treat the explicit dictionary as author-owned persisted data. Temporarily
   add `"api_key": "demo"` and rerun the probe: the field is visible. Remove
   it immediately, and never use a real credential for this experiment.

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

1. What state and interruption guarantees distinguish deep from shallow bridge
   mode, and which attempt record exposes the difference?
2. When should a workflow yield a tool call versus a session action?
3. Where do output processors run for TTS and history, and which prepared
   payload proves the boundary?
4. Which providers are session-owned and which workflow dependencies remain
   caller-owned after session exit?
5. Why should a deep workflow define an explicit metadata-only state snapshot,
   and which fields are safe to persist?

<!-- BEGIN auto:exercise-completion -->
---
Self-check complete? Prepare the cumulative spine, then replay it through this chapter:

```bash
uv sync --extra quickstart --group dev
uv run python docs/teaching/offline_spine.py --run --through 14 --jobs 4 --show-evidence
```

- [Review the chapter narrative](./README.md)
- [Update the progress worksheet](../PROGRESS.md)
- [Complete the Generalise phase review](../PROGRESS.md#generalise-phase-review)
- [Continue to Chapter 15 — Operate in production →](../15-operate-in-production/)
<!-- END auto:exercise-completion -->
