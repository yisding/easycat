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
    RecorderContext,
    RecorderInvariantError,
    ShallowModeInterruptionError,
    UnitKind,
)

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
    "RecorderContext",
    "RecorderInvariantError",
    "ShallowModeInterruptionError",
    "UnitKind",
]
