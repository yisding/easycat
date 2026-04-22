"""Agent framework bridge integrations.

Public exports for bridge construction and protocol types.
"""

from easycat.integrations.agents._agent_runner import AgentRunner, AgentRunnerConfig
from easycat.integrations.agents._factory import auto_adapt_agent
from easycat.integrations.agents._helpers import (
    INTERRUPTION_NOTE,
    serialize_output,
    split_replacement_by_original_parts,
)
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
    BridgeConfigurationError,
    BridgeInputError,
    CancellationMode,
    CommitRule,
    ConventionViolationError,
    ExecutionCursor,
    ExternalAgentBridge,
    FrameworkStateSnapshot,
    InterruptionPlan,
    MutationInjectedError,
    RecorderContext,
    RecorderInvariantError,
    ShallowModeInterruptionError,
    UnitKind,
)
from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge
from easycat.integrations.agents.langchain import LangChainBridge
from easycat.integrations.agents.langgraph import LangGraphBridge
from easycat.integrations.agents.openai_agents import OpenAIAgentsBridge
from easycat.integrations.agents.pydantic_ai import PydanticAIBridge
from easycat.integrations.agents.responses_api import RemoteResponsesAPIBridge

__all__ = [
    "AgentBridgeEvent",
    "AgentRecorder",
    "AgentRunner",
    "AgentRunnerConfig",
    "AgentTurnInput",
    "BridgeConfigurationError",
    "BridgeInputError",
    "CancellationMode",
    "CommitRule",
    "ConventionViolationError",
    "ExecutionCursor",
    "ExternalAgentBridge",
    "FrameworkStateSnapshot",
    "GenericWorkflowBridge",
    "INTERRUPTION_NOTE",
    "InterruptionPlan",
    "LangChainBridge",
    "LangGraphBridge",
    "MutationInjectedError",
    "OpenAIAgentsBridge",
    "PydanticAIBridge",
    "RecorderContext",
    "RecorderInvariantError",
    "RemoteResponsesAPIBridge",
    "ShallowModeInterruptionError",
    "UnitKind",
    "auto_adapt_agent",
    "serialize_output",
    "split_replacement_by_original_parts",
]
