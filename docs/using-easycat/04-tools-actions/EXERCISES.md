# Chapter 4 Exercises

These exercises separate data tools, session effects, event observation, and
spoken-text preparation. Run them from the repository root.

## 1. Inspect the speech-only transformation

Run the offline checkpoint:

```bash
uv run python docs/using-easycat/04-tools-actions/main.py preview
```

Confirm that the `Agent text` line keeps the normal spelling and punctuation,
while the `Spoken text` line contains `shi-vawn`, `win`, and paced digits.

Then change the `Nguyen` replacement in `build_output_processors()` and run
the preview again. Which line changes? Why would rewriting the agent text
itself be less useful for logs and chat transcripts?

## 2. Trace a normal function tool

Start the live app:

```bash
uv run python docs/using-easycat/04-tools-actions/main.py run
```

Ask for Siobhan Nguyen's phone number. Find these stages:

1. `[tool started] lookup_contact` appears with a call ID.
2. `[tool result]` carries the same call ID and the directory result.
3. The assistant answers using that result.
4. TTS speaks the pronunciation-adjusted form.

Ask for a different name. Confirm that the tool's “not found” result reaches
the agent without creating a session action.

## 3. Trace a session action

Say goodbye while the live app is listening. The agent should call
`finish_conversation`, speak its final response, and then stop.

Explain the route in your own words:

```text
agent tool -> shared SessionActions queue -> EasyCat executor -> session stop
```

Now deliberately replace `session_actions=actions` with a new
`SessionActions()` instance. Predict the result before running it. Restore the
shared instance afterward.

## 4. Treat events as observation

Change the `tool_started` callback to record `time.monotonic()`, then print the
elapsed time in `tool_result`. The callback should measure the tool without
changing its return value.

Why would starting filler audio in that callback require more policy? Consider
what must happen if the tool finishes immediately, the user interrupts, or TTS
is already speaking.

## 5. Add one read-only tool

Add a `list_contacts()` function tool that returns the names in the fictional
directory. Keep it independent from `SessionActions`; listing data should not
end or otherwise mutate the voice session.

Update the agent instructions so it uses the tool when asked who is available.
Then verify that both tool lifecycle callbacks appear and that pronunciation
processing still affects only the spoken response.

## 6. Compare pause styles

In `build_output_processors()`, compare:

```python
style="ellipsis"
style="emdash"
style="ssml"
```

Use offline preview first. Notice that SSML produces a structured payload,
while the two plain-text styles produce punctuation hints. Restore
`style="ellipsis"` before using the default OpenAI TTS path, whose plain-text
input policy does not preserve SSML breaks.

## Done when

You can explain all four statements without treating them as synonyms:

- A function tool returns application data to the agent.
- A session-action tool enqueues a typed EasyCat side-effect request.
- A tool event reports lifecycle state to observers.
- An output processor changes only what reaches TTS.
