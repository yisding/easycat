"""Static consumer contract for every shipped agent bridge."""

from easycat.integrations.agents import (
    AgentRunner,
    GenericWorkflowBridge,
    LangChainBridge,
    LangGraphBridge,
    LlamaAgentsBridge,
    OpenAIAgentsBridge,
    PydanticAIBridge,
    RemoteResponsesAPIBridge,
)
from easycat.integrations.agents.base import ExternalAgentBridge


def accept_shipped_bridges(
    runner: AgentRunner,
    generic: GenericWorkflowBridge,
    langchain: LangChainBridge,
    langgraph: LangGraphBridge,
    llama: LlamaAgentsBridge,
    openai: OpenAIAgentsBridge,
    pydantic_ai: PydanticAIBridge,
    responses_api: RemoteResponsesAPIBridge,
) -> None:
    bridge: ExternalAgentBridge
    bridge = runner
    bridge = generic
    bridge = langchain
    bridge = langgraph
    bridge = llama
    bridge = openai
    bridge = pydantic_ai
    bridge = responses_api
    del bridge
