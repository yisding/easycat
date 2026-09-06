# Chapter 4: Add Tools Without Losing Control

The app can already hear, decide when a turn ends, and speak. Now give the
agent a way to fetch application data, let it request a controlled session
side effect, observe each tool call, and make the result easier to pronounce.

This chapter keeps four surfaces separate because they have different owners:

| Surface | Owner | What it changes |
|---|---|---|
| Agent function tool | Your agent framework | Data available to the model |
| `SessionActions` | EasyCat | Controlled effects at turn finalization |
| Tool events | EasyCat observers | Logs, UI state, and timing signals |
| Output processor | EasyCat's TTS boundary | Spoken text only |

The example uses OpenAI Agents SDK tools because the first chapters use its
agent. Chapter 5 compares the other EasyCat agent bridges.

## Prerequisites

- Complete [chapter 3](../03-conversation-controls/), or be comfortable with
  `VoiceApp` and `EasyConfig.mic`.
- Run `uv sync --extra quickstart --group dev` from the repository root.
- Set `OPENAI_API_KEY` for live `run` mode. Offline `preview` mode does not use
  credentials, a microphone, or a provider API.
- A microphone and speakers for live mode.
- Run `uv run easycat doctor` after exporting the key. If it lives in `.env`,
  run `uv run easycat doctor --env-file .env`. Use
  `uv run easycat doctor --json` or
  `uv run easycat doctor --env-file .env --json` for parseable checks.
- If the key lives in `.env`, add `--env-file .env` after `uv run` when running
  live mode.

## Preview the speech rules offline

Start with the deterministic checkpoint:

```bash
uv run python docs/using-easycat/04-tools-actions/main.py preview
```

It prints two forms of the same response:

```text
Agent text: Siobhan Nguyen's phone number is +1 (555) 123-4567.
Spoken text: shi-vawn win's phone number is 1 ... 5 ... 5 ... 5 ... 1 ... 2 ... 3 ... 4 ... 5 ... 6 ... 7.
```

The exact model-facing answer remains readable. Only the payload sent toward
TTS receives the phonetic spelling and digit pacing.

## Run the tool-enabled voice app

```bash
uv run python docs/using-easycat/04-tools-actions/main.py run
```

If the key is in `.env`:

```bash
uv run --env-file .env python docs/using-easycat/04-tools-actions/main.py run
```

Ask, “What is Siobhan Nguyen's phone number?” Then say goodbye. The terminal
shows a start and result event for each function call. The contact result is
spoken with the output rules, and the goodbye tool asks EasyCat to stop after
the final response audio drains.

## A normal tool returns information

`lookup_contact` is a regular OpenAI Agents SDK `@function_tool`. Its typed
arguments and docstring become the tool schema. It returns data to the agent,
which decides how to use that data in its response.

EasyCat does not own that function or its schema. `OpenAIAgentsBridge` adapts
the framework's run into EasyCat's streaming agent protocol and maps the
framework's calls to `ToolCallStarted` and `ToolCallResult` events. This is the
same separation you will use with database lookups, HTTP requests, or business
logic.

Keep ordinary tools narrow and return application data rather than prose that
tries to steer the whole conversation. The agent instructions remain the
place for conversational policy.

## A session action requests a controlled side effect

`finish_conversation` is also an agent tool, but its body does not receive the
live `Session`. It receives the shared `SessionActions` queue through the
agent framework's typed context:

```python
actions = SessionActions()

bridge = OpenAIAgentsBridge(agent=agent, context=actions)
config = EasyConfig.mic(agent=bridge, session_actions=actions)
```

Both references must point to the same instance. The tool enqueues
`actions.end_call(...)`; EasyCat drains that queue while finalizing the turn,
emits the session-action lifecycle events, lets queued response audio drain,
and then stops the session.

This boundary matters. A model-invoked function should not hold or mutate the
live `Session` from the agent framework's worker context. `SessionActions` is a
thread-safe, typed request channel with EasyCat-owned execution and audit
events.

Session actions are not ordinary tool results. The model does not wait for the
provider side effect, and executor failures are emitted as
`SessionActionFailed` events rather than returned into the agent framework. When
the model must know whether a side effect succeeded before saying so, implement
that operation as a normal agent tool and return a typed result to the model.

The queue also offers transfer, DTMF, SMS, and do-not-call actions. Those need
the matching transport or application executor; `end_call` works in this
local lesson. Chapter 10 applies the telephony actions with their real runtime
requirements.

## Tool events observe; they do not act

Calling `VoiceApp.session("local")` gives the app the unstarted `Session`, so
the example can register ergonomic callbacks before `run_session` starts it:

```python
session.on(
    tool_started=lambda tool_name, call_id: print(tool_name, call_id),
    tool_result=lambda call_id, result: print(call_id, result),
)
```

These callbacks are useful for logs, spinners, latency measurements, and UI
state. They do not change tool execution or the model result.

EasyCat does not synthesize filler speech automatically. A `tool_started`
event is a timing hook if an application wants a custom “one moment” cue, but
that cue needs its own cancellation and audio-ordering policy so it cannot
race the real result. The built-in feature here is lifecycle observation, not
automatic filler generation.

## Output processors change speech, not meaning

`PhoneticReplacementProcessor` and `PauseProcessor` run after agent text is
produced and before TTS synthesis. They do not rewrite agent history, tool
results, terminal `AgentFinal` events, or text shown in a chat UI.

Processor order is significant. This lesson first replaces names, then finds
a phone-number span and separates its digits. The `minimum_units=7` guard
prevents short numbers elsewhere in a reply from being paced accidentally.

`PauseProcessor` defaults to an ellipsis cue that remains plain text through
every bundled TTS path. This lesson selects `style="ellipsis"` explicitly to
make that policy visible. Exact `pause_ms` timing requires `style="ssml"` and a
provider whose input policy advertises native SSML; unsupported SSML tags are
stripped before synthesis.

### `strip_markdown` is the same boundary, one layer down

An LLM that has been asked for a list will happily emit `**bold**` and
`` `backticks` ``, and a TTS engine will read those characters aloud.
`strip_markdown=True` removes markdown formatting from agent output before
synthesis, in the same "changes speech, not meaning" position as the processors
above:

```python
EasyConfig(agent=agent, strip_markdown=True)
```

It leaves history, tool results, and `AgentFinal` untouched, so a chat UI still
shows the formatted text. Fenced and inline code keeps its *content* — only the
delimiters go.

## Reaching outside the process

Two fields extend what an agent can reach and what happens when it fails.

`mcp_servers=` passes MCP server URIs through to whichever agent bridge you are
using, so tools hosted outside your process join the same tool surface the
`@function_tool` decorators above create:

```python
EasyConfig(agent=agent, mcp_servers=["stdio://./my-mcp-server", "https://tools.example/mcp"])
```

Accepted schemes are `stdio://`, `sse://`, `http://`, and `https://`. The list
is **frozen per session** — changing it mid-session is not supported — so
treat it as deployment configuration, not runtime state. Remote tools are also
a latency and trust boundary: a slow MCP call is inside the agent's turn budget
(`timeouts.agent_timeout` from chapter 3), and a remote server sees whatever
arguments the model chooses to send.

`on_agent_failure=` decides what the caller hears when the agent raises or
times out, instead of silence:

```python
EasyConfig(agent=agent, on_agent_failure="Sorry, I hit a problem. Say that again?")
```

It accepts fixed text or a callable taking the exception, so you can vary the
line by failure type. Without it, an agent failure produces an `Error` event and
no speech — fine for a headless worker, poor on a phone call, where a person is
listening to nothing.

Continue with [the exercises](./EXERCISES.md) to trace each boundary and add a
tool without giving it control over the session.

## What you should be able to answer now

> Why are the agent context and `EasyConfig.session_actions` the same object?

The tool enqueues requests through the context; EasyCat must drain that exact
queue from the session.

> Do pronunciation rules alter the agent's memory?

No. They transform only the TTS payload.

> Does `tool_started` make the assistant say a filler phrase?

No. It is an observation hook; custom audio behavior is an application policy.

## What's next

Chapter 5 keeps these tool boundaries and swaps the agent layer across
OpenAI Agents, PydanticAI, LangChain, LangGraph, LlamaAgents, and a custom
bridge.
