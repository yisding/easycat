"""Agent framework bridge integrations.

Public exports for bridge construction and protocol types.
"""

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
from easycat.integrations.agents.openai_agents import OpenAIAgentsBridge
from easycat.integrations.agents.pydantic_ai import PydanticAIBridge

__all__ = [
    "AgentBridgeEvent",
    "AgentRecorder",
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
    "InterruptionPlan",
    "MutationInjectedError",
    "OpenAIAgentsBridge",
    "PydanticAIBridge",
    "RecorderContext",
    "RecorderInvariantError",
    "ShallowModeInterruptionError",
    "UnitKind",
]
