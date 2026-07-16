# Chapter 13 — Exercises

<!-- BEGIN auto:navigation -->
[← Back to chapter](./README.md) · [Ladder index](../) · [Progress worksheet](../PROGRESS.md) · [Chapter 14 — Bring your own agent →](../14-bring-your-own-agent/)
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

## 1. Add a Cartesia provider preset

**Task.** Add a `--provider-mix cartesia` preset (both STT and TTS
via Cartesia's WebSocket API). What's the minimum diff from
`deepgram-eleven`?

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 3</summary>

Add `cartesia` to the `--provider-mix` choices and return
   `{"stt": "cartesia", "tts": "cartesia"}` from
   `provider_mix()`. Require `CARTESIA_API_KEY`, and install the
   provider extra with `uv sync --extra cartesia --group dev`.

</details>

<details markdown="1">
<summary>Hint 2 of 3</summary>

Check `src/easycat/stt/factory.py` and `src/easycat/tts/factory.py`:
   Cartesia is already registered on both sides. The exercise
   changes only this teaching script; the `Agent`, `Session`,
   event bus, journal, and smart-turn configuration stay put.

</details>

<details markdown="1">
<summary>Hint 3 of 3</summary>

First verify the preset without making a provider request:
   import `main.py`, set a placeholder `CARTESIA_API_KEY`, and
   assert that `provider_mix("cartesia")` returns those two string
   shortcuts. A credentialed run writes the same production-shaped
   bundle as the other chapter 13 cells, so inspect it directly with
   `uv run easycat latency PATH --json`; no translator is needed.

</details>
<!-- END auto:exercise-hints -->

## 2. Tightest P95/P50 ratio

**Task.** Record about 20 matched turns per cell with the same short
prompt ("What time is it?"). For each resulting production bundle,
run `uv run easycat latency PATH --json`. Which provider mix has the
tightest server-side p95/p50 ratio? What extra evidence would you
need before making the same claim about transports end to end?

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 4</summary>

P95/P50 ratio measures *consistency*, not absolute speed. A
   slow-but-consistent pipeline beats a fast-but-jittery one for
   user experience.

</details>

<details markdown="1">
<summary>Hint 2 of 4</summary>

`easycat latency` reports production journal milestones through
   the first server-side TTS byte. It can support a provider-pipeline
   comparison without translating the bundle.

</details>

<details markdown="1">
<summary>Hint 3 of 4</summary>

It cannot prove browser or phone delivery latency. Pair WebRTC
   runs with client `getStats()` artifacts and phone runs with
   provider/PSTN timing before ranking transports.

</details>

<details markdown="1">
<summary>Hint 4 of 4</summary>

With only a few turns per cell, P95 is a single turn's
   slowest run — noisy. Re-run each cell ~20 times for a
   meaningful number.

</details>
<!-- END auto:exercise-hints -->

## 3. SendDTMFAction on a real call

**Task.** Wire `SendDTMFAction` from chapter 7 into the agent (the
user asks for "press 1 to continue"). What does the journal show
on the Twilio preset? What does a user on the phone hear?

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 4</summary>

`SendDTMFAction(digits="1")` is dispatched to
   `TwilioSessionActionExecutor`, which calls Twilio's REST API
   to update the active call with `<Play digits="1">`. A successful
   journal path is `session_action_requested`,
   `session_action_started`, then `session_action_completed`; the
   started record names `TwilioSessionActionExecutor`. The Twilio
   preset wires it through `TelephonyConfig.twilio_actions` using
   `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN`.

</details>

<details markdown="1">
<summary>Hint 2 of 4</summary>

The user on the phone hears the DTMF tone before the call yields
   back to the bot's audio.

</details>

<details markdown="1">
<summary>Hint 3 of 4</summary>

On the `local` transport, `CoreSessionActionExecutor` does not
   claim DTMF. The journal records `session_action_requested`
   followed by `session_action_failed` with `No session action
   executor for send_dtmf`; there is no started or completed record.
   The failure is observable rather than a silent no-op.

</details>

<details markdown="1">
<summary>Hint 4 of 4</summary>

Exercise the executor with a fake Twilio client first, as
   `tests/telephony/test_session_actions.py` does. If you try the
   end-to-end path, use an isolated development account and number,
   not production traffic.

</details>
<!-- END auto:exercise-hints -->

## 4. Trace scoped session teardown

**Task.** Run the provider-free lifecycle probe:

```bash
uv run python \
  docs/teaching/13-swap-providers-and-transports/session_scope_probe.py
```

Compare the `graceful` and `cancelled` traces. Explain why the graceful
trace contains `stop(force=False)` followed by a force-stop no-op, while
the cancelled trace has only an effective `stop(force=True)`. Then
explain why postmortem export belongs after session scope exit but before
the caller-owned client closes.

<!-- BEGIN auto:exercise-hints -->
**Hints**

After your first attempt, open Hint 1 only. Close it and try again before opening
the next hint; keep each attempt in your evidence record.

<details markdown="1">
<summary>Hint 1 of 5</summary>

`wait_for_shutdown_signal()` calls default `stop(force=False)` after
   SIGINT/SIGTERM so in-flight work can drain. An outer task cancellation
   can bypass that call.

</details>

<details markdown="1">
<summary>Hint 2 of 5</summary>

`async with session:` starts on entry and always calls
   `stop(force=True)` on exit. If graceful stop already closed the
   session, the second call is deliberately idempotent.

</details>

<details markdown="1">
<summary>Hint 3 of 5</summary>

Clean stop preserves a read-only journal view while releasing live
   providers, transport, and writable storage.

</details>

<details markdown="1">
<summary>Hint 4 of 5</summary>

The outer client is not one of the providers constructed from
   `EasyConfig`. A custom workflow captured it, so caller code retains
   ownership and chooses its scope.

</details>

<details markdown="1">
<summary>Hint 5 of 5</summary>

Move `session.export_postmortem()` inside the session block in the
   probe. The assertion no longer describes a postmortem export, even
   though the fake object can still append an event.

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

1. Which two independent axes does the provider × transport matrix vary, and
   which values remain fixed along each axis?
2. Can you draw the one-code-change-per-axis diagram and cite the six-cell
   matrix evidence that validates it?
3. How does structural `event_bus` opt-in work, and which records distinguish
   reconnect telemetry from HTTP provider-error telemetry?
4. Why must session scope exit precede postmortem bundle export, and which
   lifecycle evidence proves the preserved view remains readable?

<!-- BEGIN auto:exercise-completion -->
---
Self-check complete? Prepare the cumulative spine, then replay it through this chapter:

```bash
uv sync --extra quickstart --group dev
uv run python docs/teaching/offline_spine.py --run --through 13 --jobs 4 --show-evidence
```

- [Review the chapter narrative](./README.md)
- [Update the progress worksheet](../PROGRESS.md)
- [Continue to Chapter 14 — Bring your own agent →](../14-bring-your-own-agent/)
<!-- END auto:exercise-completion -->
