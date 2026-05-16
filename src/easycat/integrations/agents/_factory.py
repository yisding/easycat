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
    - workflow objects with ``on_user_turn(...)`` -> :class:`GenericWorkflowBridge`
    - ``pydantic_graph.Graph`` -> raises :class:`BridgeInputError`
      (requires explicit ``PydanticAIBridge(graph=..., ...)`` construction)
    - ``pydantic_ai.Agent`` -> :class:`PydanticAIBridge` (Agent mode)
    - ``agents.Agent`` (OpenAI Agents SDK) -> :class:`OpenAIAgentsBridge`
    - ``langgraph.graph.state.CompiledStateGraph`` -> :class:`LangGraphBridge`
    - ``langchain_core.runnables.Runnable`` -> :class:`LangChainBridge`

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

    # 4. pydantic_graph.Graph -> error (requires explicit PydanticAIBridge).
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

    # 5. pydantic_ai.Agent -> PydanticAIBridge (Agent mode).
    try:
        from pydantic_ai import Agent as PydanticAgent

        if isinstance(agent, PydanticAgent):
            from easycat.integrations.agents.pydantic_ai import PydanticAIBridge

            return PydanticAIBridge(agent=agent)
    except ImportError:
        pass

    # 6. OpenAI Agents SDK -> OpenAIAgentsBridge.
    try:
        from agents import Agent as OpenAIAgent  # type: ignore[import-untyped]

        if isinstance(agent, OpenAIAgent):
            from easycat.integrations.agents.openai_agents import OpenAIAgentsBridge

            return OpenAIAgentsBridge(agent=agent)
    except ImportError:
        pass

    # 6b. LangGraph compiled graph -> LangGraphBridge (check before
    # plain LangChain Runnable since CompiledStateGraph *is* a Runnable).
    try:
        from langgraph.graph.state import CompiledStateGraph  # type: ignore[import-untyped]

        if isinstance(agent, CompiledStateGraph):
            if getattr(agent, "checkpointer", None) is None:
                raise BridgeInputError(
                    "LangGraph graphs must be compiled with a checkpointer "
                    "to be auto-adapted. Call graph.compile("
                    "checkpointer=InMemorySaver()) or construct "
                    "LangGraphBridge(graph=..., ...) explicitly."
                )
            from easycat.integrations.agents.langgraph import LangGraphBridge

            return LangGraphBridge(graph=agent)
    except ImportError:
        pass

    # 6c. LangChain Runnable -> LangChainBridge.
    try:
        from langchain_core.runnables import Runnable  # type: ignore[import-untyped]

        if isinstance(agent, Runnable):
            from easycat.integrations.agents.langchain import LangChainBridge

            # Bare language models (``ChatOpenAI(...)``, any
            # ``BaseChatModel`` / ``BaseLLM``) are Runnables too, but they
            # reject the default ``{"input": ..., "history": ...}`` dict
            # with ``Invalid input type <class 'dict'>``.  Feed them a
            # message sequence instead so ``EasyConfig.mic(agent=...)``
            # works on the first turn while history still threads through.
            messages_input = _is_language_model(agent)
            return LangChainBridge(runnable=agent, messages_input=messages_input)
    except ImportError:
        pass

    # 7. Realtime-API-shaped objects -> error.
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


def _is_language_model(agent: Any) -> bool:
    """True for a (possibly bound) LangChain language model.

    A bare ``BaseChatModel`` / ``BaseLLM`` — and the same model wrapped
    by ``.bind(...)`` / ``.bind_tools(...)`` / ``.with_config(...)``,
    each of which returns a ``RunnableBinding`` around it — only accept a
    string or message sequence as input, not the ``LangChainBridge``
    default payload dict (they reject it with
    ``Invalid input type <class 'dict'>``).  We peel any
    ``RunnableBinding`` layers off ``.bound`` so a bound chat/LLM is
    still recognised and fed a message sequence on the first turn.
    Returns ``False`` (rather than raising) if ``langchain_core`` is
    unavailable so the caller falls back to the default dict payload.
    """
    try:
        from langchain_core.language_models import (  # type: ignore[import-untyped]
            BaseChatModel,
            BaseLLM,
        )
    except ImportError:
        return False
    try:
        from langchain_core.runnables import (  # type: ignore[import-untyped]
            RunnableBinding,
        )
    except ImportError:
        RunnableBinding = ()  # type: ignore[assignment]
    # ``RunnableBinding`` may nest (e.g. ``.bind_tools(...).with_config(...)``);
    # ``seen`` guards against a pathological self-referential ``.bound``.
    seen: set[int] = set()
    while isinstance(agent, RunnableBinding) and id(agent) not in seen:
        seen.add(id(agent))
        agent = getattr(agent, "bound", None)
    return isinstance(agent, (BaseChatModel, BaseLLM))
