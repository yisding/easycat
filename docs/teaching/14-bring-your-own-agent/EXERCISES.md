# Chapter 14 — Exercises

## 1. Demote yourself to shallow mode

**Task.** Change `on_user_turn` to `async def on_user_turn(self,
text)` — drop `recorder` and `cancel_token`. You've just demoted
to shallow mode. Temporarily comment out `apply_interruption` too;
that method is an explicit shallow-mode opt-in. Run the script and
try to interrupt the bot mid-sentence. What stops, and what can no
longer be reconciled?

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

## 2. Custom action with a custom executor

**Task.** Add a `CustomAction(name="play_chime", payload={"freq":
440})` and a small executor that prints the action. Trigger it from
the workflow. How does the journal record the action's lifecycle?

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

## 3. Watch the pronunciation pipeline at work

**Task.** Register the `default_pronunciation_processors()` stack
and say *"Call me at 555-867-5309."* Open the bundle afterwards and
look at: (a) the `output_processor.*` records (which processor ran,
which strings changed), and (b) any `ssml_downgraded: true` flag the
TTS scheduler emitted because no bundled provider supports SSML
today. The pronunciation pipeline is *wired*; the audible part of
the chain ends one stage short of the speaker for now.

**Hints**

1. `default_pronunciation_processors()` wires
   `PhoneticReplacementProcessor` (fixed-string swaps) and
   `PauseProcessor` (regex-matched `<break>` insertion). The
   default pause pattern targets phone-number-shaped digit groups.
2. **Honesty check.** None of the bundled TTS providers currently
   expose an `input_policy` that accepts SSML natively. That means
   the session's `_tts_scheduler` calls `strip_ssml_tags` on any SSML
   payload before sending it to the provider, and journals an
   `ssml_downgraded: true` record. With today's providers you will
   hear the same flat reading whether the `PauseProcessor` is
   registered or not. The exercise is really "watch the journal record
   the downgrade."
3. To actually hear pauses, you'd need to plug in a TTS provider
   with `TTSInputPolicy.native_ssml()` that accepts SSML break tags.
   None ship with EasyCat as of this writing — a custom provider via
   `create_tts_provider` is the path. File this as a capability you'd
   add when a customer needs it.
4. The PauseProcessor itself is wired correctly — it inserts
   `<break time="...ms"/>` between matched units (see
   `src/easycat/llm_output_processing.py`). The gap is only in
   provider coverage. The journal is the source of truth: grep
   `ssml_downgraded` to see every downgrade.

## Self-check

You should be able to: (a) explain the difference between deep and
shallow mode in one sentence each, (b) name when to use a tool vs
a session action without re-reading chapter 7, and (c) describe
where in the pipeline output processors run (TTS only? history
too?) without checking the source.
