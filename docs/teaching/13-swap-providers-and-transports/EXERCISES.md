# Chapter 13 — Exercises

## 1. Add a Cartesia provider preset

**Task.** Add a `--provider-mix cartesia` preset (both STT and TTS
via Cartesia's WebSocket API). What's the minimum diff from
`deepgram-eleven`?

**Hints**

1. Add `cartesia` to the `--provider-mix` choices and return
   `{"stt": "cartesia", "tts": "cartesia"}` from
   `provider_mix()`. Require `CARTESIA_API_KEY`, and install the
   provider extra with `uv sync --extra cartesia --group dev`.
2. Check `src/easycat/stt/factory.py` and `src/easycat/tts/factory.py`:
   Cartesia is already registered on both sides. The exercise
   changes only this teaching script; the `Agent`, `Session`,
   event bus, journal, and smart-turn configuration stay put.
3. First verify the preset without making a provider request:
   import `main.py`, set a placeholder `CARTESIA_API_KEY`, and
   assert that `provider_mix("cartesia")` returns those two string
   shortcuts. A credentialed run writes the same production-shaped
   bundle as the other chapter 13 cells, so inspect it directly with
   `uv run easycat latency PATH --json`; no translator is needed.

## 2. Tightest P95/P50 ratio

**Task.** Record about 20 matched turns per cell with the same short
prompt ("What time is it?"). For each resulting production bundle,
run `uv run easycat latency PATH --json`. Which provider mix has the
tightest server-side p95/p50 ratio? What extra evidence would you
need before making the same claim about transports end to end?

**Hints**

1. P95/P50 ratio measures *consistency*, not absolute speed. A
   slow-but-consistent pipeline beats a fast-but-jittery one for
   user experience.
2. `easycat latency` reports production journal milestones through
   the first server-side TTS byte. It can support a provider-pipeline
   comparison without translating the bundle.
3. It cannot prove browser or phone delivery latency. Pair WebRTC
   runs with client `getStats()` artifacts and phone runs with
   provider/PSTN timing before ranking transports.
4. With only a few turns per cell, P95 is a single turn's
   slowest run — noisy. Re-run each cell ~20 times for a
   meaningful number.

## 3. SendDTMFAction on a real call

**Task.** Wire `SendDTMFAction` from chapter 7 into the agent (the
user asks for "press 1 to continue"). What does the journal show
on the Twilio preset? What does a user on the phone hear?

**Hints**

1. `SendDTMFAction(digits="1")` is dispatched to
   `TwilioSessionActionExecutor`, which calls Twilio's REST API
   to update the active call with `<Play digits="1">`. A successful
   journal path is `session_action_requested`,
   `session_action_started`, then `session_action_completed`; the
   started record names `TwilioSessionActionExecutor`.
2. The user on the phone hears the DTMF tone before the call yields
   back to the bot's audio.
3. On the `local` transport, `CoreSessionActionExecutor` does not
   claim DTMF. The journal records `session_action_requested`
   followed by `session_action_failed` with `No session action
   executor for send_dtmf`; there is no started or completed record.
   The failure is observable rather than a silent no-op.
4. Exercise the executor with a fake Twilio client first, as
   `tests/telephony/test_session_actions.py` does. If you try the
   end-to-end path, use an isolated development account and number,
   not production traffic.

## Self-check

You should be able to: (a) name two axes the matrix attacks, (b)
draw the "one code change per axis" diagram from memory, and (c)
explain the structural `event_bus` opt-in and distinguish reconnect
telemetry from HTTP provider-error telemetry.
