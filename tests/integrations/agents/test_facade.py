"""auto_adapt_agent() bridge selection and error paths."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from easycat.cancel import CancelToken
from easycat.integrations.agents._factory import auto_adapt_agent
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
    BridgeInputError,
    CancellationMode,
    CommitRule,
    ExternalAgentBridge,
    FrameworkStateSnapshot,
    UnitKind,
)


class _CustomBridge:
    """Minimal ExternalAgentBridge implementation."""

    COMMITTABLE_BOUNDARIES = {UnitKind.AGENT: CommitRule.BETWEEN_TURNS}

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        yield AgentBridgeEvent(kind="text_delta", text="custom")
        yield AgentBridgeEvent(kind="done", text="custom")

    def snapshot_state(self) -> FrameworkStateSnapshot:
        return FrameworkStateSnapshot(fields={}, kind="custom")

    def apply_interruption(self, delivered_text: str, mode: CancellationMode, **_) -> None:
        pass

    def replace_last_assistant_text(self, text: str) -> None:
        pass

    def append_interruption_note(self, note: str) -> None:
        pass

    def reset(self) -> None:
        pass


class TestAutoAdaptWithBridge:
    def test_bridge_passthrough(self):
        bridge = _CustomBridge()
        assert isinstance(bridge, ExternalAgentBridge)
        assert auto_adapt_agent(bridge) is bridge

    def test_unknown_object_passthrough(self):
        obj = object()
        adapted = auto_adapt_agent(obj)
        assert adapted is obj


class TestAutoAdaptBridgeSelection:
    def test_workflow_shallow_routes_to_generic_workflow_bridge(self):
        from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge

        class _Shallow:
            async def on_user_turn(self, text: str) -> str:
                return text

        adapted = auto_adapt_agent(_Shallow())
        assert isinstance(adapted, GenericWorkflowBridge)
        assert not adapted.deep_mode

    def test_workflow_deep_routes_to_generic_workflow_bridge(self):
        from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge

        class _Deep:
            async def on_user_turn(self, text: str, *, recorder=None, cancel_token=None):
                yield f"deep: {text}"

        adapted = auto_adapt_agent(_Deep())
        assert isinstance(adapted, GenericWorkflowBridge)
        assert adapted.deep_mode

    def test_pydantic_graph_raises_bridge_input_error(self):
        pytest.importorskip("pydantic_graph")
        from pydantic_graph import Graph

        with pytest.raises(BridgeInputError, match="PydanticAIBridge"):
            auto_adapt_agent(Graph(nodes=[]))

    def test_realtime_class_name_raises_bridge_input_error(self):
        class RealtimeClient:
            pass

        with pytest.raises(BridgeInputError, match="realtime"):
            auto_adapt_agent(RealtimeClient())

    def test_realtime_method_raises_bridge_input_error(self):
        class _Client:
            def create_realtime_session(self):
                pass

        with pytest.raises(BridgeInputError, match="realtime"):
            auto_adapt_agent(_Client())


class TestAutoAdaptLangChain:
    """Bare language models vs. composed Runnables route differently."""

    def test_bare_chat_model_uses_messages_input(self):
        pytest.importorskip("langchain_core")
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        from easycat.integrations.agents.langchain import LangChainBridge

        adapted = auto_adapt_agent(FakeListChatModel(responses=["hi"]))
        assert isinstance(adapted, LangChainBridge)
        # Default dict payload would crash a chat model with
        # "Invalid input type <class 'dict'>"; messages mode avoids it.
        assert adapted._messages_input is True

    def test_bare_llm_uses_messages_input(self):
        pytest.importorskip("langchain_core")
        from langchain_core.language_models.fake import FakeListLLM

        from easycat.integrations.agents.langchain import LangChainBridge

        adapted = auto_adapt_agent(FakeListLLM(responses=["hi"]))
        assert isinstance(adapted, LangChainBridge)
        assert adapted._messages_input is True

    def test_retried_chat_model_uses_messages_input(self):
        """``.with_retry()`` returns a ``RunnableRetry`` (a
        ``RunnableBindingBase`` sibling of ``RunnableBinding``, *not* a
        subclass).  Its ``.bound`` must still be peeled so the bare chat
        model is recognised and fed messages, not the dict payload that
        crashes with ``Invalid input type <class 'dict'>``."""
        pytest.importorskip("langchain_core")
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        from easycat.integrations.agents.langchain import LangChainBridge

        model = FakeListChatModel(responses=["hi"]).with_retry()
        adapted = auto_adapt_agent(model)
        assert isinstance(adapted, LangChainBridge)
        assert adapted._messages_input is True

    def test_bound_then_retried_chat_model_uses_messages_input(self):
        """Nested wrappers (``.bind(...).with_retry()``) must all peel."""
        pytest.importorskip("langchain_core")
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        from easycat.integrations.agents.langchain import LangChainBridge

        model = FakeListChatModel(responses=["hi"]).bind(stop=["x"]).with_retry()
        adapted = auto_adapt_agent(model)
        assert isinstance(adapted, LangChainBridge)
        assert adapted._messages_input is True

    def test_composed_runnable_keeps_dict_payload(self):
        pytest.importorskip("langchain_core")
        from langchain_core.runnables import RunnableLambda

        from easycat.integrations.agents.langchain import LangChainBridge

        adapted = auto_adapt_agent(RunnableLambda(lambda x: x))
        assert isinstance(adapted, LangChainBridge)
        assert adapted._messages_input is False

    def test_model_first_sequence_uses_messages_input(self):
        """``ChatOpenAI() | StrOutputParser()`` feeds the model the raw
        input — the default dict payload would crash its first step with
        ``Invalid input type <class 'dict'>``, so it must use messages
        mode just like a bare model.  Also covers a bound model head
        (``model.bind(...) | parser``), the shape ``with_structured_output``
        compiles to."""
        pytest.importorskip("langchain_core")
        from langchain_core.language_models.fake_chat_models import FakeListChatModel
        from langchain_core.output_parsers import StrOutputParser

        from easycat.integrations.agents.langchain import LangChainBridge

        model = FakeListChatModel(responses=["hi"])
        for runnable in (
            model | StrOutputParser(),
            model.bind(stop=["x"]) | StrOutputParser(),
        ):
            adapted = auto_adapt_agent(runnable)
            assert isinstance(adapted, LangChainBridge)
            assert adapted._messages_input is True

    def test_prompt_first_sequence_keeps_dict_payload(self):
        """A ``prompt | model`` chain's first step is the prompt
        template, which *wants* the prompt-variables dict — it must keep
        the default dict payload, not be misdetected as model-first."""
        pytest.importorskip("langchain_core")
        from langchain_core.language_models.fake_chat_models import FakeListChatModel
        from langchain_core.prompts import ChatPromptTemplate

        from easycat.integrations.agents.langchain import LangChainBridge

        chain = ChatPromptTemplate.from_template("{input}") | FakeListChatModel(responses=["hi"])
        adapted = auto_adapt_agent(chain)
        assert isinstance(adapted, LangChainBridge)
        assert adapted._messages_input is False

    @pytest.mark.asyncio
    async def test_bare_chat_model_invokes_without_dict_crash(self):
        """End-to-end: ``EasyConfig.mic(agent=ChatOpenAI(...))`` shape —
        the first turn must not raise ``Invalid input type``."""
        pytest.importorskip("langchain_core")
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        from easycat.integrations.agents._recorder import JournalAgentRecorder
        from easycat.integrations.agents.base import RecorderContext
        from easycat.runtime.journal import InMemoryRingBuffer

        adapted = auto_adapt_agent(FakeListChatModel(responses=["the answer"]))
        rec = JournalAgentRecorder(
            journal=InMemoryRingBuffer(capacity=1000),
            artifact_store=None,
            context=RecorderContext(run_id="r1", session_id="s1", turn_id="t1"),
        )
        events = [ev async for ev in adapted.invoke(AgentTurnInput.from_text("question"), rec)]
        done = [e for e in events if e.kind == "done"]
        assert done and done[0].text == "the answer"


class TestAutoAdaptLangGraph:
    """A compiled LangGraph graph — bare or wrapped by a generic
    Runnable combinator — must route to ``LangGraphBridge``, never the
    plain ``LangChainBridge`` (which would feed it
    ``configurable.session_id`` instead of LangGraph's required
    ``thread_id`` and crash a checkpointed graph on the first turn)."""

    @staticmethod
    def _compiled_graph(*, checkpointer: bool = True):
        pytest.importorskip("langgraph")
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.graph import END, START, StateGraph

        # ``dict`` state schema keeps the graph free of forward-ref
        # annotations — the tests only exercise bridge *selection*.
        g = StateGraph(dict)
        g.add_node("n", lambda s: {})
        g.add_edge(START, "n")
        g.add_edge("n", END)
        return g.compile(checkpointer=InMemorySaver() if checkpointer else None)

    def test_bare_compiled_graph_routes_to_langgraph_bridge(self):
        from easycat.integrations.agents.langgraph import LangGraphBridge

        adapted = auto_adapt_agent(self._compiled_graph())
        assert isinstance(adapted, LangGraphBridge)

    def test_with_types_wrapped_graph_routes_to_langgraph_bridge(self):
        """``graph.with_types(...)`` returns a ``RunnableBinding`` whose
        ``isinstance(CompiledStateGraph)`` is False — its ``.bound`` must
        be peeled so a checkpointed graph isn't sent to LangChainBridge
        and crashed with ``KeyError: 'thread_id'``."""
        from easycat.integrations.agents.langgraph import LangGraphBridge

        wrapped = self._compiled_graph().with_types(input_type=dict, output_type=dict)
        adapted = auto_adapt_agent(wrapped)
        assert isinstance(adapted, LangGraphBridge)

    def test_retried_wrapped_graph_routes_to_langgraph_bridge(self):
        """``.with_retry()`` returns a ``RunnableRetry`` — a
        ``RunnableBindingBase`` that does *not* proxy attribute access,
        so the peeled graph (not the wrapper) must reach the bridge or
        its ``graph.checkpointer`` probe wrongly sees ``None``."""
        from easycat.integrations.agents.langgraph import LangGraphBridge

        adapted = auto_adapt_agent(self._compiled_graph().with_retry())
        assert isinstance(adapted, LangGraphBridge)

    def test_bound_then_retried_graph_routes_to_langgraph_bridge(self):
        from easycat.integrations.agents.langgraph import LangGraphBridge

        adapted = auto_adapt_agent(self._compiled_graph().bind().with_retry())
        assert isinstance(adapted, LangGraphBridge)

    def test_bound_thread_id_survives_wrapper(self):
        """``graph.with_config(configurable={"thread_id": ...})`` is the
        common resume pattern; a later ``.with_types(...)`` must not lose
        it (the peeled graph copy still carries ``.config``)."""
        graph = self._compiled_graph().with_config(configurable={"thread_id": "resume-1"})
        adapted = auto_adapt_agent(graph.with_types(input_type=dict))
        assert adapted._thread_id == "resume-1"

    def test_config_bound_outside_retry_wrapper_survives(self):
        """``graph.with_retry().with_config(configurable={"thread_id"...})``
        lands the config on the *outer* ``RunnableBinding``, not on a
        graph copy; peeling ``.bound`` must re-apply it onto the unwrapped
        graph rather than minting a fresh thread."""
        graph = self._compiled_graph().with_retry()
        adapted = auto_adapt_agent(graph.with_config(configurable={"thread_id": "resume-2"}))
        assert adapted._thread_id == "resume-2"

    def test_config_bound_outside_with_types_wrapper_survives(self):
        graph = self._compiled_graph().with_types(input_type=dict)
        adapted = auto_adapt_agent(graph.with_config(configurable={"thread_id": "resume-3"}))
        assert adapted._thread_id == "resume-3"

    def test_non_thread_configurable_keys_survive_wrapper(self):
        """Non-``thread_id`` ``configurable`` keys (tenant ids, feature
        flags read by nodes) bound outside a wrapper must reach the
        graph's run config too, not just the thread id."""
        graph = self._compiled_graph().bind().with_retry()
        adapted = auto_adapt_agent(
            graph.with_config(configurable={"thread_id": "resume-4", "tenant": "acme"})
        )
        assert adapted._thread_id == "resume-4"
        assert adapted._config()["configurable"]["tenant"] == "acme"

    def test_outer_wrapper_config_wins_over_inner_graph_copy(self):
        """When both an inner ``with_config`` graph copy and an outer
        wrapper bind ``thread_id``, the outer value wins (matching
        LangChain ``with_config`` merge precedence)."""
        graph = self._compiled_graph().with_config(configurable={"thread_id": "inner"})
        adapted = auto_adapt_agent(
            graph.with_retry().with_config(configurable={"thread_id": "outer"})
        )
        assert adapted._thread_id == "outer"

    def test_wrapped_checkpointerless_graph_still_raises(self):
        with pytest.raises(BridgeInputError, match="checkpointer"):
            auto_adapt_agent(self._compiled_graph(checkpointer=False).with_types(input_type=dict))

    # ── Wrapper behaviour must survive, not be silently dropped ──────

    def test_bind_kwargs_survive_via_wrapper(self):
        """``graph.bind(**kwargs)`` is a ``RunnableBinding`` whose
        ``astream_events`` passes the bound kwargs through.  Peeling to
        the bare graph would silently drop them; the bridge must instead
        drive the wrapper (its attribute proxy still exposes the graph's
        checkpointer/state API)."""
        from langchain_core.runnables.base import RunnableBinding

        bound = self._compiled_graph().bind(configurable={"flag": "on"})
        adapted = auto_adapt_agent(bound)
        assert isinstance(adapted._graph, RunnableBinding)
        assert adapted._graph.kwargs == {"configurable": {"flag": "on"}}
        # State API still reachable through the binding's proxy.
        assert adapted._graph.checkpointer is not None

    def test_listeners_survive_via_wrapper(self):
        """``graph.with_listeners(...)`` carries the listener on the
        ``RunnableBinding`` (as a ``config_factories`` entry, not in
        ``.config``), so the old peel-and-reapply-``.config`` path
        dropped it.  Driving the wrapper preserves it."""
        from langchain_core.runnables.base import RunnableBinding

        calls: list[str] = []
        wrapped = self._compiled_graph().with_listeners(on_start=lambda run: calls.append("start"))
        adapted = auto_adapt_agent(wrapped)
        assert isinstance(adapted._graph, RunnableBinding)
        assert getattr(adapted._graph, "config_factories", None)

    def test_retry_is_peeled_to_bare_graph(self):
        """``RunnableRetry`` neither proxies attribute access nor wraps
        the streaming path the bridge drives (its retry only covers
        ``invoke``/``batch``), so it is inert here and must be peeled to
        the bare graph rather than driven."""
        from langgraph.graph.state import CompiledStateGraph

        adapted = auto_adapt_agent(self._compiled_graph().with_retry())
        assert isinstance(adapted._graph, CompiledStateGraph)

    def test_empty_bind_under_retry_still_routes(self):
        """``graph.bind().with_retry()`` carries no behaviour to lose
        (empty kwargs), so it keeps peeling to the bare graph and routes
        — not rejected."""
        from langgraph.graph.state import CompiledStateGraph

        adapted = auto_adapt_agent(self._compiled_graph().bind().with_retry())
        assert isinstance(adapted._graph, CompiledStateGraph)

    def test_bind_kwargs_under_retry_rejected(self):
        """``graph.bind(tag="x").with_retry()`` re-nests as
        ``RunnableBinding(kwargs) → RunnableRetry → graph``: the retry
        breaks the binding's state proxy, so the bound kwargs can be
        neither preserved nor silently dropped — reject loudly."""
        with pytest.raises(BridgeInputError, match="with_retry"):
            auto_adapt_agent(self._compiled_graph().bind(tag="x").with_retry())

    def test_listeners_under_retry_rejected(self):
        with pytest.raises(BridgeInputError, match="with_retry"):
            auto_adapt_agent(
                self._compiled_graph().with_listeners(on_start=lambda run: None).with_retry()
            )

    def test_bind_kwargs_with_config_no_retry_survive(self):
        """Without an interposing retry the chain is all-``RunnableBinding``:
        the bind kwargs *and* an outer ``with_config`` thread id are both
        preserved by driving the wrapper."""
        from langchain_core.runnables.base import RunnableBinding

        chain = (
            self._compiled_graph().bind(k=1).with_config(configurable={"thread_id": "resume-x"})
        )
        adapted = auto_adapt_agent(chain)
        assert isinstance(adapted._graph, RunnableBinding)
        assert adapted._thread_id == "resume-x"
        assert adapted._graph.kwargs == {"k": 1}
        assert adapted._graph.checkpointer is not None
