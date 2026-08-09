# Chapter 5: Bring Your Agent

EasyCat owns the voice session; your agent framework still owns reasoning,
tools, and framework-native state. An agent bridge is the translator between
those two systems.

Most applications do not construct a bridge. They pass an agent or workflow
object to `EasyConfig`, and EasyCat detects the matching adapter while it
builds the session. This chapter shows how that choice works, when explicit
construction is useful, and how to bring a small custom workflow with no agent
SDK at all.

## Prerequisites

- Complete [chapter 4](../04-tools-actions/), or understand the difference
  between agent tools and EasyCat session actions.
- Run `uv sync --extra quickstart --group dev` from the repository root.
- `OPENAI_API_KEY` for live `run` mode. The custom workflow itself is local;
  the key is only for the default OpenAI STT and TTS providers. Offline
  `matrix` mode uses no credentials or provider calls.
- A microphone and speakers for live mode.
- Run `uv run easycat doctor` after exporting the key. If it lives in `.env`,
  run `uv run easycat doctor --env-file .env`. Use
  `uv run easycat doctor --json` or
  `uv run easycat doctor --env-file .env --json` for parseable checks.
- If the key lives in `.env`, add `--env-file .env` after `uv run` for live
  mode.

Framework-specific experiments later in this chapter list their own extras.
Do not install every framework just to use the matrix or custom workflow.

## Inspect the adapter matrix offline

```bash
uv run python docs/using-easycat/05-agent-bridges/main.py matrix
```

The command prints every built-in route, then exercises two routes that need
no optional agent SDK:

```text
Detected SupportWorkflow -> GenericWorkflowBridge (deep_mode=False)
Detected PlainAgent -> unchanged; Session adds AgentRunner
```

That difference is intentional. `auto_adapt_agent()` turns known frameworks
and `on_user_turn` workflows into full bridge objects. It leaves a minimal
`async run(text)` agent unchanged so the session factory can wrap it in an
`AgentRunner` with the caller's timeout settings.

## Run a custom workflow by voice

```bash
uv run python docs/using-easycat/05-agent-bridges/main.py run
```

If the key is in `.env`:

```bash
uv run --env-file .env python docs/using-easycat/05-agent-bridges/main.py run
```

Ask when support is open, then ask which turn this is. `SupportWorkflow`
increments its own state and returns deterministic replies. It makes no model
API call: EasyCat only uses the key for speech recognition and synthesis.

The app passes the raw workflow, not a bridge:

```python
config = EasyConfig.mic(agent=SupportWorkflow())
```

During session construction, EasyCat recognizes `on_user_turn(text)` and
creates a `GenericWorkflowBridge`. STT, TTS, transport, session events, and
turn-taking remain unchanged when the agent layer changes.

## One contract, different framework semantics

Every bridge translates one framework turn into EasyCat's streaming agent
events. That can include text deltas, tool starts/results, a structured final
output, journal cursors, and framework-state snapshots. It also translates
EasyCat cancellation and interruption back into the framework's native state
model.

The concrete bridges do not pretend every framework has identical features.
Choose the adapter whose state and interruption semantics match the agent you
already own:

| Input you pass | EasyCat route | Use it for | Repo install |
|---|---|---|---|
| OpenAI Agents SDK `Agent` | `OpenAIAgentsBridge` | Tools, handoffs, SDK context, previous-response chaining | `--extra quickstart` or `--extra openai-agents` |
| PydanticAI `Agent` | `PydanticAIBridge` | Typed dependencies, tools, and structured results | `--extra pydantic-ai` or `--extra pydantic-ai-v2` |
| LangChain `Runnable` | `LangChainBridge` | LCEL chains, runnable events, and message history | `--extra langchain` for 1.x or `--extra langchain-v0` for 0.3.x |
| Compiled LangGraph graph | `LangGraphBridge` | Nodes, checkpoints, resumable state, and native state edits | `--extra langgraph`; install the model package separately |
| LlamaIndex Workflow | `LlamaAgentsBridge` | Local/remote workflows and human-in-the-loop resumption | `--extra llama-agents` |
| Remote Responses API base URL | `RemoteResponsesAPIBridge` | An agent running behind an HTTP/SSE service boundary | Core; provide `agent_model` and remote auth as needed |
| Object with `on_user_turn(...)` | `GenericWorkflowBridge` | Your own orchestration without a framework adapter | No framework extra |
| Object with `async run(text)` | `AgentRunner` | The smallest single-response agent contract | No framework extra |

`quickstart` already includes OpenAI Agents SDK. Do not redundantly add its
bundled extra. PydanticAI v1 and v2 are separate, conflicting install choices;
select the one your application uses. LangChain 0.3 and 1.x are likewise
separate, conflicting install choices, and both run the same bridge contract.

## Auto-detection is the default path

For standard framework objects, prefer:

```python
EasyConfig.mic(agent=my_agent)
```

EasyCat's ordered detection matters in a few cases:

- A compiled LangGraph graph is also a LangChain `Runnable`, so LangGraph is
  detected first. Auto-adapted graphs must be compiled with a checkpointer.
- A raw Pydantic graph needs explicit `PydanticAIBridge(graph=..., ...)`
  construction because EasyCat cannot infer its state and node factories.
- Voice-to-voice realtime SDK objects are rejected. EasyCat is a chained
  STT → agent → TTS runtime, not a wrapper for realtime speech-to-speech APIs.
- A valid HTTP(S) URL selects `RemoteResponsesAPIBridge` and needs an explicit
  model (`agent_model` in session configuration).

These failures are early configuration errors. They prevent a similar-looking
object from silently taking the wrong state or audio path.

## Construct a bridge when you need bridge options

Chapter 4 explicitly built `OpenAIAgentsBridge` to pass the shared
`SessionActions` queue as SDK context. The same rule applies elsewhere:

- Build `PydanticAIBridge` to supply typed dependencies or a Pydantic graph's
  state factories.
- Build `LangChainBridge` to customize input/history behavior.
- Build `LangGraphBridge` to control thread/checkpoint configuration.
- Build `LlamaAgentsBridge` for a remote workflow server, custom start events,
  or human-response events.
- Build `RemoteResponsesAPIBridge` to set HTTP metadata and timeouts directly.

An explicit bridge is already stateful. In browser, WebSocket, or telephony
servers, construct a fresh one inside `config_factory` for each connection.
Known declarative SDK specs can be reused because EasyCat builds a fresh
bridge around them per session. A custom `SupportWorkflow` instance is mutable
application state, so it also belongs inside that per-connection factory.

### Llama workflow state after barge-in

With `preserve_context=True` (the default), a local `LlamaAgentsBridge`
reuses the workflow handler's `Context` after normal completion. It also keeps
that Context when an interrupted handler confirms `is_done()`, because the
completed Context is safe to pass to the next `workflow.run(ctx=...)`.

If cancellation returns while the handler is still non-terminal, the active
Context cannot be reused safely: doing so can raise `ContextStateError` or
replay buffered deltas from the cancelled answer. The bridge therefore drops
that workflow-internal Context and records a
`LlamaWorkflowContextDropped` framework error. Conversation history still
arrives through the configured `context_key`, and the next start event carries
`easycat_interruption_note` so the workflow knows that the prior response was
cut off. Treat retrieval caches, summaries, and other critical `ctx.store`
data as recoverable if a workflow step might ignore cancellation.

## Custom workflows have shallow and deep modes

This chapter's `SupportWorkflow` is shallow:

```python
async def on_user_turn(self, text: str) -> str: ...
```

Shallow mode can return a string or stream string deltas. It is a good fit for
fast orchestration whose internal steps do not need to appear in the EasyCat
journal. EasyCat cannot safely rewrite opaque workflow state during mid-turn
barge-in, so shallow mode rejects interruption unless the workflow implements
its own `apply_interruption(...)` hook.

Add a named `recorder` parameter to select deep mode:

```python
async def on_user_turn(
    self,
    text: str,
    *,
    recorder,
    cancel_token=None,
) -> str: ...
```

Deep mode lets orchestration record its tools/nodes and cooperate with
`cancel_token` during slow work. Use it when chapter 3's barge-in guarantees
must extend inside your custom workflow.

If you are integrating an entire framework rather than one workflow,
subclass public `BridgeTemplate`. It supplies cursor cleanup, interruption
journal ordering, safe state serialization, and reset defaults; the bridge
author supplies event translation, a framework-state snapshot, and native
interruption mutation. The full conformance guide is
[Writing a Custom Agent Bridge](../../extending/agent-bridge.md).

Continue with [the exercises](./EXERCISES.md) to test detection, upgrade a
workflow boundary, and choose a per-connection ownership model.

## What you should be able to answer now

> Do I need to import `OpenAIAgentsBridge` for a normal `agents.Agent`?

No. Pass the agent directly unless you need bridge-specific options such as
SDK context.

> Why is a LangGraph graph not handled as a generic LangChain runnable?

Its checkpointer, thread, node, and interruption state require the
LangGraph-specific bridge.

> When is a custom workflow too shallow?

When long-running internal work needs cooperative cancellation, journaled
steps, or framework-state repair after barge-in.

## What's next

Chapter 6 drops below `VoiceApp.run` to control the public `Session`: lifecycle,
event subscriptions, text turns, resets, and graceful versus forced teardown.
