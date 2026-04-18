# LangChain and LangGraph Bridge — Deferred

> **This is a deferred follow-up, not a current workstream or
> peripheral.** It documents the eventual `LangChainBridge` and
> `LangGraphBridge` implementations that will be added once the
> essential plan and its immediate peripherals have stabilized.
> Nothing in this document is scheduled for the current essential
> plan or any of the `peripheral-*.md` files. The doc exists so
> that (a) the current bridge architecture can be verified
> forward-compatible with LangChain/LangGraph, and (b) when the
> work is eventually scheduled, the author has a concrete
> starting point instead of a blank page.
>
> **Not part of:**
>
> - `essential-debug-first-runtime.md` (essential plan)
> - `workstream-1-journal-foundation.md` through
>   `workstream-5-legacy-removal.md`
> - `peripheral-dx-onboarding.md`
> - `peripheral-cli.md`
> - `peripheral-redaction.md`
> - `peripheral-provider-ecosystem.md`
> - `peripheral-observability-and-cost.md`
> - `peripheral-eval-and-debugger-ui.md`
>
> **In scope (this file):** `LangChainBridge` wrapping the LangChain
> `Runnable` protocol via `astream_events(version="v2")`,
> `LangGraphBridge` wrapping `CompiledGraph` via
> `stream(stream_mode=...)`, shared `_langchain_events.py` event
> translator module, checkpoint-based committable boundaries for
> LangGraph, `update_state()`-based interruption patching,
> `auto_adapt_agent()` dispatch extensions, end-to-end examples
> paralleling the WS2A appendix.

## Context

LangChain is the most-installed Python LLM framework as of 2026
and remains the default on-ramp for users coming from the broader
LLM ecosystem. LangGraph — LangChain's stateful orchestration
runtime — has become the reference implementation for
"long-running, stateful agent workflows" with durable execution,
built-in checkpointing, time-travel, and human-in-the-loop
interrupts.

Supporting both as first-class bridges is not on the essential
plan's critical path because:

1. The essential plan's thesis is a debug-first chained voice
   runtime. Any agent framework that fits the `ExternalAgentBridge`
   protocol gives users the same debugging experience, so
   framework coverage is additive, not load-bearing.
2. PydanticAI + OpenAI Agents SDK already cover the two agent
   frameworks most widely used with voice as of late 2025 / early
   2026. LangChain users are typically building text chat, RAG,
   or document pipelines, and shift to voice later in their
   journey.
3. LangGraph's checkpointing and time-travel story overlaps with
   EasyCat's replay and `forked_replay` work in
   `peripheral-eval-and-debugger-ui.md`. Sequencing LangGraph
   after that peripheral lands means EasyCat's replay surface is
   already shaped by LangGraph vocabulary before the bridge
   arrives.

But the demand is real: a substantial slice of users who might
adopt EasyCat arrive with an existing LangChain chain or
LangGraph graph they want to attach voice to, and forcing them to
port to PydanticAI or OpenAI Agents is a hard sell. This doc
exists to make sure the current architecture can absorb
LangChain/LangGraph whenever the time comes, and to pre-compute
the implementation so the future work is a matter of writing code
rather than re-designing the bridge boundary.

## Protocol Fit Analysis

The first question is: does the current `ExternalAgentBridge`
protocol fit LangChain/LangGraph without structural changes? The
answer is yes, with very minor extension in one place. Here is
the audit, feature by feature.

### What fits as-is

**Four-method protocol (`invoke`, `snapshot_state`,
`apply_interruption`, `reset`)** — maps cleanly onto LangChain
runnables and LangGraph compiled graphs. `invoke` drives
`astream_events(version="v2")` for LangChain or
`stream(stream_mode=[...], version="v2")` for LangGraph.
`snapshot_state` returns a JSON-safe dict; for LangGraph it
includes `checkpoint_id` and `thread_id` so any snapshot can be
resumed via `graph.get_state(config)`. `apply_interruption` uses
`graph.update_state(config, values)` to mutate message history
after a barge-in. `reset` creates a new thread ID for the next
session.

**`AgentRecorder` protocol** — every method already exists for
the LangChain/LangGraph event shape:

- `on_chain_start` / `on_chain_end` → `record_unit_entered` /
  `record_unit_exited` with `unit_kind="specialist"`
- `on_chat_model_start` / `on_chat_model_end` →
  `record_unit_entered` / `record_unit_exited` with
  `unit_kind="model_node"`
- `on_chat_model_stream` → yielded as `AgentBridgeEvent.text_delta`
- `on_tool_start` → `record_tool_call(phase="start", ...)`
- `tool_call_chunks` streaming → `record_tool_call(phase="delta", ...)`
- `on_tool_end` → `record_tool_call(phase="result", ...)`
- `on_tool_error` → `record_tool_call(phase="error", ...)`
- LangGraph node transition via `Command(goto=...)` →
  `record_framework_handoff(from_unit, to_unit, reason)`
- LangGraph checkpoint commit → `record_state_snapshot(ref)`
- LangGraph `interrupt()` primitive (human-in-the-loop, not
  barge-in) → treated as a cooperative pause; the bridge can
  model it as a cancellation boundary with
  `reason="langgraph_interrupt"` or expose it via a separate
  method (see "Deferred design decisions" below)

**`ExecutionCursor` unit kinds** — `agent`, `specialist`,
`workflow_node`, `model_node`, `tool_call` already cover every
LangChain/LangGraph concept without invention:

- LangChain `Runnable` chain → `agent` or `specialist` depending
  on semantics
- LangChain LLM call → `model_node`
- LangChain tool call → `tool_call`
- LangGraph `StateGraph` node → `workflow_node`
- LangGraph subgraph (nested graph) → nested `workflow_node` with
  `parent_unit_id` populated from the enclosing node

**`parent_unit_id` and nested cursors** — LangChain's
`astream_events(version="v2")` already returns `run_id` and
`parent_ids` on every event. Mapping `parent_ids[0]` → EasyCat's
`parent_unit_id` is mechanical. Nested runnables compose
naturally into nested EasyCat cursors with zero invention.

**`COMMITTABLE_BOUNDARIES` and replay** — LangGraph's checkpointing
is the cleanest fit in the entire analysis. Every LangGraph
checkpoint is a committable point. A `LangGraphBridge` publishes:

```python
COMMITTABLE_BOUNDARIES = {
    "workflow_node": "between_nodes",       # every checkpoint
    "tool_call": "between_phases",
    "model_node": "non_committable_during_stream",
    "agent": "between_turns",
}
```

The richer LangGraph model (native `checkpoint_id`,
`get_state_history()`, `update_state()` time-travel, subgraph
checkpointing) actually *enables* stronger replay semantics than
PydanticAI does. See "LangGraph checkpoint vocabulary alignment"
below for how this connects to
`peripheral-eval-and-debugger-ui.md`'s `forked_replay` fidelity
class.

**`FrameworkStateSnapshot`** — LangGraph's `StateSnapshot` already
has structured fields (`values`, `config`, `metadata`, `next`,
`tasks`, `interrupts`, `created_at`, `parent_config`). Converting
to `FrameworkStateSnapshot` is mechanical. The user-defined
`values` dict goes via artifact ref (it can be arbitrarily large);
the config and metadata fit inline.

**`FrameworkHandoff` records** — LangGraph's `Command(goto=...)`
is the most explicit handoff primitive in the three frameworks
EasyCat would support. The bridge inspects each node's return
value, extracts the `goto` target, and emits a handoff triple
(`FrameworkUnitExited` → `FrameworkHandoff` →
`FrameworkUnitEntered`) with `from_unit=<current node>`,
`to_unit=<goto target>`, `reason="Command(goto)"`. Cleaner than
PydanticAI's implicit graph transitions because the source,
target, and reason are all in the Command object.

**`_langchain_events.py` translator module** — the convention
documented in WS2A T2.4 ("one translator per framework, sibling to
the bridge file, with a single `translate_event(event, recorder)`
entry point") makes the home for this obvious. LangChain's event
dicts (`{"event": "on_chat_model_stream", "data": {...}, ...}`)
get mapped into the same `AgentRecorder` calls that
`_pydantic_ai_events.py` uses for PydanticAI's typed event
objects.

### What needs minor extension

**None that requires structural changes.** There is one
optional refinement worth noting, but it is not a blocker:

- **`ExecutionCursor.metadata` (optional)** — the cursor
  currently has `unit_id`, `unit_kind`, `display_name`,
  `parent_unit_id`, `sequence`, `entered_at`, `committable`. It
  has no explicit metadata dict. LangGraph's native
  `checkpoint_id` and LangChain's `run_id` can live in the
  enclosing `FrameworkUnitEntered` record's `framework_metadata`
  field (which the journal schema already defines), so a cursor
  metadata dict is not required. But if the WS2A plan reviewers
  want to forward-proof the cursor for framework-native IDs, an
  optional `metadata: dict[str, Any] = field(default_factory=dict)`
  field on `ExecutionCursor` would be a ~3-line addition. This
  is a judgment call and not load-bearing for LangChain/LangGraph
  support.

### What would require structural changes

**Nothing.** The `ExternalAgentBridge` protocol, `AgentRecorder`,
`ExecutionCursor` unit kinds, `COMMITTABLE_BOUNDARIES`, framework
transition records, journal schema, and stage model all fit
LangChain/LangGraph today. Adding the bridges later is purely
additive: two new files
(`src/easycat/integrations/agents/langchain.py` and
`src/easycat/integrations/agents/langgraph.py`), one translator
(`_langchain_events.py`), an `auto_adapt_agent()` dispatch update,
and an examples appendix update.

## LangGraph Checkpoint Vocabulary Alignment

`peripheral-eval-and-debugger-ui.md` already commits to adopting
LangGraph's user-facing `checkpoint_id` vocabulary for EasyCat's
forked-replay surface:

> Internally the journal still uses monotonic `sequence` numbers,
> but externally users see `checkpoint_id` (e.g., `cp_87`) — the
> same concept shape as `get_state_history()` / `update_state()`
> that every LangGraph user already knows.

When a `LangGraphBridge` exists, this alignment becomes literal
rather than aspirational: the bridge puts LangGraph's native
`checkpoint_id` values directly into the
`FrameworkStateCommitted` record's `framework_metadata` field,
and EasyCat's forked-replay CLI and debugger UI surface those IDs
verbatim. LangGraph users see the exact checkpoint IDs they
already use with `graph.get_state_history()`; non-LangGraph users
see EasyCat's `cp_N` generated IDs. One vocabulary, one debugger
UI, two backends.

This also means the LangGraph bridge has a natural affinity for
the `forked_replay` fidelity class in
`peripheral-eval-and-debugger-ui.md`. Where PydanticAI forked
replay has to re-derive checkpoint semantics from the graph's
node boundaries, LangGraph forked replay can call
`graph.update_state(checkpoint_config, values)` directly —
LangGraph's own API does the work. Adding LangGraph is thus
simultaneously the cheapest *and* most capable bridge addition.

## Implementation Sketch

Two bridges, one shared translator, one `auto_adapt_agent()`
update.

### `LangChainBridge` — wraps a `Runnable`

For users with a LangChain chain, agent, or any `Runnable`
composition that is not built with LangGraph. Shallow integration
via `astream_events(version="v2")`.

```python
# src/easycat/integrations/agents/langchain.py (sketch)
from collections.abc import AsyncIterator
from langchain_core.runnables import Runnable

from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
    CancellationMode,
    CommitRule,
    ExternalAgentBridge,
    FrameworkStateSnapshot,
)
from easycat.integrations.agents._langchain_events import translate_event
from easycat.cancel import CancelToken


class LangChainBridge:
    """Wraps a LangChain `Runnable` via astream_events(version='v2')."""

    COMMITTABLE_BOUNDARIES = {
        "agent": CommitRule.BETWEEN_TURNS,
        "specialist": CommitRule.BETWEEN_RUNNABLE_BOUNDARIES,
        "model_node": CommitRule.NON_COMMITTABLE_DURING_STREAM,
        "tool_call": CommitRule.BETWEEN_PHASES,
    }

    def __init__(self, runnable: Runnable, *, display_name: str | None = None):
        self._runnable = runnable
        self._display_name = display_name or type(runnable).__name__
        self._history: list = []  # LangChain message history

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        # Build input with history; exact shape depends on the runnable.
        input_data = {"input": turn_input.text, "history": self._history}

        async for event in self._runnable.astream_events(
            input_data, version="v2"
        ):
            if cancel_token and cancel_token.is_cancelled():
                recorder.record_cancellation_boundary(
                    mode=CancellationMode.IMMEDIATE_STOP,
                    reason="cancel_token_set",
                )
                break

            # The shared translator maps LangChain event dicts to
            # AgentRecorder calls + optional AgentBridgeEvent yields.
            for bridge_event in translate_event(event, recorder):
                yield bridge_event

        # Update history from the final output (shape is runnable-specific).

    def snapshot_state(self) -> FrameworkStateSnapshot:
        return FrameworkStateSnapshot(
            fields={
                "runnable": self._display_name,
                "history_length": len(self._history),
                # Large history goes via artifact ref.
            }
        )

    def apply_interruption(
        self, delivered_text: str, mode: CancellationMode
    ) -> None:
        # Truncate the last AIMessage in history to match delivered text.
        # LangChain history is a list of BaseMessage subclasses.
        ...

    def reset(self) -> None:
        self._history.clear()
```

### `LangGraphBridge` — wraps a `CompiledGraph`

For users with a LangGraph StateGraph. Deep integration via
`stream(stream_mode=["updates", "messages", "tools", "custom"],
version="v2")`, full use of checkpointer for committable
boundaries and interruption patching.

```python
# src/easycat/integrations/agents/langgraph.py (sketch)
from collections.abc import AsyncIterator
import uuid

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
    CancellationMode,
    CommitRule,
    ExecutionCursor,
    ExternalAgentBridge,
    FrameworkStateSnapshot,
)
from easycat.integrations.agents._langchain_events import translate_event
from easycat.cancel import CancelToken


class LangGraphBridge:
    """Wraps a LangGraph CompiledGraph with checkpointer support."""

    COMMITTABLE_BOUNDARIES = {
        "workflow_node": CommitRule.BETWEEN_NODES,  # every checkpoint
        "agent": CommitRule.BETWEEN_TURNS,
        "model_node": CommitRule.NON_COMMITTABLE_DURING_STREAM,
        "tool_call": CommitRule.BETWEEN_PHASES,
    }

    def __init__(
        self,
        graph: CompiledStateGraph,
        *,
        thread_id: str | None = None,
    ):
        if graph.checkpointer is None:
            raise ValueError(
                "LangGraphBridge requires a graph compiled with a "
                "checkpointer. Call graph.compile(checkpointer=...) "
                "before passing it to LangGraphBridge."
            )
        self._graph = graph
        self._thread_id = thread_id or str(uuid.uuid4())

    def _config(self) -> dict:
        return {"configurable": {"thread_id": self._thread_id}}

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        config = self._config()
        input_data = {"messages": [("user", turn_input.text)]}

        current_node: str | None = None

        async for mode, chunk in self._graph.astream(
            input_data,
            config=config,
            stream_mode=["updates", "messages", "tools"],
            subgraphs=True,
            version="v2",
        ):
            if cancel_token and cancel_token.is_cancelled():
                recorder.record_cancellation_boundary(
                    mode=CancellationMode.IMMEDIATE_STOP,
                    reason="cancel_token_set",
                )
                break

            if mode == "updates":
                # Node transitions. Emit workflow_node cursor entries
                # and handoff triples when the node changes.
                for node_name, _state_update in chunk["data"].items():
                    if current_node and current_node != node_name:
                        # Handoff triple
                        ...
                    current_node = node_name
                    cursor = ExecutionCursor(
                        unit_id=f"{node_name}-{uuid.uuid4().hex[:8]}",
                        unit_kind="workflow_node",
                        display_name=node_name,
                        parent_unit_id=None,
                        sequence=0,  # populated from checkpoint metadata
                        entered_at=0,
                        committable=True,
                    )
                    recorder.record_unit_entered(cursor)

            elif mode == "messages":
                # LLM token stream. Route through shared translator
                # to emit text_delta events and record model_node
                # cursor entries.
                msg, metadata = chunk["data"]
                for bridge_event in translate_event(
                    {"event": "on_chat_model_stream", "data": {"chunk": msg}},
                    recorder,
                ):
                    yield bridge_event

            elif mode == "tools":
                # Tool lifecycle. Route through shared translator.
                tool_event = chunk["data"]
                for bridge_event in translate_event(tool_event, recorder):
                    yield bridge_event

        # After the turn, snapshot the checkpoint ID for the journal.
        state = self._graph.get_state(config)
        checkpoint_id = state.config["configurable"].get("checkpoint_id")
        if checkpoint_id:
            recorder.record_state_snapshot(ref=f"langgraph:{checkpoint_id}")

    def snapshot_state(self) -> FrameworkStateSnapshot:
        state = self._graph.get_state(self._config())
        return FrameworkStateSnapshot(
            fields={
                "framework": "langgraph",
                "thread_id": self._thread_id,
                "checkpoint_id": state.config["configurable"].get("checkpoint_id"),
                "next_nodes": list(state.next),
                "step": state.metadata.get("step"),
                # state.values goes via artifact ref (user-defined, potentially large)
            }
        )

    def apply_interruption(
        self, delivered_text: str, mode: CancellationMode
    ) -> None:
        # Use LangGraph's native update_state to patch the last AIMessage.
        state = self._graph.get_state(self._config())
        messages = state.values.get("messages", [])
        if messages and messages[-1].type == "ai":
            messages[-1].content = (
                f"{delivered_text}..." if delivered_text else ""
            )
            self._graph.update_state(
                self._config(), {"messages": messages}
            )

    def reset(self) -> None:
        self._thread_id = str(uuid.uuid4())
```

### `_langchain_events.py` shared translator

```python
# src/easycat/integrations/agents/_langchain_events.py (sketch)
from collections.abc import Iterator
from easycat.integrations.agents.base import AgentBridgeEvent, AgentRecorder


def translate_event(event: dict, recorder: AgentRecorder) -> Iterator[AgentBridgeEvent]:
    """Map a LangChain astream_events v2 event into AgentRecorder calls
    plus optional AgentBridgeEvent yields.

    Shared by LangChainBridge and LangGraphBridge. One translator per
    framework; neither bridge reimplements event mapping.
    """
    event_type = event.get("event") if isinstance(event, dict) else None

    if event_type == "on_chat_model_stream":
        chunk = event["data"]["chunk"]
        text = getattr(chunk, "content", "") or ""
        if text:
            yield AgentBridgeEvent(type="text_delta", text=text)
        # Streaming tool call arguments:
        for tc_chunk in getattr(chunk, "tool_call_chunks", None) or []:
            if tc_chunk.get("name"):
                recorder.record_tool_call(
                    phase="start",
                    name=tc_chunk["name"],
                    args_ref=None,
                    result_ref=None,
                )
            if tc_chunk.get("args"):
                recorder.record_tool_call(
                    phase="delta",
                    name=tc_chunk.get("name", ""),
                    args_ref=None,
                    result_ref=None,
                )

    elif event_type == "on_tool_start":
        recorder.record_tool_call(
            phase="start",
            name=event["name"],
            args_ref=None,
            result_ref=None,
        )

    elif event_type == "on_tool_end":
        recorder.record_tool_call(
            phase="result",
            name=event["name"],
            args_ref=None,
            result_ref=None,
        )

    elif event_type == "on_tool_error":
        recorder.record_tool_call(
            phase="error",
            name=event["name"],
            args_ref=None,
            result_ref=None,
        )

    # on_chain_start, on_chain_end, on_chat_model_start, on_chat_model_end
    # map to record_unit_entered / record_unit_exited with appropriate
    # unit_kind. Implementation omitted for brevity.
```

### `auto_adapt_agent()` dispatch extension

```python
# src/easycat/integrations/agents/base.py (relevant section)

def auto_adapt_agent(agent: Any) -> ExternalAgentBridge:
    # ... existing OpenAI Agents, PydanticAI, GenericWorkflow dispatch ...

    # New: LangChain runnables
    try:
        from langchain_core.runnables import Runnable
        if isinstance(agent, Runnable):
            # Disambiguate: is this a LangGraph CompiledGraph?
            try:
                from langgraph.graph.state import CompiledStateGraph
                if isinstance(agent, CompiledStateGraph):
                    if agent.checkpointer is None:
                        raise BridgeInputError(
                            "LangGraph graphs must be compiled with a "
                            "checkpointer to use LangGraphBridge. "
                            "Call graph.compile(checkpointer=...)."
                        )
                    from .langgraph import LangGraphBridge
                    return LangGraphBridge(graph=agent)
            except ImportError:
                pass  # langgraph not installed
            from .langchain import LangChainBridge
            return LangChainBridge(runnable=agent)
    except ImportError:
        pass  # langchain not installed
```

## Examples

These parallel the WS2A appendix examples. Each is ~40–70 lines and
includes full `EasyCatConfig` wiring.

### Example L1: `LangChainBridge` wrapping an LCEL chain

```python
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from easycat import EasyCatConfig, LocalTransportConfig, create_session
from easycat.integrations.agents import LangChainBridge


prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a helpful voice assistant."),
    ("placeholder", "{history}"),
    ("user", "{input}"),
])

model = ChatOpenAI(model="gpt-5.2")
chain = prompt | model

config = EasyCatConfig(
    transport=LocalTransportConfig(),
    agent=LangChainBridge(runnable=chain),
)
session = create_session(config)
```

Journal output per turn: `agent` cursor entered → `specialist`
entered for the prompt chain → `model_node` with text deltas →
`specialist` exited → `agent` exited.

### Example L2: `LangGraphBridge` wrapping a two-node StateGraph

```python
from typing import TypedDict, Annotated
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from easycat import EasyCatConfig, LocalTransportConfig, create_session
from easycat.integrations.agents import LangGraphBridge


class State(TypedDict):
    messages: Annotated[list, add_messages]


model = ChatOpenAI(model="gpt-5.2")


def research_node(state: State) -> dict:
    response = model.invoke(state["messages"])
    return {"messages": [AIMessage(content=f"Research: {response.content}")]}


def write_node(state: State) -> dict:
    response = model.invoke(state["messages"])
    return {"messages": [AIMessage(content=f"Summary: {response.content}")]}


graph = (
    StateGraph(State)
    .add_node("research", research_node)
    .add_node("write", write_node)
    .add_edge(START, "research")
    .add_edge("research", "write")
    .add_edge("write", END)
    .compile(checkpointer=InMemorySaver())
)


config = EasyCatConfig(
    transport=LocalTransportConfig(),
    agent=LangGraphBridge(graph=graph),
)
session = create_session(config)
```

Journal output per turn: `workflow_node(research)` entered →
nested `model_node` with text deltas → `workflow_node(research)`
exited → `FrameworkHandoff(research → write, langgraph_edge)` →
`workflow_node(write)` entered → nested `model_node` with text
deltas → `workflow_node(write)` exited → `FrameworkStateCommitted`
with LangGraph's native `checkpoint_id` in
`framework_metadata.checkpoint_id`.

### Example L3: LangGraph with tool calls and `Command(goto=...)`

```python
from typing import TypedDict, Annotated, Literal
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from easycat import EasyCatConfig, LocalTransportConfig, create_session
from easycat.integrations.agents import LangGraphBridge


class State(TypedDict):
    messages: Annotated[list, add_messages]


@tool
def get_weather(city: str) -> str:
    """Look up the weather for a city."""
    return f"The weather in {city} is 24°C and sunny."


model = ChatOpenAI(model="gpt-5.2").bind_tools([get_weather])


def agent_node(state: State) -> Command[Literal["tools", END]]:
    response = model.invoke(state["messages"])
    if response.tool_calls:
        return Command(update={"messages": [response]}, goto="tools")
    return Command(update={"messages": [response]}, goto=END)


def tools_node(state: State) -> Command[Literal["agent"]]:
    last = state["messages"][-1]
    results = []
    for tc in last.tool_calls:
        result = get_weather.invoke(tc["args"])
        results.append(("tool", result, tc["id"]))
    return Command(
        update={"messages": [AIMessage(content=str(results))]},
        goto="agent",
    )


graph = (
    StateGraph(State)
    .add_node("agent", agent_node)
    .add_node("tools", tools_node)
    .add_edge(START, "agent")
    .compile(checkpointer=InMemorySaver())
)


config = EasyCatConfig(
    transport=LocalTransportConfig(),
    agent=LangGraphBridge(graph=graph),
    mcp_servers=["stdio://mcp-filesystem"],  # forwarded to model.bind_tools
)
session = create_session(config)
```

Journal output per turn: `workflow_node(agent)` entered →
`model_node` with text deltas → `tool_call(get_weather)` start
with `FunctionToolCallEvent` shape → `workflow_node(agent)`
exited → `FrameworkHandoff(agent → tools, Command(goto))` →
`workflow_node(tools)` entered → `tool_call(get_weather)` result
→ `workflow_node(tools)` exited →
`FrameworkHandoff(tools → agent, Command(goto))` →
`workflow_node(agent)` entered → `model_node` with final response
deltas → `workflow_node(agent)` exited → `FrameworkStateCommitted`
with LangGraph `checkpoint_id`. The handoff triples are emitted
by inspecting each node's return `Command` object, which is
cleaner than any other framework EasyCat supports.

## Dependencies on the Essential Plan

| Item | Depends on |
|---|---|
| `LangChainBridge` | WS2A (`ExternalAgentBridge` protocol, `AgentRecorder`, WS2A T2.4 translator convention) |
| `LangGraphBridge` | WS2A + peripheral `forked_replay` checkpoint vocabulary from `peripheral-eval-and-debugger-ui.md` |
| `_langchain_events.py` shared translator | WS2A T2.4 translator convention |
| `auto_adapt_agent()` dispatch update | WS2A T2.8 |
| LangGraph `checkpoint_id` → journal `framework_metadata` | WS1 T1.1 `FrameworkTransitionRecord.framework_metadata` field (already present) |
| LangGraph time-travel → `forked_replay` | `peripheral-eval-and-debugger-ui.md` `forked_replay` fidelity class |
| MCP pass-through (LangChain) | WS2B T2B.6 (forwards to `bind_tools` / model bindings) |
| MCP pass-through (LangGraph) | WS2B T2B.6 (forwards to node runnables via shared config) |

Zero dependencies on WS3, WS4, or WS5 beyond the inherited bridge
protocol. No changes to the essential plan are required.

## Suggested Sequencing

1. **After essential WS5 closes** — the essential plan has a
   stable bridge protocol, the three shipped bridges
   (`OpenAIAgentsBridge`, `PydanticAIBridge`,
   `GenericWorkflowBridge`) are in production use, and the
   `_pydantic_ai_events.py` translator has proven the "one
   translator per framework" pattern works. At this point the
   LangChain bridge is mostly a copy of the pattern with
   LangChain-specific event mapping.
2. **After `peripheral-eval-and-debugger-ui.md`
   `forked_replay`** — so LangGraph's native `checkpoint_id`
   can flow through an existing replay vocabulary instead of
   requiring EasyCat to invent one alongside.
3. **LangChainBridge first, LangGraphBridge second** — LangChain
   is the simpler wrapper (one `Runnable`, `astream_events`
   handles everything), LangGraph adds the
   checkpointer/update_state/time-travel layer on top. Landing
   LangChain first proves the shared translator works before
   adding the extra state-management surface.
4. **Treat as its own workstream (WS6 or equivalent)** when
   scheduled. Not large enough to be multiple workstreams —
   realistic scope is one workstream covering both bridges, the
   shared translator, the dispatch update, two examples, and
   migration notes.

## Deferred Design Decisions

These are questions worth answering when the work is scheduled,
not now:

**LangGraph `interrupt()` vs voice barge-in.** LangGraph has a
first-class `interrupt()` primitive for human-in-the-loop
workflows — code inside a node calls `interrupt(prompt)` and the
graph pauses until the caller resumes with `Command(resume=...)`.
This is semantically different from EasyCat's voice interruption
(barge-in mid-response). The bridge needs to decide whether
`LangGraphBridge.apply_interruption()` participates in LangGraph
interrupts at all, and if so, how. Two options:

1. **Treat them as separate concepts.** Voice interruption goes
   through `apply_interruption(delivered_text, mode)` →
   `update_state()`. LangGraph's `interrupt()` stays a
   LangGraph-internal concept that EasyCat records but does not
   drive. The bridge emits a journal record when it detects an
   interrupt (via `StateSnapshot.interrupts`) but does not expose
   a resume mechanism through the bridge protocol.

2. **Expose interrupt/resume via a separate bridge method.**
   `LangGraphBridge.resume(value)` or similar, not part of
   `ExternalAgentBridge` but available on the bridge class
   directly. Lets users drive LangGraph interrupts from
   application code that knows it is using LangGraph.

Option 1 is simpler and keeps the bridge protocol stable. Option
2 is more powerful but leaks LangGraph concepts into the
application. Defer the decision; flag it in the future plan.

**Per-node tool binding vs graph-wide MCP forwarding.** LangGraph
nodes can call different models with different tool sets.
`EasyCatConfig(mcp_servers=[...])` in the current plan forwards
one server list to all agents referenced by the graph. For
LangGraph the equivalent is "forward to all `bind_tools` calls
inside all nodes". The bridge either walks graph nodes at
construction to find the model bindings (fragile) or requires the
user to pass an explicit list of runnables that should receive
MCP servers (similar to `PydanticAIBridge(graph=..., agents=...)`).
The latter is clearer and matches the PydanticAI pattern; use it.

**`stream_mode` combination.** LangGraph's `stream()` accepts
multiple modes in a list
(`stream_mode=["updates", "messages", "tools"]`). Different modes
give different levels of detail; the bridge should pick the
combination that gives full tool call visibility without
duplicated events. The baseline recommendation is
`["updates", "messages", "tools"]` plus `subgraphs=True` and
`version="v2"`, but the future plan should validate this against a
real graph with tool calls to confirm no events are dropped and
no events are emitted twice.

**LangGraph subgraphs as nested cursors.** With `subgraphs=True`,
LangGraph emits a `ns` (namespace) tuple on every event
identifying the subgraph hierarchy. The bridge maps this to
nested `ExecutionCursor.parent_unit_id` chains, but the exact
mapping — one cursor per namespace segment or one cursor per
subgraph — needs examples to validate. Defer until the work is
scheduled.

## Competitive Context

- **LangChain 2026** is still the default on-ramp for Python LLM
  developers. Supporting it as a bridge adds EasyCat to the
  default consideration set for voice projects.
- **LangGraph 2026** has become the reference implementation for
  stateful agent workflows with checkpointing. Its
  `get_state_history()` and `update_state()` APIs are widely
  known and referenced; adopting the vocabulary alignment first
  (in `peripheral-eval-and-debugger-ui.md`) and then the bridge
  second lets EasyCat land LangGraph support as a natural
  extension rather than a forced fit.
- **LangSmith** observability already covers LangChain and
  LangGraph. EasyCat does not compete with LangSmith for general
  observability — EasyCat's debug-first thesis is voice-specific
  (VAD decisions, Smart Turn, barge-in, TTS delivery ledger),
  which LangSmith does not model. A LangChain/LangGraph user who
  already uses LangSmith for tracing can add EasyCat without
  giving anything up; the journal captures voice-specific
  details LangSmith misses, and OTel export from
  `peripheral-observability-and-cost.md` can forward EasyCat
  records to LangSmith alongside the bridge's own events.
- **`deepagents` package and LangGraph streaming modes**
  (November 2025) introduced `stream_mode="tools"` and richer
  subagent streaming. By the time this deferred work is
  scheduled, the LangGraph stream API will have likely stabilized
  further, which reduces integration risk.

## When to Revisit

Schedule this deferred work when any of the following trigger:

1. **User demand signal.** If more than a handful of potential
   adopters request LangChain or LangGraph support during
   onboarding, prioritize. The essential plan is complete enough
   that adding a bridge is no longer blocking other work.
2. **LangGraph becomes the dominant agent framework for voice.**
   If LangGraph's multi-agent story becomes the default for voice
   agents (plausible given its checkpointer/time-travel
   advantages), supporting it becomes load-bearing for EasyCat's
   competitive position.
3. **`peripheral-eval-and-debugger-ui.md`
   `forked_replay` ships.** At that point the vocabulary alignment
   with LangGraph's `checkpoint_id` becomes concrete and the
   LangGraph bridge has a natural home.
4. **A specific user contributes a PR.** LangChain/LangGraph
   support is a reasonable contribution path for external
   developers — the protocol fit is documented, the translator
   convention is established, and the scope is well-defined.

Until one of these triggers, this doc is the bookmark. The
essential plan stays focused, and the current bridge architecture
is verified forward-compatible with no rework required.
