# Writing a Custom Agent Bridge

Agents plug into the Session through `ExternalAgentBridge`, the single
contract defined in `easycat.integrations.agents.base`. There are three
tiers, and most custom agents never need the full protocol:

1. **A plain async agent** — any object with `async def run(text) -> str`.
2. **A workflow object** — any object with `on_user_turn(...)`, wrapped by
   `GenericWorkflowBridge` automatically.
3. **A full bridge** — implement `ExternalAgentBridge` yourself (what the
   OpenAI Agents / PydanticAI / LangChain / LangGraph integrations do).

`auto_adapt_agent()` (re-exported from `easycat`) detects known framework
objects; `EasyConfig(agent=...)` calls it for you.

## Tier 1: a plain async agent

The smallest complete agent — Session wraps it in `AgentRunner`, which adds
timeout, cancellation, and conversation history:

```python
from easycat import EasyConfig, run


class EchoAgent:
    async def run(self, text: str) -> str:
        return f"You said: {text}"


run(EasyConfig.mic(agent=EchoAgent()))
```

## Tier 2: a workflow object

Objects with `on_user_turn` get wrapped in `GenericWorkflowBridge`. Shallow
mode returns a string (or an async iterator of string deltas for streaming):

```python
class SupportWorkflow:
    def __init__(self) -> None:
        self.history: list[str] = []

    async def on_user_turn(self, text: str) -> str:
        self.history.append(text)
        return f"Turn {len(self.history)}: {text}"
```

Deep mode adds `recorder` (and optionally `cancel_token`) parameters so your
orchestration can journal tool calls and support mid-turn barge-in:

```python
async def on_user_turn(self, text, *, recorder, cancel_token=None) -> str: ...
```

## Tier 3: the full bridge protocol

Implement `ExternalAgentBridge` when you own a framework with internal
execution state. The members (see `easycat/integrations/agents/base.py` for
the authoritative docstrings):

| Member | Purpose |
| --- | --- |
| `COMMITTABLE_BOUNDARIES` | Map of unit kinds to commit rules for interruption handling. |
| `async invoke(turn_input, recorder, cancel_token)` | Run one turn, yielding `AgentBridgeEvent`s (`text_delta`, `text_replace`, `tool_started`, ...). |
| `snapshot_state()` | JSON-safe `FrameworkStateSnapshot` of framework state. |
| `apply_interruption(delivered_text, mode, ...)` | Truncate history to what the user actually heard. |
| `replace_last_assistant_text(text)` | Adopt the post-processed (Markdown-stripped) reply. |
| `append_interruption_note(note)` | Alternative interruption style: annotate instead of truncate. |
| `reset()` | Clear all framework state for a fresh session. |

Start from `easycat/integrations/agents/generic_workflow.py` — it is the
reference implementation that maps a minimal surface onto the full protocol.

## Verifying conformance

```python
from easycat.integrations.agents import AgentRunner
from easycat.integrations.agents.base import ExternalAgentBridge
from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge


def test_workflow_adapts_to_bridge() -> None:
    bridge = GenericWorkflowBridge(SupportWorkflow())
    assert isinstance(bridge, ExternalAgentBridge)


def test_plain_agent_wraps_into_bridge() -> None:
    runner = AgentRunner(EchoAgent())
    assert isinstance(runner, ExternalAgentBridge)
```

The in-tree behavioral contract — the bridge *event grammar* every
implementation must produce — lives in
[`tests/contracts/test_agent_bridge_contracts.py`](../../tests/contracts/test_agent_bridge_contracts.py).

## Notes

- Append-only bridges yield unindexed `text_delta` events as text arrives;
  Session splits them into sentences for TTS. Buffering a whole reply before
  yielding adds first-audio latency.
- A bridge with indexed, replaceable framework parts yields
  `text_replace(text=..., part_index=N)` for the complete current part, then
  indexed `text_delta` events for continuations. Repeating `text_replace` at
  `N` replaces that part. Do not mix indexed and unindexed text events in one
  turn.
- Session repairs replacements while text is still buffered. If replacement
  changes text after TTS admission, playback is cancelled and cleared and the
  rest of that turn is not synthesized; the corrected final transcript still
  completes normally. This avoids replaying duplicate speech over a stale
  audible prefix.
- Honor `cancel_token` cooperatively — check it between model/tool steps
  rather than relying on task cancellation.
- `apply_interruption` receives the text the user actually heard
  (estimated from delivered audio), not the full generated reply.
