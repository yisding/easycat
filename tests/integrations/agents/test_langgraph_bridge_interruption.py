"""LangGraph bridge aclose()-propagation (barge-in) tests."""

from __future__ import annotations

from easycat._turn_context import TurnContext
from easycat.cancel import CancelToken
from easycat.runtime.context import RunContext
from easycat.stages.agent import AgentStage

from ._langgraph_bridge_support import (
    AgentRunner,
    AgentRunnerConfig,
    Any,
    AsyncIterator,
    CancellationMode,
    InMemoryRingBuffer,
    LangGraphBridge,
    _content,
    _MockCompiledGraph,
    _MockMessage,
    _MockState,
    _model_stream,
    _node_end,
    _node_start,
    asyncio,
    pytest,
)


class TestLangGraphBridgeAclosePropagation:
    """A barge-in ``aclose()`` on the consumer side must propagate down
    the ``AgentStage → AgentRunner → LangGraphBridge`` generator chain so
    ``LangGraphBridge._drive_stream``'s ``BaseException`` cleanup commits
    the *partial* turn synchronously — before the follow-up
    ``apply_interruption()``.  ``invoke()`` yields *from* ``_drive_stream``
    via ``async for``, which does not forward the injected ``GeneratorExit``
    into it, so without an explicit ``aclose()`` the inner generator is only
    GC-finalized later and ``apply_interruption()`` runs first, rewriting
    the *previous* turn's assistant message instead of this one."""

    @pytest.mark.asyncio
    async def test_consumer_aclose_propagates_to_drive_stream_cleanup(self):
        class _HangingGraph(_MockCompiledGraph):
            def astream_events(self, input: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
                async def _gen() -> AsyncIterator[dict[str, Any]]:
                    yield _node_start("answer", "n1")
                    yield _model_stream("Hello world", run_id="m1", parent="n1", node="answer")
                    await asyncio.Event().wait()
                    yield _node_end("answer", "n1")  # pragma: no cover

                return _gen()

        prior_ai = _MockMessage("assistant", "previous turn", message_id="prev")
        graph = _HangingGraph(state=_MockState(values={"messages": [prior_ai]}))
        bridge = LangGraphBridge(graph)
        runner = AgentRunner(bridge, AgentRunnerConfig(timeout=None))
        stage = AgentStage(runner, journal=InMemoryRingBuffer(capacity=1000))

        ctx = RunContext(run_id="r1", session_id="s1", runtime_mode="chained_pipeline")
        turn = TurnContext(turn_id="t1", cancel_token=CancelToken())

        stream = stage.execute_streaming("hi", ctx, turn)
        # Consume up to the first delivered text delta.
        event = await anext(stream)
        while getattr(event, "kind", None) != "text_delta":
            event = await anext(stream)
        assert getattr(event, "text", "") == "Hello world"

        # Ordering guard: the partial turn is not committed yet; the prior
        # turn's AI message is still the tail of graph state.
        assert _content(graph._state.values["messages"][-1]) == "previous turn"
        assert graph.update_state_calls == []

        # Consumer breaks after the first delivered delta.  ``aclose()`` must
        # drive ``_drive_stream``'s cleanup synchronously; ``wait_for`` guards
        # against a non-propagated close hanging on ``Event().wait()``.
        await asyncio.wait_for(stream.aclose(), timeout=2.0)

        # The partial turn was committed during aclose (BaseException arm),
        # appended after — not overwriting — the prior turn's message.
        msgs = graph._state.values["messages"]
        assert msgs[0] is prior_ai
        assert _content(msgs[-1]) == "Hello world"
        assert msgs[-1] is not prior_ai

        # apply_interruption truncates *this* turn; the prior turn is intact.
        bridge.apply_interruption("Hello world", CancellationMode.IMMEDIATE_STOP)
        msgs = graph._state.values["messages"]
        assert _content(msgs[-1]) == "Hello world..."
        assert _content(msgs[0]) == "previous turn"
