"""Agent framework bridge integrations.

Public surface for application authors: the bridge classes, the runner
wrapper, the ``AgentTurnInput`` / ``AgentBridgeEvent`` protocol types,
and the ``auto_adapt_agent`` factory.

Bridge *authors* extending this package can reach for the lower-level
types (``AgentRecorder``, ``ExecutionCursor``, ``CancellationMode``,
etc.) directly from :mod:`easycat.integrations.agents.base` — they are
intentionally not re-exported here so the voice-application surface
stays small.
"""

from easycat.integrations.agents._agent_runner import AgentRunner, AgentRunnerConfig
from easycat.integrations.agents._factory import auto_adapt_agent
from easycat.integrations.agents._helpers import (
    INTERRUPTION_NOTE,
    serialize_output,
)
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentTurnInput,
    ExternalAgentBridge,
)
from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge
from easycat.integrations.agents.langchain import LangChainBridge
from easycat.integrations.agents.langgraph import LangGraphBridge
from easycat.integrations.agents.llama_agents import LlamaAgentsBridge
from easycat.integrations.agents.openai_agents import OpenAIAgentsBridge
from easycat.integrations.agents.pydantic_ai import PydanticAIBridge
from easycat.integrations.agents.responses_api import RemoteResponsesAPIBridge

__all__ = [
    "AgentBridgeEvent",
    "AgentRunner",
    "AgentRunnerConfig",
    "AgentTurnInput",
    "ExternalAgentBridge",
    "GenericWorkflowBridge",
    "INTERRUPTION_NOTE",
    "LangChainBridge",
    "LangGraphBridge",
    "LlamaAgentsBridge",
    "OpenAIAgentsBridge",
    "PydanticAIBridge",
    "RemoteResponsesAPIBridge",
    "auto_adapt_agent",
    "serialize_output",
]
