# Chapter 14 — Exercises

<!-- BEGIN auto:navigation -->
[← Chapter narrative](./README.md) · [Teaching ladder](../) · [Chapter 15 — Operate in production →](../15-operate-in-production/)
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
<details markdown="1">
<summary>Reveal hints after your first attempt</summary>

**Hints**

1. The bridge still receives the session's cancel token in shallow
   mode. For a streaming workflow it stops forwarding chunks once it
   observes cancellation, and the session cancels queued TTS audio.
   Your workflow cannot see that token itself, though, so a blocking or
   non-cooperative operation may keep doing work until the runtime task
   is cancelled.
2. Look for `assistant_interruption_notified` with `notified: false`.
   That means audio cancellation happened, but the bridge could not
   reconcile the opaque workflow's state. The original barge-in remains
   visible as `control_signal_cause` with `cause: barge_in`; there is no
   separate shallow-mode control signal.
3. Inspect `MyWorkflow._history` after the interruption. Without an
   interruption hook it can retain generated text the caller did not
   hear, even though playback stopped. That state mismatch — not a
   promise that the full sentence stays audible — is shallow mode's
   important limitation.
4. Add `recorder` back to select deep mode and keep `cancel_token` so
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
<details markdown="1">
<summary>Reveal hints after your first attempt</summary>

**Hints**

1. Here is the complete executor contract. Merge the two config fields
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

2. Enqueueing only changes the in-memory queue. When the session drains
   that queue, the journal records `session_action_requested`, then
   `session_action_started`, then `session_action_completed`. If
   `execute(...)` raises, the last record is `session_action_failed`
   instead. The completed record includes the result metadata.
3. The session dispatches to the *first* executor that returns
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
<details markdown="1">
<summary>Reveal hints after your first attempt</summary>

**Hints**

1. `default_pronunciation_processors()` always adds a
   `PauseProcessor` for phone-number-shaped digit groups. It adds a
   `PhoneticReplacementProcessor` only when you pass a non-empty
   `name_pronunciations` mapping, as `build_output_processors()` does.
2. The scheduler writes one `tts_payload_prepared` record per payload,
   not a family of `output_processor.*` records. Its `processors` list
   tells you which processors were configured; `changed`,
   `original_format`, `prepared_format`, and `ssml_downgraded` describe
   the combined result. It does not retain every intermediate string or
   attribute a change to one processor.
3. None of the four bundled TTS providers currently accepts SSML.
   `PauseProcessor` first inserts exact 120 ms `<break>` tags between
   the phone-number digits, then the scheduler strips those tags before
   calling the provider. The provider-ready plain text keeps the digits
   separated by spaces, so pronunciation may change, but the exact
   120 ms timing guarantee is gone. Do not expect identical audio or a
   precise pause from that fallback.
4. For provider-neutral pause cues, try
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
<details markdown="1">
<summary>Reveal hints after your first attempt</summary>

**Hints**

1. The session closes providers and transports it constructs from
   `EasyConfig`; the client captured by `MyWorkflow` was created by
   caller code.
2. `GenericWorkflowBridge` delegates turn behavior and state hooks. It
   does not infer ownership from `MyWorkflow.__dict__` or close every
   reachable object.
3. Without an explicit `await client.close()` or async context, there
   is no owner. Restore the outer client scope rather than teaching the
   session to reach into custom workflow internals.
4. Keep postmortem export after the inner session scope. The read-only
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
<details markdown="1">
<summary>Reveal hints after your first attempt</summary>

**Hints**

1. `GenericWorkflowBridge.snapshot_state()` exposes generic bridge metadata
   plus the dictionary returned by `MyWorkflow.snapshot_state()`.
2. Interruption artifacts use that explicit workflow dictionary as their
   payload. If the hook is absent, the generic bridge falls back to a much
   broader `workflow.__dict__` serialization.
3. The chapter's hook deliberately reports counts, roles, and booleans. Those
   values are enough to see history growth and a pending action without
   copying conversation content or caller-owned object representations.
4. Treat the explicit dictionary as author-owned persisted data. Temporarily
   add `"api_key": "demo"` and rerun the probe: the field is visible. Remove
   it immediately, and never use a real credential for this experiment.

</details>
<!-- END auto:exercise-hints -->

## Self-check

You should be able to: (a) explain the difference between deep and
shallow mode in one sentence each, (b) name when to use a tool vs
a session action without re-reading chapter 7, and (c) describe
where in the pipeline output processors run (TTS only? history
too?) without checking the source, and (d) distinguish session-owned
providers from caller-owned workflow dependencies, and (e) explain why a
deep workflow should define an explicit metadata-only state snapshot.

<!-- BEGIN auto:exercise-completion -->
---
Self-check complete? Prepare the cumulative spine, then replay it through this chapter:

```bash
uv sync --extra quickstart --group dev
uv run python docs/teaching/offline_spine.py --run --through 14 --jobs 4 --show-evidence
```

- [Review the chapter narrative](./README.md)
- [Continue to Chapter 15 — Operate in production →](../15-operate-in-production/)
<!-- END auto:exercise-completion -->
