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

    def test_composed_runnable_keeps_dict_payload(self):
        pytest.importorskip("langchain_core")
        from langchain_core.runnables import RunnableLambda

        from easycat.integrations.agents.langchain import LangChainBridge

        adapted = auto_adapt_agent(RunnableLambda(lambda x: x))
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
