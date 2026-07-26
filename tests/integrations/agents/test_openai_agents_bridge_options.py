"""Focused OpenAI Agents bridge option and MCP runner tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from easycat.integrations.agents._openai_agents_events import (
    extract_text_delta,
    extract_tool_delta,
    map_run_item,
)
from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import AgentTurnInput, RecorderContext
from easycat.integrations.agents.openai_agents import OpenAIAgentsBridge
from easycat.runtime import InMemoryRingBuffer


def _recorder(journal: InMemoryRingBuffer | None = None) -> JournalAgentRecorder:
    return JournalAgentRecorder(
        journal=journal or InMemoryRingBuffer(capacity=1000),
        artifact_store=None,
        context=RecorderContext(run_id="r1", session_id="s1", turn_id="t1"),
    )


class _Agent:
    def __init__(self, name: str = "Agent", mcp_servers: list[Any] | None = None) -> None:
        self.name = name
        self.model = "gpt-test"
        self.mcp_servers = [] if mcp_servers is None else mcp_servers


class _RunResult:
    def __init__(
        self,
        *,
        last_agent: Any,
        last_response_id: str | None = "resp-1",
        final_output: Any = "final",
        message_history: list[Any] | None = None,
        stream_error: Exception | None = None,
        usage: Any = None,
    ) -> None:
        self.last_agent = last_agent
        self.last_response_id = last_response_id
        self.final_output = final_output
        self._message_history = message_history or []
        self._stream_error = stream_error
        self.context_wrapper = SimpleNamespace(usage=usage)

    async def stream_events(self) -> AsyncIterator[Any]:
        if self._stream_error is not None:
            raise self._stream_error
        if False:
            yield None

    def to_input_list(self) -> list[Any]:
        return self._message_history


class _RunnerCall:
    def __init__(
        self,
        *,
        agent: Any,
        input_data: Any,
        kwargs: dict[str, Any],
        mcp_servers_at_call: list[Any],
    ) -> None:
        self.agent = agent
        self.input_data = input_data
        self.kwargs = kwargs
        self.mcp_servers_at_call = mcp_servers_at_call


class _FakeRunner:
    def __init__(
        self,
        results: list[_RunResult] | None = None,
        *,
        call_error: Exception | None = None,
    ) -> None:
        self._results = list(results or [])
        self._call_error = call_error
        self.calls: list[_RunnerCall] = []

    def run_streamed(self, agent: Any, input_data: Any, **kwargs: Any) -> _RunResult:
        self.calls.append(
            _RunnerCall(
                agent=agent,
                input_data=input_data,
                kwargs=kwargs,
                mcp_servers_at_call=list(agent.mcp_servers),
            )
        )
        if self._call_error is not None:
            raise self._call_error
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_invoke_records_sdk_usage(monkeypatch):
    import easycat.integrations.agents.openai_agents as openai_agents_module

    agent = _Agent()
    result = _RunResult(
        last_agent=agent,
        usage=SimpleNamespace(
            input_tokens=18,
            output_tokens=7,
            input_tokens_details=SimpleNamespace(cached_tokens=5),
        ),
    )
    monkeypatch.setattr(openai_agents_module, "Runner", _FakeRunner([result]))
    journal = InMemoryRingBuffer(capacity=1000)
    bridge = OpenAIAgentsBridge(agent)

    _ = [
        event
        async for event in bridge.invoke(
            AgentTurnInput.from_text("hello"),
            _recorder(journal),
        )
    ]

    [usage] = [record for record in journal.read() if record.name == "agent_usage"]
    assert usage.data == {
        "run_id": "r1",
        "provider": "openai_agents",
        "model": "gpt-test",
        "input_tokens": 18,
        "output_tokens": 7,
        "cached_input_tokens": 5,
    }


@pytest.mark.asyncio
async def test_constructor_options_forwarded_and_mcp_restored_after_success(monkeypatch):
    import easycat.integrations.agents.openai_agents as openai_agents_module

    original_mcp_servers = ["stdio://original"]
    temporary_mcp_servers = ["stdio://session", "https://remote.example.test/mcp"]
    agent = _Agent(mcp_servers=original_mcp_servers)
    run_config = object()
    context = object()
    hooks = object()
    runner = _FakeRunner([_RunResult(last_agent=agent, last_response_id="resp-success")])
    monkeypatch.setattr(openai_agents_module, "Runner", runner)

    bridge = OpenAIAgentsBridge(
        agent,
        run_config=run_config,
        context=context,
        max_turns=7,
        hooks=hooks,
        mcp_servers=temporary_mcp_servers,
    )

    events = [
        event async for event in bridge.invoke(AgentTurnInput.from_text("hello"), _recorder())
    ]

    assert [event.kind for event in events] == ["done"]
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call.agent is agent
    assert call.input_data == "hello"
    assert call.kwargs == {
        "run_config": run_config,
        "context": context,
        "auto_previous_response_id": True,
        "max_turns": 7,
        "hooks": hooks,
    }
    assert call.mcp_servers_at_call == temporary_mcp_servers
    assert agent.mcp_servers is original_mcp_servers
    assert agent.mcp_servers == ["stdio://original"]


@pytest.mark.asyncio
async def test_previous_response_id_is_passed_on_second_turn(monkeypatch):
    import easycat.integrations.agents.openai_agents as openai_agents_module

    agent = _Agent()
    runner = _FakeRunner(
        [
            _RunResult(
                last_agent=agent,
                last_response_id="resp-first",
                message_history=[{"role": "assistant", "content": "first"}],
            ),
            _RunResult(last_agent=agent, last_response_id="resp-second"),
        ]
    )
    monkeypatch.setattr(openai_agents_module, "Runner", runner)
    bridge = OpenAIAgentsBridge(agent, use_previous_response_id=True)
    rec = _recorder()

    async for _ in bridge.invoke(AgentTurnInput.from_text("first"), rec):
        pass
    async for _ in bridge.invoke(AgentTurnInput.from_text("second"), rec):
        pass

    assert len(runner.calls) == 2
    assert runner.calls[0].input_data == "first"
    assert runner.calls[0].kwargs == {"auto_previous_response_id": True}
    assert runner.calls[1].input_data == [{"role": "user", "content": "second"}]
    assert runner.calls[1].kwargs == {
        "previous_response_id": "resp-first",
        "auto_previous_response_id": True,
    }


@pytest.mark.asyncio
async def test_mcp_servers_restored_when_runner_call_fails(monkeypatch):
    import easycat.integrations.agents.openai_agents as openai_agents_module

    original_mcp_servers = ["stdio://original"]
    agent = _Agent(mcp_servers=original_mcp_servers)
    runner = _FakeRunner(call_error=RuntimeError("runner failed"))
    monkeypatch.setattr(openai_agents_module, "Runner", runner)
    bridge = OpenAIAgentsBridge(agent, mcp_servers=["stdio://temporary"])

    with pytest.raises(RuntimeError, match="runner failed"):
        async for _ in bridge.invoke(AgentTurnInput.from_text("hello"), _recorder()):
            pass

    assert runner.calls[0].mcp_servers_at_call == ["stdio://temporary"]
    assert agent.mcp_servers is original_mcp_servers
    assert agent.mcp_servers == ["stdio://original"]


@pytest.mark.asyncio
async def test_mcp_servers_restored_when_stream_iteration_fails(monkeypatch):
    import easycat.integrations.agents.openai_agents as openai_agents_module

    original_mcp_servers = ["stdio://original"]
    agent = _Agent(mcp_servers=original_mcp_servers)
    runner = _FakeRunner(
        [
            _RunResult(
                last_agent=agent,
                stream_error=RuntimeError("stream failed"),
            )
        ]
    )
    monkeypatch.setattr(openai_agents_module, "Runner", runner)
    bridge = OpenAIAgentsBridge(agent, mcp_servers=["stdio://temporary"])

    with pytest.raises(RuntimeError, match="stream failed"):
        async for _ in bridge.invoke(AgentTurnInput.from_text("hello"), _recorder()):
            pass

    assert runner.calls[0].mcp_servers_at_call == ["stdio://temporary"]
    assert agent.mcp_servers is original_mcp_servers
    assert agent.mcp_servers == ["stdio://original"]


@pytest.mark.asyncio
async def test_openai_agents_warmup_primes_models_not_runner(monkeypatch):
    """warmup() issues a cheap bounded model lookup — never a billed Runner.run."""
    # Needs the optional openai-agents SDK; skip in lanes without it.
    default_models_module = pytest.importorskip("agents.models.default_models")
    openai_provider_module = pytest.importorskip("agents.models.openai_provider")

    import easycat.integrations.agents.openai_agents as openai_agents_module

    retrieved: list[tuple[str, float]] = []

    class _FakeModels:
        async def retrieve(self, model: str, *, timeout: float) -> Any:
            retrieved.append((model, timeout))
            return SimpleNamespace(id=model)

        async def list(self) -> Any:  # pragma: no cover - must not be called
            raise AssertionError("warmup should retrieve one model, not list every model")

    class _FakeClient:
        def __init__(self) -> None:
            self.models = _FakeModels()

    class _FakeProvider:
        def _get_client(self) -> _FakeClient:
            return _FakeClient()

    monkeypatch.setattr(openai_provider_module, "OpenAIProvider", _FakeProvider)
    monkeypatch.setattr(default_models_module, "get_default_model", lambda: "gpt-default")
    # A sentinel Runner that fails loudly if warmup ever touches it.
    monkeypatch.setattr(
        openai_agents_module,
        "Runner",
        SimpleNamespace(run=_fail_if_called, run_streamed=_fail_if_called),
    )

    agent = _Agent()
    agent.model = "gpt-agent"
    bridge = OpenAIAgentsBridge(agent, run_config=SimpleNamespace(model="gpt-run"))
    await bridge.warmup()

    assert retrieved == [("gpt-run", openai_agents_module._OPENAI_AGENTS_WARMUP_TIMEOUT_SECONDS)]


@pytest.mark.asyncio
async def test_openai_agents_warmup_swallows_errors(monkeypatch):
    """A client/auth error during warmup must not propagate to Session.start()."""
    # Needs the optional openai-agents SDK; skip in lanes without it.
    openai_provider_module = pytest.importorskip("agents.models.openai_provider")

    class _BoomProvider:
        def _get_client(self) -> Any:
            raise RuntimeError("no api key")

    monkeypatch.setattr(openai_provider_module, "OpenAIProvider", _BoomProvider)

    bridge = OpenAIAgentsBridge(_Agent())
    # Returns cleanly despite the client construction raising.
    await bridge.warmup()


def test_warmup_model_name_extracts_id_from_model_objects():
    """``run_config.model``/``agent.model`` may be SDK ``Model`` objects."""
    # run_config carries a Model object; its id should win over agent/default.
    run_model = SimpleNamespace(model="gpt-run-obj")
    agent = _Agent()
    agent.model = SimpleNamespace(model="gpt-agent-obj")
    bridge = OpenAIAgentsBridge(agent, run_config=SimpleNamespace(model=run_model))

    assert bridge._warmup_model_name("gpt-default") == "gpt-run-obj"


def test_warmup_model_name_falls_back_through_candidates():
    """Empty/objectless candidates fall through to the next usable id."""
    agent = _Agent()
    agent.model = "  gpt-agent  "  # stripped and used when run_config has none
    bridge = OpenAIAgentsBridge(agent, run_config=SimpleNamespace(model="   "))
    assert bridge._warmup_model_name("gpt-default") == "gpt-agent"

    # A Model object with no usable string id falls back to default_model.
    agent_no_id = _Agent()
    agent_no_id.model = SimpleNamespace(model=None)
    bridge_default = OpenAIAgentsBridge(agent_no_id, run_config=None)
    assert bridge_default._warmup_model_name("gpt-default") == "gpt-default"


def _fail_if_called(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("warmup must not invoke the Runner")


def test_openai_agents_event_extractors_map_text_and_tool_deltas():
    text_delta = extract_text_delta(
        SimpleNamespace(type="response.output_text.delta", delta="hello")
    )
    tool_delta = extract_tool_delta(
        SimpleNamespace(type="response.function_call_arguments.delta", delta='{"q"', item_id="i1")
    )

    assert text_delta == "hello"
    assert extract_text_delta(SimpleNamespace(type="response.created")) == ""
    assert tool_delta is not None
    assert tool_delta.kind == "tool_delta"
    assert tool_delta.text == '{"q"'
    assert tool_delta.call_id == "i1"
    assert (
        extract_tool_delta(
            SimpleNamespace(type="response.function_call_arguments.delta", delta="")
        )
        is None
    )
    assert extract_tool_delta(SimpleNamespace(type="response.output_text.delta")) is None


def test_openai_agents_map_run_item_records_tool_start_and_result():
    journal = InMemoryRingBuffer(capacity=1000)
    recorder = _recorder(journal)
    pending: dict[str, str] = {}

    start = map_run_item(
        SimpleNamespace(
            type="tool_call_item",
            raw_item=SimpleNamespace(name="lookup", call_id="call-1"),
        ),
        recorder,
        pending,
    )
    result = map_run_item(
        SimpleNamespace(
            type="tool_call_output_item",
            raw_item=SimpleNamespace(call_id="call-1"),
            output={"ok": True},
        ),
        recorder,
        pending,
    )

    assert start is not None
    assert start.kind == "tool_started"
    assert start.tool_name == "lookup"
    assert start.call_id == "call-1"
    assert result is not None
    assert result.kind == "tool_result"
    assert result.call_id == "call-1"
    assert result.result == "{'ok': True}"
    assert pending == {}
    records = [record for record in journal.read() if record.name == "tool_phase_changed"]
    assert [record.data["phase"] for record in records] == ["start", "result"]
    assert [record.data["tool_name"] for record in records] == ["lookup", "lookup"]
    assert [record.data["call_id"] for record in records] == ["call-1", "call-1"]


def test_openai_agents_map_run_item_reads_call_id_from_dict_raw_item():
    # The SDK's ToolCallOutputItem.raw_item is a FunctionCallOutput *dict*, not
    # an attribute-bearing object, so the call_id must be read from the dict.
    recorder = _recorder()
    pending: dict[str, str] = {"call_abc": "get_weather"}  # seeded as if start fired

    result = map_run_item(
        SimpleNamespace(
            type="tool_call_output_item",
            raw_item={
                "call_id": "call_abc",
                "output": "ok",
                "type": "function_call_output",
            },
            output="ok",
        ),
        recorder,
        pending,
    )

    assert result is not None
    assert result.kind == "tool_result"
    assert result.call_id == "call_abc"  # real id, not ""
    assert result.result == "ok"
    assert pending == {}  # pending.pop matched -> empties


def test_openai_agents_map_run_item_ignores_unknown_items():
    recorder = _recorder()
    pending: dict[str, str] = {}

    event = map_run_item(SimpleNamespace(type="message_output_item"), recorder, pending)

    assert event is None
    assert pending == {}
