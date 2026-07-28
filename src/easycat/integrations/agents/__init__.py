"""Agent framework bridge integrations.

Public surface for application authors: the bridge classes, the runner
wrapper, the ``AgentTurnInput`` / ``AgentBridgeEvent`` protocol types,
and the ``auto_adapt_agent`` factory.

Bridge *authors* start from
:class:`~easycat.integrations.agents.template.BridgeTemplate` (the
starter base class that owns the boilerplate) and register detection
via :func:`register_agent_detector`.  The recorder and interruption
types required to implement that contract are re-exported here;
additional low-level primitives live in
:mod:`easycat.integrations.agents.base`.
"""

from easycat.integrations.agents._agent_runner import AgentRunner, AgentRunnerConfig
from easycat.integrations.agents._factory import (
    auto_adapt_agent,
    clear_agent_detectors,
    is_reusable_agent_spec,
    register_agent_detector,
)
from easycat.integrations.agents._helpers import INTERRUPTION_NOTE
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
    CancellationMode,
    ExternalAgentBridge,
    FrameworkStateSnapshot,
    InterruptionPlan,
)
from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge
from easycat.integrations.agents.langchain import LangChainBridge
from easycat.integrations.agents.langgraph import LangGraphBridge
from easycat.integrations.agents.llama_agents import LlamaAgentsBridge
from easycat.integrations.agents.openai_agents import OpenAIAgentsBridge
from easycat.integrations.agents.pydantic_ai import PydanticAIBridge
from easycat.integrations.agents.responses_api import RemoteResponsesAPIBridge
from easycat.integrations.agents.template import BridgeTemplate

__all__ = [
    "AgentBridgeEvent",
    "AgentRecorder",
    "AgentRunner",
    "AgentRunnerConfig",
    "AgentTurnInput",
    "BridgeTemplate",
    "CancellationMode",
    "ExternalAgentBridge",
    "FrameworkStateSnapshot",
    "GenericWorkflowBridge",
    "INTERRUPTION_NOTE",
    "InterruptionPlan",
    "LangChainBridge",
    "LangGraphBridge",
    "LlamaAgentsBridge",
    "OpenAIAgentsBridge",
    "PydanticAIBridge",
    "RemoteResponsesAPIBridge",
    "auto_adapt_agent",
    "clear_agent_detectors",
    "is_reusable_agent_spec",
    "register_agent_detector",
]
