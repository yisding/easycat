"""Deprecated: use easycat.integrations.agents for new bridge-based adapters."""
# ruff: noqa: E402

import warnings

warnings.warn(
    "easycat.agents is deprecated. Use easycat.integrations.agents for new bridge-based "
    "adapters. See docs/migration-debug-first-runtime.md for migration details.",
    DeprecationWarning,
    stacklevel=2,
)

from easycat.agents.base import BaseAgentAdapter, serialize_output  # noqa: E402, F401
from easycat.agents.factory import auto_adapt_agent  # noqa: E402, F401
from easycat.agents.openai_agents import (  # noqa: E402, F401
    OpenAIAgentsAdapter,
    build_openai_agents_adapter,
)
from easycat.agents.pydantic_ai import PydanticAIAdapter  # noqa: E402, F401
from easycat.agents.pydantic_ai_workflow import (  # noqa: E402, F401
    PydanticAIWorkflowAdapter,
    WorkflowTurnResult,
)

__all__ = [
    "BaseAgentAdapter",
    "OpenAIAgentsAdapter",
    "PydanticAIAdapter",
    "PydanticAIWorkflowAdapter",
    "WorkflowTurnResult",
    "auto_adapt_agent",
    "build_openai_agents_adapter",
    "serialize_output",
]
