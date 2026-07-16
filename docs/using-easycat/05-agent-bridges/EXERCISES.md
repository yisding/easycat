# Chapter 5 Exercises

Use these exercises to choose an adapter deliberately and keep mutable agent
state inside the right session boundary.

## 1. Verify detection without credentials

Run:

```bash
uv run python docs/using-easycat/05-agent-bridges/main.py matrix
```

Confirm that `SupportWorkflow` becomes `GenericWorkflowBridge` while
`PlainAgent` remains unchanged until `AgentRunner` wraps it. Explain why the
session factory, rather than `auto_adapt_agent()`, owns the plain agent's
runner timeout configuration.

## 2. Keep the speech pipeline and swap the brain

Run the local voice checkpoint:

```bash
uv run python docs/using-easycat/05-agent-bridges/main.py run
```

Ask for support hours twice, then ask which turn this is. Confirm that the
workflow owns the turn counter while EasyCat still owns audio, transcription,
turn detection, and synthesis.

Replace `SupportWorkflow()` with this minimal agent:

```python
class ExerciseAgent:
    async def run(self, text: str) -> str:
        return f"The agent received: {text}"
```

Run again. Which adapter row now applies? Restore `SupportWorkflow` afterward.

## 3. Upgrade shallow workflow detection

Add keyword-only `recorder` and `cancel_token=None` parameters to
`SupportWorkflow.on_user_turn`. The method does not need to use them yet.

Run matrix mode and confirm that it now reports `deep_mode=True`. This is a
signature-level capability choice: `GenericWorkflowBridge` inspects it when
constructed.

For a real slow operation, check `cancel_token.is_cancelled` between awaited
steps and stop producing deltas promptly. Restore the shallow signature when
you finish so the chapter output matches the documented checkpoint.

## 4. Choose the bridge

For each application, name the input object and EasyCat route:

1. An LCEL prompt/model/parser chain with `astream_events()`.
2. A multi-node agent with a persistent checkpointer and resumable thread.
3. A typed agent whose tools receive a PydanticAI dependency object.
4. A LlamaIndex workflow hosted by a separate workflow server.
5. A company-owned orchestration class with `on_user_turn`.
6. An agent service exposing `/v1/responses` over HTTP and SSE.

For each one, decide whether auto-detection is sufficient or explicit bridge
options are required.

## 5. Preserve per-connection isolation

Imagine serving browser sessions with one module-level
`SupportWorkflow()` instance. Its `turns` field would be shared by callers.

Sketch a `config_factory` that creates both a fresh workflow and a fresh
`EasyConfig` for each connection. Then compare that with passing one
declarative OpenAI Agents SDK `Agent` spec: EasyCat can safely build a fresh
bridge per session around the reusable spec, but it cannot prove your custom
object's mutable state is isolated.

## 6. Know when to author a full bridge

Read [Writing a Custom Agent Bridge](../../extending/agent-bridge.md). List the
three author-supplied responsibilities of `BridgeTemplate`:

- translate framework events;
- snapshot framework state;
- plan and apply native interruption mutation.

If your integration only needs `on_user_turn`, stop at
`GenericWorkflowBridge`. A full bridge is justified when a framework has its
own streaming event vocabulary and persistent execution state.

## Done when

You can look at an agent object and explain:

- which EasyCat adapter will own it;
- whether auto-detection is enough;
- where conversation state lives;
- what happens on barge-in;
- whether a server needs a per-connection factory.
