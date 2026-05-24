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

## 3. Hear the pronunciation pipeline at work

**Task.** Register the `default_pronunciation_processors()` stack
and say *"Call me at 555-867-5309."* Listen for the pause. Now
drop the `PauseProcessor` and say it again. How does the stress
pattern change?

**Hints**

1. `default_pronunciation_processors()` wires
   `PhoneticReplacementProcessor` (fixed-string swaps) and
   `PauseProcessor` (regex-matched `<break>` insertion). The
   default pause pattern targets phone-number-shaped digit groups.
2. With pauses: the TTS reads "five five five" *pause* "eight six
   seven" *pause* "five three oh nine" — natural cadence.
3. Without pauses: the TTS reads "five five five eight six seven
   five three oh nine" as one big number. Less intelligible over
   the phone.
4. The PauseProcessor wraps matched units in SSML `<break
   time="500ms"/>` tags — only works if your TTS supports SSML.
   OpenAI's TTS does; some don't. Check
   `tts.input.format="ssml"` in `src/easycat/tts/input.py`.

## Self-check

You should be able to: (a) explain the difference between deep and
shallow mode in one sentence each, (b) name when to use a tool vs
a session action without re-reading chapter 7, and (c) describe
where in the pipeline output processors run (TTS only? history
too?) without checking the source.
