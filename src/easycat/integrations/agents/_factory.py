"""Agent auto-detection and bridge construction."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from easycat.integrations.agents.base import BridgeInputError, ExternalAgentBridge


@dataclass(frozen=True, slots=True)
class _AgentDetector:
    predicate: Callable[[Any], bool]
    bridge_factory: Callable[[Any], Any]


@dataclass(frozen=True, slots=True)
class _AdaptedAgent:
    value: Any


_AgentAdapter = Callable[[Any, str | None], _AdaptedAgent | None]


# Custom detectors run after the AgentRunner/bridge guards and before the
# ordered built-in adapter pipeline.
_AGENT_DETECTORS: list[_AgentDetector] = []


def register_agent_detector(
    predicate: Callable[[Any], bool],
    bridge_factory: Callable[[Any], Any],
) -> None:
    """Register a custom detector for :func:`auto_adapt_agent`.

    ``predicate(agent)`` returning ``True`` routes ``agent`` to
    ``bridge_factory(agent)``, which must return an
    :class:`~easycat.integrations.agents.base.ExternalAgentBridge`
    (e.g. a :class:`~easycat.integrations.agents.template.BridgeTemplate`
    subclass).  Detectors are consulted in registration order, *after*
    the ``AgentRunner`` unwrap and the bridge passthrough but *before*
    the built-in framework branches — so a custom detector can claim an
    object a built-in branch would otherwise match.

    Registration is programmatic only (call this from your application
    or plugin setup code); there is no entry-point or config-file
    mechanism.  Predicate exceptions propagate — keep predicates cheap
    and defensive (``isinstance`` / duck-type checks).
    """
    _AGENT_DETECTORS.append(_AgentDetector(predicate, bridge_factory))


def clear_agent_detectors() -> None:
    """Remove all detectors registered via :func:`register_agent_detector`."""
    _AGENT_DETECTORS.clear()


def auto_adapt_agent(agent: Any, *, model: str | None = None) -> Any:
    """Wrap known third-party agent objects in an :class:`ExternalAgentBridge`.

    Supported auto-detected frameworks:

    - URL string -> :class:`RemoteResponsesAPIBridge`
    - ``ExternalAgentBridge`` -> pass-through
    - any object matched by a :func:`register_agent_detector` predicate
      -> that detector's ``bridge_factory(agent)``
    - ``workflows.Workflow`` / LlamaIndex workflow -> :class:`LlamaAgentsBridge`
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
    url_match = _adapt_url(agent, model)
    if url_match is not None:
        return url_match.value

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

    # Custom detectors retain precedence over every built-in framework.
    for detector in _AGENT_DETECTORS:
        if detector.predicate(agent):
            return detector.bridge_factory(agent)

    for adapter in _BUILTIN_AGENT_ADAPTERS:
        match = adapter(agent, model)
        if match is not None:
            return match.value

    # Plain ``async run(text)`` agents are returned unchanged.  The
    # factory (create_session / create_text_session) decides whether to
    # wrap them in :class:`AgentRunner` and with what
    # :class:`AgentRunnerConfig`; ``AgentStage`` provides a default-config
    # safety wrap for callers that construct ``Session`` directly.
    return agent


def _adapt_url(agent: Any, model: str | None) -> _AdaptedAgent | None:
    if not isinstance(agent, str):
        return None

    from urllib.parse import urlparse

    parsed = urlparse(agent)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    if model is None:
        raise BridgeInputError(
            "auto_adapt_agent() requires model= when agent is a URL. "
            "Pass model= explicitly or use create_session(agent=url, "
            "agent_model=...) instead."
        )
    from easycat.integrations.agents.responses_api import RemoteResponsesAPIBridge

    return _AdaptedAgent(RemoteResponsesAPIBridge(base_url=agent, model=model))


def _adapt_llama_workflow(agent: Any, _model: str | None) -> _AdaptedAgent | None:
    from easycat.integrations.agents.llama_agents import is_llama_workflow_instance

    if not is_llama_workflow_instance(agent):
        return None
    from easycat.integrations.agents.llama_agents import LlamaAgentsBridge

    return _AdaptedAgent(LlamaAgentsBridge(workflow=agent))


_BRIDGE_SUPPLIED_WORKFLOW_KWARGS = frozenset({"recorder", "cancel_token"})


def _adapt_generic_workflow(agent: Any, _model: str | None) -> _AdaptedAgent | None:
    on_user_turn = getattr(agent, "on_user_turn", None)
    if not callable(on_user_turn) or isinstance(agent, type):
        return None
    if not _workflow_signature_is_supported(on_user_turn):
        return None
    from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge

    return _AdaptedAgent(GenericWorkflowBridge(workflow=agent))


def _workflow_signature_is_supported(on_user_turn: Callable[..., Any]) -> bool:
    try:
        parameters = inspect.signature(on_user_turn).parameters.values()
    except (ValueError, TypeError):
        return True

    positional = [
        parameter
        for parameter in parameters
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        and parameter.default is inspect.Parameter.empty
    ]
    required_keyword_only = [
        parameter
        for parameter in parameters
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY
        and parameter.default is inspect.Parameter.empty
    ]
    unsupplied_keyword_only = [
        parameter
        for parameter in required_keyword_only
        if parameter.name not in _BRIDGE_SUPPLIED_WORKFLOW_KWARGS
    ]
    if len(positional) > 1:
        raise BridgeInputError(
            f"on_user_turn() has {len(positional)} required positional "
            "parameters but GenericWorkflowBridge only passes (text). "
            "Remove extra required parameters or construct the bridge explicitly."
        )
    if unsupplied_keyword_only:
        names = ", ".join(parameter.name for parameter in unsupplied_keyword_only)
        raise BridgeInputError(
            "on_user_turn() has required keyword-only parameter(s) "
            f"({names}) that GenericWorkflowBridge cannot supply. "
            "Remove required keyword-only parameters or construct the bridge explicitly."
        )
    return len(positional) == 1


def _reject_pydantic_graph(agent: Any, _model: str | None) -> _AdaptedAgent | None:
    try:
        from pydantic_graph import Graph as PydanticGraph
    except ImportError:
        return None
    if not isinstance(agent, PydanticGraph):
        return None
    raise BridgeInputError(
        "pydantic_graph.Graph requires explicit bridge construction: "
        "PydanticAIBridge(graph=..., state_factory=..., initial_node_factory=...)"
    )


def _adapt_pydantic_agent(agent: Any, _model: str | None) -> _AdaptedAgent | None:
    try:
        from pydantic_ai import Agent as PydanticAgent
    except ImportError:
        return None
    if not isinstance(agent, PydanticAgent):
        return None
    from easycat.integrations.agents.pydantic_ai import PydanticAIBridge

    return _AdaptedAgent(PydanticAIBridge(agent=agent))


def _adapt_openai_agent(agent: Any, _model: str | None) -> _AdaptedAgent | None:
    try:
        from agents import Agent as OpenAIAgent
    except ImportError:
        return None
    if not isinstance(agent, OpenAIAgent):
        return None
    from easycat.integrations.agents.openai_agents import OpenAIAgentsBridge

    return _AdaptedAgent(OpenAIAgentsBridge(agent=agent))


def _adapt_langgraph(agent: Any, _model: str | None) -> _AdaptedAgent | None:
    compiled_graph = _unwrap_compiled_state_graph(agent)
    if compiled_graph is None:
        return None
    if getattr(compiled_graph, "checkpointer", None) is None:
        raise BridgeInputError(
            "LangGraph graphs must be compiled with a checkpointer "
            "to be auto-adapted. Call graph.compile("
            "checkpointer=InMemorySaver()) or construct "
            "LangGraphBridge(graph=..., ...) explicitly."
        )
    from easycat.integrations.agents.langgraph import LangGraphBridge

    return _AdaptedAgent(LangGraphBridge(graph=compiled_graph))


def _adapt_langchain(agent: Any, _model: str | None) -> _AdaptedAgent | None:
    try:
        from langchain_core.runnables import Runnable
    except ImportError:
        return None
    if not isinstance(agent, Runnable):
        return None
    from easycat.integrations.agents.langchain import LangChainBridge

    # Bare language models need a message sequence rather than the default
    # ``{"input": ..., "history": ...}`` mapping accepted by compositions.
    messages_input = _is_language_model(agent)
    return _AdaptedAgent(LangChainBridge(runnable=agent, messages_input=messages_input))


def _reject_realtime_agent(agent: Any, _model: str | None) -> _AdaptedAgent | None:
    cls_name = type(agent).__name__
    if "Realtime" in cls_name or hasattr(agent, f"create_{'realtime'}_session"):
        raise BridgeInputError(
            "Voice-to-voice / realtime API objects cannot be auto-adapted. "
            "EasyCat is a chained voice runtime; use the provider SDK directly "
            "for realtime speech-to-speech."
        )
    return None


# Order is policy: LangGraph must precede LangChain because a compiled graph is
# also a Runnable, while realtime rejection deliberately comes after every
# supported framework adapter.
_BUILTIN_AGENT_ADAPTERS: tuple[_AgentAdapter, ...] = (
    _adapt_llama_workflow,
    _adapt_generic_workflow,
    _reject_pydantic_graph,
    _adapt_pydantic_agent,
    _adapt_openai_agent,
    _adapt_langgraph,
    _adapt_langchain,
    _reject_realtime_agent,
)


# ``configurable`` keys that pin a runnable to one conversation. A bound value
# for any of these is resolved identically by every per-session bridge, so the
# spec cannot be safely shared across concurrent connections.
_CONVERSATION_PIN_KEYS = ("thread_id", "checkpoint_id", "session_id")


@dataclass(frozen=True, slots=True)
class _RunnableLayer:
    wrapper: Any
    preserves_graph_api: bool


def _runnable_pins_conversation(agent: Any) -> bool:
    """Return ``True`` when a LangChain/LangGraph ``Runnable`` binds a
    per-conversation key (``thread_id`` / ``checkpoint_id`` / ``session_id``)
    via ``with_config(configurable={...})``.

    Reuses the same SDK-free ``RunnableBinding`` walk the LangGraph bridge uses
    to hoist a bound ``thread_id`` (:func:`._bound_config`), so the detection
    here cannot drift from the value the bridge would actually resolve.
    """
    from easycat.integrations.agents.langgraph import _bound_config

    configurable = _bound_config(agent).get("configurable")
    if not isinstance(configurable, dict):
        return False
    return any(configurable.get(key) for key in _CONVERSATION_PIN_KEYS)


def is_reusable_agent_spec(agent: Any) -> bool:
    """Return ``True`` when *agent* is a declarative framework spec safe to
    reuse across concurrent per-connection sessions.

    A per-connection server forwards the same ``agent`` value into a fresh
    :class:`~easycat.config.EasyConfig` for every connection, and
    :func:`auto_adapt_agent` runs again on each one.  That is safe for a
    *declarative framework spec* — the OpenAI Agents SDK ``Agent``, a PydanticAI
    ``Agent``, a LangChain ``Runnable`` (which also covers a compiled LangGraph
    graph), or a LlamaIndex workflow — because the per-session *bridge*
    (rebuilt from the spec for each connection) is what owns the mutable
    per-session state, not the wrapped spec:

    * The OpenAI / PydanticAI / LangChain bridges keep conversation history on
      the bridge instance (``_message_history`` / per-bridge history store),
      not on the wrapped agent.
    * The LangGraph bridge mints a *fresh* ``thread_id`` per bridge, so
      concurrent connections sharing one compiled graph read/write isolated
      checkpointer threads.
    * LlamaIndex workflow runs allocate their own per-run context.

    Cloning the spec per connection (e.g. ``copy.deepcopy``) is *not* used: it
    is lossy for these objects — deep-copying a compiled LangGraph graph also
    copies its checkpointer, breaking an intentionally shared persistent store —
    whereas the per-bridge isolation above already provides the correct
    boundary. This keeps the documented quickstart
    ``VoiceApp(agent=Agent(...)).run("browser")`` working out of the box.

    It returns ``False`` for anything EasyCat cannot prove is rebuilt fresh:
    an already-constructed :class:`~easycat.integrations.agents.base.ExternalAgentBridge`
    or :class:`AgentRunner` (passed through by reference, carrying per-session
    conversation/stream state), and any unrecognized object such as a plain
    ``async run(text)`` callable or a custom workflow (reused by reference).
    Those must be supplied through a per-connection ``config_factory`` that
    constructs a fresh agent per connection.  ``str`` URLs/provider names are
    handled by the caller as primitives and never reach this predicate.

    A LangChain/LangGraph ``Runnable`` that pins a *conversation* via
    ``with_config(configurable={...})`` is the one exception to "a fresh bridge
    means no shared state": a bound ``thread_id`` / ``checkpoint_id`` (LangGraph)
    or ``session_id`` (LangChain history) is resolved identically by every
    per-session bridge, so all concurrent connections would read and write the
    *same* checkpointer thread / history store and corrupt each other. Such a
    runnable is rejected here so it is routed through a ``config_factory``.

    Advanced setups that push *per-session* mutable framework configuration onto
    a single shared instance (e.g. distinct MCP servers per connection, which
    the OpenAI bridge applies to the wrapped agent for the duration of a turn)
    should also use a ``config_factory`` so each connection owns its own agent.
    """
    # OpenAI Agents SDK ``Agent`` -> fresh ``OpenAIAgentsBridge`` per session.
    try:
        from agents import Agent as OpenAIAgent

        if isinstance(agent, OpenAIAgent):
            return True
    except ImportError:
        pass
    # PydanticAI ``Agent`` -> fresh ``PydanticAIBridge`` per session. A
    # ``pydantic_graph.Graph`` is intentionally excluded: it is not
    # auto-adaptable and requires explicit ``PydanticAIBridge`` construction.
    try:
        from pydantic_ai import Agent as PydanticAgent

        if isinstance(agent, PydanticAgent):
            return True
    except ImportError:
        pass
    # LangChain ``Runnable`` -> fresh ``LangChainBridge`` / ``LangGraphBridge``
    # per session (a compiled LangGraph graph is itself a ``Runnable``) — unless
    # it pins a conversation via ``with_config(configurable={...})``, which every
    # per-session bridge would resolve to the same shared thread/history.
    try:
        from langchain_core.runnables import Runnable

        if isinstance(agent, Runnable):
            return not _runnable_pins_conversation(agent)
    except ImportError:
        pass
    # LlamaIndex / LlamaAgents workflow -> fresh ``LlamaAgentsBridge`` per
    # session (workflow runs allocate their own per-run context).
    from easycat.integrations.agents.llama_agents import is_llama_workflow_instance

    return is_llama_workflow_instance(agent)


def _unwrap_compiled_state_graph(agent: Any) -> Any | None:
    """Return the object :class:`LangGraphBridge` should drive for a
    (possibly wrapped) ``CompiledStateGraph``, else ``None``.

    A compiled LangGraph graph is a ``CompiledStateGraph``, but wrapping
    it in a generic ``Runnable`` combinator — ``graph.bind(...)``,
    ``graph.with_listeners(...)``, ``graph.with_config(...)``,
    ``graph.with_types(...)``, ``graph.with_retry(...)`` — hides it
    inside a ``RunnableBinding`` / ``RunnableRetry`` whose real graph
    sits on ``.bound``.  It must still route through
    :class:`LangGraphBridge` (not the plain :class:`LangChainBridge`,
    which supplies ``configurable.session_id`` where LangGraph requires
    ``thread_id`` and crashes a checkpointed graph on the first turn),
    *without silently dropping the wrapper's behaviour*.

    The two wrapper families differ:

    * ``RunnableBinding`` (``bind`` / ``with_config`` / ``with_listeners``
      / ``with_types``) overrides ``astream_events`` to apply its bound
      kwargs + merged config (listeners included) and proxies *every
      other* attribute to ``.bound``.  So when the chain from a binding
      down to the graph is **all** ``RunnableBinding``, the bridge can
      drive that binding directly: ``astream_events`` honours the
      binding while ``graph.checkpointer`` / ``get_state`` / ``channels``
      proxy through to the real graph.  We therefore return that
      outermost preservable binding — peeling here would drop bound
      kwargs and listeners.
    * ``RunnableRetry`` (``with_retry``) does *not* proxy attribute
      access (so the bridge's ``graph.checkpointer`` probe would see
      ``None``) and does *not* override the streaming path the bridge
      uses — its retry only wraps ``invoke``/``batch``, so it is inert
      on ``astream_events`` and nothing is lost by peeling it.  A retry
      anywhere in the chain also breaks the binding proxy for everything
      above it.

    So peel only the non-preservable prefix (an outer ``RunnableRetry``,
    or a ``RunnableBinding`` sitting *above* a ``RunnableRetry`` whose
    proxy is broken by it) and execute through the deepest object whose
    descent to the graph is all-``RunnableBinding`` — or the bare graph
    when no binding directly wraps it.

    A peeled layer that carries only ``.config`` (a ``with_config`` /
    ``with_types`` / inert ``with_retry``) loses nothing material: its
    config is collected and re-applied onto the returned object via
    ``.with_config(...)`` (innermost→outermost so an outer wrapper's
    value wins and ``configurable`` sub-dicts deep-merge — matching
    LangChain ``with_config`` and
    :func:`~easycat.integrations.agents.langgraph._bound_config`).  But a
    peeled layer carrying *behaviour* re-applying ``.config`` cannot
    reproduce — non-empty bound ``.kwargs`` (a ``bind(**kwargs)``) or
    ``.config_factories`` (a ``with_listeners(...)``) stranded above a
    ``RunnableRetry`` — would be **silently dropped**.  Rather than do
    that we raise :class:`BridgeInputError`: the only honest options for
    a wrapper whose semantics cannot be preserved are to reject it or
    drive it with a custom bridge, and ``with_retry()`` interposed
    between such a binding and the graph makes pure-proxy execution
    impossible (the retry neither proxies the state API nor retries the
    streaming path).  Returns ``None`` (caller falls back to the plain
    Runnable branch) when ``langgraph`` is unavailable.
    """
    runtime_types = _langgraph_runtime_types()
    if runtime_types is None:
        return None
    graph_type, binding_types, binding_base_types = runtime_types
    chain = _walk_compiled_graph_chain(
        agent,
        graph_type=graph_type,
        binding_types=binding_types,
        binding_base_types=binding_base_types,
    )
    if chain is None:
        return None
    graph, layers = chain
    target, peeled_layers = _select_graph_target(graph, layers)
    return _restore_peeled_wrapper_config(target, peeled_layers)


def _langgraph_runtime_types() -> (
    tuple[type[Any], tuple[type[Any], ...], tuple[type[Any], ...]] | None
):
    try:
        from langgraph.graph.state import CompiledStateGraph
    except ImportError:
        return None
    try:
        from langchain_core.runnables.base import RunnableBinding, RunnableBindingBase
    except ImportError:
        return CompiledStateGraph, (), ()
    return CompiledStateGraph, (RunnableBinding,), (RunnableBindingBase,)


def _walk_compiled_graph_chain(
    agent: Any,
    *,
    graph_type: type[Any],
    binding_types: tuple[type[Any], ...],
    binding_base_types: tuple[type[Any], ...],
) -> tuple[Any, list[_RunnableLayer]] | None:
    """Return the concrete graph and its outer-to-inner wrapper chain."""
    seen: set[int] = set()
    layers: list[_RunnableLayer] = []
    node = agent
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        if isinstance(node, graph_type):
            return node, layers
        if isinstance(node, binding_base_types):
            layers.append(
                _RunnableLayer(
                    wrapper=node,
                    preserves_graph_api=isinstance(node, binding_types),
                )
            )
            node = getattr(node, "bound", None)
            continue
        break
    return None


def _select_graph_target(
    graph: Any,
    layers: list[_RunnableLayer],
) -> tuple[Any, list[_RunnableLayer]]:
    """Choose the deepest target with an intact graph-state API proxy."""
    # The longest innermost run of consecutive ``RunnableBinding`` layers
    # (those directly above the graph) is drivable through-the-wrapper:
    # its ``astream_events`` applies every binding and ``__getattr__``
    # proxies the state API down to the graph. ``target_index`` identifies
    # the outermost such binding; everything before it is non-preservable (a
    # retry, or a binding whose proxy a retry below it has broken).
    target_index = len(layers)
    while target_index > 0 and layers[target_index - 1].preserves_graph_api:
        target_index -= 1
    target = layers[target_index].wrapper if target_index < len(layers) else graph
    return target, layers[:target_index]


def _restore_peeled_wrapper_config(target: Any, layers: list[_RunnableLayer]) -> Any:
    """Reject lost behavior and reapply config from wrappers that were peeled."""
    peeled_configs: list[dict[str, Any]] = []
    for layer in layers:
        wrapper = layer.wrapper
        if getattr(wrapper, "kwargs", None) or getattr(wrapper, "config_factories", None):
            raise BridgeInputError(
                "Cannot auto-adapt a LangGraph graph whose .bind(**kwargs) / "
                ".with_listeners(...) wrapper is interposed by .with_retry(): "
                "RunnableRetry neither exposes the graph's state API nor "
                "retries the streaming path the voice bridge drives, so the "
                "wrapper's behaviour would be silently dropped. Compile that "
                "behaviour into the graph (or drop the .with_retry()), or "
                "construct LangGraphBridge(graph=...) yourself and drive the "
                "wrapper explicitly."
            )
        cfg = getattr(wrapper, "config", None)
        if isinstance(cfg, dict) and cfg:
            peeled_configs.append(cfg)
    if not peeled_configs:
        return target
    return target.with_config(_merge_wrapper_configs(peeled_configs))


def _merge_wrapper_configs(layers: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge ``RunnableBinding`` config layers (outermost first) into one
    config dict, innermost→outermost so an outer wrapper's value wins and
    ``configurable`` sub-dicts deep-merge rather than replace."""
    merged: dict[str, Any] = {}
    configurable: dict[str, Any] = {}
    for layer in reversed(layers):
        for key, value in layer.items():
            if key == "configurable" and isinstance(value, dict):
                configurable.update(value)
            else:
                merged[key] = value
    if configurable:
        merged["configurable"] = configurable
    return merged


def _is_language_model(agent: Any) -> bool:
    """True for a (possibly bound) LangChain language model — or a
    model-first LCEL sequence whose first step is one.

    A bare ``BaseChatModel`` / ``BaseLLM`` — and the same model wrapped
    by ``.bind(...)`` / ``.bind_tools(...)`` / ``.with_config(...)``
    (each returns a ``RunnableBinding``) or ``.with_retry(...)`` (returns
    a ``RunnableRetry``) — only accept a string or message sequence as
    input, not the ``LangChainBridge`` default payload dict (they reject
    it with ``Invalid input type <class 'dict'>``).  Both wrapper
    families subclass ``RunnableBindingBase`` and expose the wrapped
    model on ``.bound``, so we peel any ``RunnableBindingBase`` layers
    off ``.bound`` — a bound *and* retried chat/LLM is still recognised
    and fed a message sequence on the first turn.

    The same crash hits *model-first* LCEL compositions: the **first**
    step of a ``RunnableSequence`` receives the runnable's raw input, so
    ``ChatOpenAI() | StrOutputParser()`` and
    ``ChatOpenAI().with_structured_output(...)`` (which compiles to a
    sequence whose head is a bound model) feed the model directly and
    reject the dict payload just like a bare model.  We descend into a
    sequence's first step (peeling binding layers around it too) and
    recognise it the same way — while a ``prompt | model`` chain keeps
    the dict payload because its head is the prompt template, which
    *wants* the prompt variables dict.  Returns ``False`` (rather than
    raising) if ``langchain_core`` is unavailable so the caller falls
    back to the default dict payload.
    """
    try:
        from langchain_core.language_models import (
            BaseChatModel,
            BaseLLM,
        )
    except ImportError:
        return False
    try:
        from langchain_core.runnables.base import (
            RunnableBindingBase,
            RunnableSequence,
        )
    except ImportError:
        runnable_binding_base_types: tuple[type[Any], ...] = ()
        runnable_sequence_types: tuple[type[Any], ...] = ()
    else:
        runnable_binding_base_types = (RunnableBindingBase,)
        runnable_sequence_types = (RunnableSequence,)
    # ``RunnableBindingBase`` may nest (e.g.
    # ``.bind_tools(...).with_config(...).with_retry()``) and a
    # model-first sequence may itself sit under a binding or nest another
    # sequence as its head; ``seen`` guards against a pathological
    # self-referential ``.bound`` / ``.first``.
    seen: set[int] = set()
    while agent is not None and id(agent) not in seen:
        seen.add(id(agent))
        if isinstance(agent, runnable_binding_base_types):
            agent = getattr(agent, "bound", None)
            continue
        if isinstance(agent, runnable_sequence_types):
            first = getattr(agent, "first", None)
            if first is None:
                steps = getattr(agent, "steps", None)
                first = steps[0] if steps else None
            agent = first
            continue
        break
    return isinstance(agent, (BaseChatModel, BaseLLM))
