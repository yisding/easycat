"""Agent auto-detection and bridge construction."""

from __future__ import annotations

import inspect
from typing import Any

from easycat.integrations.agents.base import BridgeInputError, ExternalAgentBridge


def auto_adapt_agent(agent: Any, *, model: str | None = None) -> Any:
    """Wrap known third-party agent objects in an :class:`ExternalAgentBridge`.

    Supported auto-detected frameworks:

    - URL string -> :class:`RemoteResponsesAPIBridge`
    - ``ExternalAgentBridge`` -> pass-through
    - ``workflows.Workflow`` / LlamaIndex workflow -> :class:`LlamaAgentsBridge`
    - workflow objects with ``on_user_turn(...)`` -> :class:`GenericWorkflowBridge`
    - ``pydantic_graph.Graph`` -> raises :class:`BridgeInputError`
      (requires explicit ``PydanticAIBridge(graph=..., ...)`` construction)
    - ``pydantic_ai.Agent`` -> :class:`PydanticAIBridge` (Agent mode)
    - ``agents.Agent`` (OpenAI Agents SDK) -> :class:`OpenAIAgentsBridge`

    Plain objects with ``async run(text) -> str`` but no framework match
    are returned unchanged — the caller (``create_session`` /
    ``AgentStage``) is responsible for wrapping them in
    :class:`AgentRunner` so that user-supplied ``agent_runner`` settings
    (timeout, history, etc.) are honored rather than silently replaced
    with defaults.

    Unknown agent types are returned unchanged.
    """
    # 0. URL string -> RemoteResponsesAPIBridge.
    if isinstance(agent, str):
        from urllib.parse import urlparse

        parsed = urlparse(agent)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            if model is None:
                raise BridgeInputError(
                    "auto_adapt_agent() requires model= when agent is a URL. "
                    "Pass model= explicitly or use create_session(agent=url, "
                    "agent_model=...) instead."
                )
            from easycat.integrations.agents.responses_api import RemoteResponsesAPIBridge

            return RemoteResponsesAPIBridge(base_url=agent, model=model)

    # 1. AgentRunner wrapping a framework object — adapt the inner agent.
    # This must run before the generic ExternalAgentBridge passthrough
    # because AgentRunner itself satisfies ExternalAgentBridge; otherwise
    # AgentRunner(raw_framework_agent) would bypass adaptation and fail
    # on the first turn when AgentRunner tries to call inner.run().
    from easycat.integrations.agents._agent_runner import AgentRunner

    if isinstance(agent, AgentRunner):
        adapted_inner = auto_adapt_agent(agent._agent, model=model)
        if adapted_inner is not agent._agent:
            agent._agent = adapted_inner
            agent._is_bridge = isinstance(adapted_inner, ExternalAgentBridge)
        return agent

    # 2. Already a bridge -- pass through.
    if isinstance(agent, ExternalAgentBridge):
        return agent

    # 3. LlamaAgents / LlamaIndex Workflow -> LlamaAgentsBridge.
    from easycat.integrations.agents.llama_agents import is_llama_workflow_instance

    if is_llama_workflow_instance(agent):
        from easycat.integrations.agents.llama_agents import LlamaAgentsBridge

        return LlamaAgentsBridge(workflow=agent)

    # 4. Workflow with on_user_turn(...) -> GenericWorkflowBridge.
    on_user_turn = getattr(agent, "on_user_turn", None)
    if callable(on_user_turn) and not isinstance(agent, type):
        try:
            sig = inspect.signature(on_user_turn)
            positional = [
                p
                for p in sig.parameters.values()
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty
            ]
            _BRIDGE_SUPPLIED_KW = {"recorder", "cancel_token"}
            required_kw_only = [
                p
                for p in sig.parameters.values()
                if p.kind == p.KEYWORD_ONLY and p.default is p.empty
            ]
            unsupplied_kw = [p for p in required_kw_only if p.name not in _BRIDGE_SUPPLIED_KW]
            if len(positional) == 1 and not unsupplied_kw:
                from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge

                return GenericWorkflowBridge(workflow=agent)
            elif len(positional) > 1:
                raise BridgeInputError(
                    f"on_user_turn() has {len(positional)} required positional "
                    f"parameters but GenericWorkflowBridge only passes (text). "
                    f"Remove extra required parameters or construct the bridge "
                    f"explicitly."
                )
            elif unsupplied_kw:
                names = ", ".join(p.name for p in unsupplied_kw)
                raise BridgeInputError(
                    f"on_user_turn() has required keyword-only parameter(s) "
                    f"({names}) that GenericWorkflowBridge cannot supply. "
                    f"Remove required keyword-only parameters or construct "
                    f"the bridge explicitly."
                )
        except (ValueError, TypeError) as exc:
            if isinstance(exc, BridgeInputError):
                raise
            from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge

            return GenericWorkflowBridge(workflow=agent)

    # 5. pydantic_graph.Graph -> error (requires explicit PydanticAIBridge).
    try:
        from pydantic_graph import Graph as PydanticGraph  # type: ignore[import-untyped]

        if isinstance(agent, PydanticGraph):
            raise BridgeInputError(
                "pydantic_graph.Graph requires explicit bridge construction: "
                "PydanticAIBridge(graph=..., state_factory=..., "
                "initial_node_factory=...)"
            )
    except ImportError:
        pass

    # 6. pydantic_ai.Agent -> PydanticAIBridge (Agent mode).
    try:
        from pydantic_ai import Agent as PydanticAgent

        if isinstance(agent, PydanticAgent):
            from easycat.integrations.agents.pydantic_ai import PydanticAIBridge

            return PydanticAIBridge(agent=agent)
    except ImportError:
        pass

    # 7. OpenAI Agents SDK -> OpenAIAgentsBridge.
    try:
        from agents import Agent as OpenAIAgent  # type: ignore[import-untyped]

        if isinstance(agent, OpenAIAgent):
            from easycat.integrations.agents.openai_agents import OpenAIAgentsBridge

            return OpenAIAgentsBridge(agent=agent)
    except ImportError:
        pass

    # 8. Realtime-API-shaped objects -> error.
    cls_name = type(agent).__name__
    if "Realtime" in cls_name or hasattr(agent, f"create_{'realtime'}_session"):
        raise BridgeInputError(
            "Voice-to-voice / realtime API objects cannot be auto-adapted. "
            "EasyCat is a chained voice runtime; use the provider SDK directly "
            "for realtime speech-to-speech."
        )

    # Plain ``async run(text)`` agents are returned unchanged.  The
    # factory (create_session / create_text_session) decides whether to
    # wrap them in :class:`AgentRunner` and with what
    # :class:`AgentRunnerConfig`; ``AgentStage`` provides a default-config
    # safety wrap for callers that construct ``Session`` directly.
    return agent
