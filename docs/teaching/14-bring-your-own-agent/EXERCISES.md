# Chapter 14 — Exercises

## 1. Demote yourself to shallow mode

**Task.** Change `on_user_turn` to `async def on_user_turn(self,
text)` — drop `recorder` and `cancel_token`. You've just demoted
to shallow mode. Run the script and try to interrupt the bot
mid-sentence. What do you see in the journal?

**Hints**

1. Shallow mode means the bridge can't see your workflow's
   internals, so it has no way to apply cooperative cancellation
   mid-turn. Your generator runs to completion, *then* the next
   user turn begins.
2. Look for `ControlSignalRecord` with
   `cause="shallow_mode_downgrade"` in the journal. The runtime
   writes this exactly when the bridge would have cancelled but
   couldn't.
3. The audible symptom: you start talking, the bot finishes its
   sentence anyway, *then* hears you. End-of-turn cancellation
   is the fallback — better than nothing, much worse than deep
   mode.
4. The fix is to add back the `recorder` parameter. The bridge
   inspects your function signature with `inspect.signature` to
   decide which mode to use.

## 2. Custom action with a custom executor

**Task.** Add a `CustomAction(name="play_chime", data={"freq":
440})` and a 10-line executor that prints the action. Trigger it
from the workflow. How does the journal record the action's
lifecycle?

**Hints**

1. Executor protocol:

   ```python
   class ChimeExecutor:
       def supports(self, action) -> bool:
           return isinstance(action, CustomAction) and action.name == "play_chime"

       async def execute(self, action, context) -> None:
           print(f"BEEP at {action.data['freq']} Hz")
   ```

   Register it via `EasyConfig(session_action_executors=[ChimeExecutor()])`.

2. The journal records `session_action.enqueued` when the workflow
   calls `actions.enqueue(...)`, then `session_action.dispatched`
   when the session drains the queue, then `session_action.executed`
   or `.failed` per the executor's return.
3. The session dispatches to the *first* executor that returns
   `True` from `supports()`. Order matters: if you register
   `CoreSessionActionExecutor` after `ChimeExecutor`, end-of-call
   actions still work, but a `CustomAction` with name="end_call"
   would claim the wrong executor. Use disjoint `name` namespaces.

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
   advertise `supports_ssml = True` (grep `src/easycat/tts/*.py`).
   That means the session's `_tts_scheduler` calls `strip_ssml_tags`
   on any SSML payload before sending it to the provider, and
   journals an `ssml_downgraded: true` record. With today's
   providers you will hear the same flat reading whether the
   `PauseProcessor` is registered or not. The exercise is really
   "watch the journal record the downgrade."
3. To actually hear pauses, you'd need to plug in a TTS provider
   that returns `True` from `supports_ssml` and accepts SSML break
   tags. None ship with EasyCat as of this writing — a custom
   provider via `create_tts_provider` is the path. File this as a
   capability you'd add when a customer needs it.
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
