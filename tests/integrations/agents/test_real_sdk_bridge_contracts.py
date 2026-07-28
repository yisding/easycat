"""Run the public bridge contract kit against first-party adapter objects.

Each optional-SDK factory skips independently. The nightly extras matrix
installs one extra per cell, so the corresponding class executes against the
real framework while unrelated classes remain skipped.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge
from easycat.integrations.agents.langchain import LangChainBridge
from easycat.integrations.agents.langgraph import LangGraphBridge
from easycat.integrations.agents.llama_agents import LlamaAgentsBridge
from easycat.integrations.agents.openai_agents import OpenAIAgentsBridge
from easycat.integrations.agents.pydantic_ai import PydanticAIBridge
from easycat.integrations.agents.responses_api import RemoteResponsesAPIBridge
from easycat.testing import AgentBridgeContractSuite

from .mock_responses_server import MockResponsesServer

pytestmark = [
    pytest.mark.agent_bridge,
    pytest.mark.surface_agent,
    pytest.mark.provider("real-sdk-bridge"),
]


def _openai_agents_bridge() -> OpenAIAgentsBridge:
    agents = pytest.importorskip("agents")
    from openai.types.responses import (
        Response,
        ResponseCompletedEvent,
        ResponseOutputMessage,
        ResponseOutputText,
    )

    class _OfflineModel(agents.Model):  # type: ignore[name-defined]
        @staticmethod
        def _response() -> Response:
            message = ResponseOutputMessage(
                id="msg-contract",
                content=[
                    ResponseOutputText(
                        annotations=[],
                        text="hello from OpenAI Agents",
                        type="output_text",
                    )
                ],
                role="assistant",
                status="completed",
                type="message",
            )
            return Response(
                id="resp-contract",
                created_at=0.0,
                model="contract-model",
                object="response",
                output=[message],
                parallel_tool_calls=False,
                tool_choice="auto",
                tools=[],
                status="completed",
            )

        async def get_response(self, *args: Any, **kwargs: Any) -> Any:
            return agents.ModelResponse(
                output=self._response().output,
                usage=agents.Usage(),
                response_id="resp-contract",
            )

        async def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            yield ResponseCompletedEvent(
                response=self._response(),
                sequence_number=0,
                type="response.completed",
            )

    agent = agents.Agent(
        name="ContractAgent",
        instructions="Return the scripted response.",
        model=_OfflineModel(),
    )
    return OpenAIAgentsBridge(agent, use_previous_response_id=False)


def _pydantic_ai_bridge() -> PydanticAIBridge:
    pytest.importorskip("pydantic_ai")
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    return PydanticAIBridge(agent=Agent(TestModel(custom_output_text="hello from PydanticAI")))


def _langchain_bridge() -> LangChainBridge:
    pytest.importorskip("langchain_core")
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    model = FakeListChatModel(responses=["hello from LangChain"])
    return LangChainBridge(model, messages_input=True)


def _langgraph_bridge() -> LangGraphBridge:
    pytest.importorskip("langgraph")
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, MessagesState, StateGraph

    def answer(state: MessagesState) -> dict[str, list[AIMessage]]:
        del state
        return {"messages": [AIMessage(content="hello from LangGraph")]}

    builder = StateGraph(MessagesState)
    builder.add_node("answer", answer)
    builder.add_edge(START, "answer")
    builder.add_edge("answer", END)
    return LangGraphBridge(builder.compile(checkpointer=InMemorySaver()))


def _llama_agents_bridge() -> LlamaAgentsBridge:
    workflows = pytest.importorskip("workflows")
    from workflows.events import StartEvent, StopEvent

    async def answer(event: Any) -> Any:
        del event
        return StopEvent(result="hello from Llama workflow")

    # The SDK resolves step annotations when the decorator runs. Assign real
    # classes explicitly so this local factory also works with postponed
    # annotations enabled for the test module.
    answer.__annotations__ = {
        "event": StartEvent,
        "return": StopEvent,
    }

    class _ContractWorkflow(workflows.Workflow):  # type: ignore[name-defined]
        pass

    workflows.step(answer, workflow=_ContractWorkflow)

    return LlamaAgentsBridge(workflow=_ContractWorkflow())


class _GenericWorkflow:
    async def on_user_turn(self, text: str) -> str:
        return f"hello from generic workflow: {text}"

    def apply_interruption(self, delivered_text: str, mode: Any) -> None:
        del delivered_text, mode


def _generic_workflow_bridge() -> GenericWorkflowBridge:
    return GenericWorkflowBridge(_GenericWorkflow())


def _remote_responses_bridge() -> RemoteResponsesAPIBridge:
    server = MockResponsesServer()
    transport = httpx.ASGITransport(app=server)
    bridge = RemoteResponsesAPIBridge(
        base_url="http://contract.test",
        model="contract-model",
        api_key="contract-key",
    )
    bridge._client = httpx.AsyncClient(
        transport=transport,
        base_url="http://contract.test",
    )
    return bridge


class TestRealOpenAIAgentsBridgeContract(AgentBridgeContractSuite):
    provider_factory = staticmethod(_openai_agents_bridge)


class TestRealPydanticAIBridgeContract(AgentBridgeContractSuite):
    provider_factory = staticmethod(_pydantic_ai_bridge)


class TestRealLangChainBridgeContract(AgentBridgeContractSuite):
    provider_factory = staticmethod(_langchain_bridge)


class TestRealLangGraphBridgeContract(AgentBridgeContractSuite):
    provider_factory = staticmethod(_langgraph_bridge)


class TestRealLlamaAgentsBridgeContract(AgentBridgeContractSuite):
    provider_factory = staticmethod(_llama_agents_bridge)


class TestGenericWorkflowBridgeContract(AgentBridgeContractSuite):
    provider_factory = staticmethod(_generic_workflow_bridge)


class TestRemoteResponsesAPIBridgeContract(AgentBridgeContractSuite):
    provider_factory = staticmethod(_remote_responses_bridge)
