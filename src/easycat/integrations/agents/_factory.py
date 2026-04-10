"""Agent auto-detection and bridge construction."""

from __future__ import annotations

import inspect
from typing import Any

from easycat.integrations.agents._base_adapter import BaseAgentAdapter


def auto_adapt_agent(agent: Any) -> Any:
    """Wrap known third-party agent objects in an EasyCat adapter.

    Routes framework agents through bridges wrapped in
    :class:`BridgeAdapterShim` so they participate in the debug-first
    journal pipeline.

    Supported auto-detected frameworks:

    - ``ExternalAgentBridge`` -> :class:`BridgeAdapterShim` (pass-through)
    - workflow objects with ``on_user_turn(...)`` -> :class:`GenericWorkflowBridge`
    - ``pydantic_graph.Graph`` -> raises :class:`BridgeInputError`
      (requires explicit ``PydanticAIBridge(graph=..., ...)`` construction)
    - ``pydantic_ai.Agent`` -> :class:`PydanticAIBridge` (Agent mode)
    - ``agents.Agent`` (OpenAI Agents SDK) -> :class:`OpenAIAgentsBridge`

    Unknown agent types are returned unchanged.
    """
    # 0. URL string -> ResponsesAPIBridge.
    if isinstance(agent, str):
        from urllib.parse import urlparse

        parsed = urlparse(agent)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            from easycat.integrations.agents._bridge_adapter_shim import BridgeAdapterShim
            from easycat.integrations.agents.responses_api import ResponsesAPIBridge

            return BridgeAdapterShim(ResponsesAPIBridge(base_url=agent, model="default"))

    # 1. Already a bridge -- wrap in shim if not already wrapped.
    try:
        from easycat.integrations.agents.base import ExternalAgentBridge

        if isinstance(agent, ExternalAgentBridge):
            from easycat.integrations.agents._bridge_adapter_shim import BridgeAdapterShim

            if isinstance(agent, BridgeAdapterShim):
                return agent
            return BridgeAdapterShim(agent)
    except ImportError:
        pass

    # 2. Already an adapter -- return as-is.
    if isinstance(agent, BaseAgentAdapter):
        return agent

    # 3. Workflow with on_user_turn(...) -> GenericWorkflowBridge.
    on_user_turn = getattr(agent, "on_user_turn", None)
    if callable(on_user_turn) and not isinstance(agent, type):
        try:
            sig = inspect.signature(on_user_turn)
            positional = [
                p
                for p in sig.parameters.values()
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty
            ]
            if len(positional) >= 1:
                from easycat.integrations.agents._bridge_adapter_shim import BridgeAdapterShim
                from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge

                return BridgeAdapterShim(GenericWorkflowBridge(workflow=agent))
        except (ValueError, TypeError):
            from easycat.integrations.agents._bridge_adapter_shim import BridgeAdapterShim
            from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge

            return BridgeAdapterShim(GenericWorkflowBridge(workflow=agent))

    # 4. pydantic_graph.Graph -> error (requires explicit PydanticAIBridge).
    try:
        from pydantic_graph import Graph as PydanticGraph  # type: ignore[import-untyped]

        if isinstance(agent, PydanticGraph):
            from easycat.integrations.agents.base import BridgeInputError

            raise BridgeInputError(
                "pydantic_graph.Graph requires explicit bridge construction: "
                "PydanticAIBridge(graph=..., state_factory=..., "
                "initial_node_factory=...)"
            )
    except ImportError:
        pass

    # 5. pydantic_ai.Agent -> PydanticAIBridge (Agent mode).
    try:
        from pydantic_ai import Agent as PydanticAgent

        if isinstance(agent, PydanticAgent):
            from easycat.integrations.agents._bridge_adapter_shim import BridgeAdapterShim
            from easycat.integrations.agents.pydantic_ai import PydanticAIBridge

            return BridgeAdapterShim(PydanticAIBridge(agent=agent))
    except ImportError:
        pass

    # 6. OpenAI Agents SDK -> OpenAIAgentsBridge.
    try:
        from agents import Agent as OpenAIAgent  # type: ignore[import-untyped]

        if isinstance(agent, OpenAIAgent):
            from easycat.integrations.agents._bridge_adapter_shim import BridgeAdapterShim
            from easycat.integrations.agents.openai_agents import OpenAIAgentsBridge

            return BridgeAdapterShim(OpenAIAgentsBridge(agent=agent))
    except ImportError:
        pass

    # 7. Realtime-API-shaped objects -> error.
    cls_name = type(agent).__name__
    if "Realtime" in cls_name or hasattr(agent, f"create_{'realtime'}_session"):
        from easycat.integrations.agents.base import BridgeInputError

        raise BridgeInputError(
            "Voice-to-voice / realtime API objects cannot be auto-adapted. "
            "EasyCat is a chained voice runtime; use the provider SDK directly "
            "for realtime speech-to-speech."
        )

    return agent
