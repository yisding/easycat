"""Shared LangChain/LangGraph event translator.

Maps LangChain ``astream_events(version="v2")`` dicts to
``AgentBridgeEvent`` instances and records tool phases on the
``AgentRecorder``.  Used by both ``LangChainBridge`` (wrapping any
``Runnable``) and ``LangGraphBridge`` (wrapping a ``CompiledStateGraph``
— which is itself a ``Runnable``).

Uses duck typing — the ``langchain_core`` package is not imported here
so tests can run without it installed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from easycat.integrations.agents.base import AgentBridgeEvent, AgentRecorder


def _chunk_text(chunk: Any) -> str:
    """Extract a string text delta from an ``AIMessageChunk``-like object.

    ``AIMessageChunk.text`` is the framework-provided shortcut that
    flattens ``content_blocks`` to text-only.  It normalizes Anthropic
    ``thinking``, OpenAI ``reasoning``, and multimodal blocks across
    providers, so we try it first and only fall back to manual content
    parsing for duck-typed chunks (tests, custom providers) that don't
    implement the ``.text`` property.
    """
    text = getattr(chunk, "text", None)
    if isinstance(text, str) and text:
        return text
    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return ""


def _custom_event_text(payload: Any) -> str:
    """Extract a TTS-safe text fragment from an ``on_custom_event`` payload.

    Custom events (``dispatch_custom_event`` from LCEL, ``get_stream_writer``
    from LangGraph forwarded as ``("custom", payload)`` chunks) are
    typically UI telemetry — agents that *want* their custom signal
    spoken must label it explicitly via a ``"text"`` / ``"speak"`` /
    ``"say"`` key so we don't accidentally narrate progress strings,
    debug logs, or state diffs.
    """
    if isinstance(payload, dict):
        for key in ("text", "speak", "say"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


# Conventional single-string output keys used by LangChain chains /
# agents (``AgentExecutor`` → ``output``, ``LLMChain`` → ``text``,
# ``ConversationChain`` → ``response``, ``RetrievalQA`` → ``result``,
# QA-with-sources / retrieval chains → ``answer``).  A ``return_direct``
# tool surfaces its result through the same ``{"output": ...}`` shape.
_CHAIN_OUTPUT_KEYS = ("output", "text", "response", "answer", "result")


def _dict_output_text(payload: dict[Any, Any]) -> str:
    """Extract the conventional string answer from a chain-output dict.

    LangChain ``AgentExecutor`` / ``LLMChain`` / ``RetrievalQA`` and a
    ``return_direct`` tool finish with a single-key dict such as
    ``{"output": "..."}`` rather than a bare string.  Speak the value of
    the first conventional output key that holds a non-empty string;
    structured/state dicts (numeric, list, nested-dict, or unrecognized
    keys — graph state, ``with_structured_output(...)`` payloads) match
    nothing and stay out of the audio stream as before.
    """
    for key in _CHAIN_OUTPUT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _plain_chunk_text(chunk: Any) -> str:
    """Extract a string text delta from a non-chat ``on_chain_stream`` chunk.

    ``RunnableLambda`` / generic LCEL stages stream whatever value they
    yield — a ``str``, an ``AIMessageChunk``-like object, a conventional
    ``{"output": "..."}`` chain-result dict, or arbitrary state (state
    dict, BaseModel, ...).  Only the first three shapes carry TTS-safe
    text; returning ``""`` for everything else keeps non-text chain
    payloads (graph state dicts, Pydantic models, ...) out of the audio
    stream.
    """
    if chunk is None:
        return ""
    if isinstance(chunk, str):
        return chunk
    if hasattr(chunk, "content"):
        return _chunk_text(chunk)
    if isinstance(chunk, dict):
        return _dict_output_text(chunk)
    return ""


def _generation_chunk_text(chunk: Any) -> str:
    """Extract text from a ``GenerationChunk``-like ``on_llm_stream`` payload.

    Non-chat LLM streams yield ``GenerationChunk`` objects whose token
    text lives on a ``.text`` attribute (no ``.content``).  Plain
    strings are accepted as a duck-typed fallback for tests/custom
    providers.
    """
    if chunk is None:
        return ""
    if isinstance(chunk, str):
        return chunk
    text = getattr(chunk, "text", None)
    if isinstance(text, str):
        return text
    return ""


def _llm_result_text(output: Any) -> str:
    """Concatenate generation texts from an ``on_llm_end`` ``LLMResult`` payload.

    LangChain forwards ``LLMResult`` through ``astream_events`` either
    as the typed object or as a plain dict; both expose
    ``generations: list[list[Generation]]`` where each ``Generation``
    carries a ``text`` field.  For the common ``n=1`` case the result
    is a single completion string.
    """
    if output is None:
        return ""
    if isinstance(output, dict):
        generations = output.get("generations")
    else:
        generations = getattr(output, "generations", None)
    if not isinstance(generations, list):
        return ""
    parts: list[str] = []
    for group in generations:
        if not isinstance(group, list):
            continue
        for gen in group:
            if isinstance(gen, dict):
                text = gen.get("text")
            else:
                text = getattr(gen, "text", None)
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


@dataclass(frozen=True, slots=True)
class _EventContext:
    """One parsed ``astream_events`` dict plus the shared translation state."""

    event: dict[str, Any]
    data: Any
    name: str
    run_id: str
    recorder: AgentRecorder | None
    state: dict[str, Any] | None


def translate_stream_event(
    event: dict[str, Any],
    recorder: AgentRecorder | None = None,
    state: dict[str, Any] | None = None,
) -> Iterator[AgentBridgeEvent]:
    """Translate one ``astream_events(version="v2")`` event.

    ``event`` is a dict with at least ``event``, ``data`` and ``name``
    keys.  Text deltas yield ``text_delta`` events; tool lifecycle
    transitions are recorded via ``recorder.record_tool_call`` and also
    yielded as ``tool_started`` / ``tool_delta`` / ``tool_result``
    events so the runtime can drive TTS and UI updates.

    ``state`` is an optional caller-owned dict used to dedupe the two
    paths a LangChain tool call surfaces through: the chat-model's
    ``tool_call_chunks`` (which carry the provider tool-call id) and
    the framework's ``on_tool_start`` / ``on_tool_end`` (which carry a
    fresh LangChain ``run_id``).  Without coordination both paths emit
    ``tool_started``, producing an unmatched leading pair for downstream
    consumers that count ``tool_started`` / ``tool_result``.  The state
    dict is mutated in place across calls within a turn.

    Dispatch is a per-event-type registry (``_EVENT_HANDLERS``),
    mirroring the ``_PROVIDER_TO_CONFIG`` registry pattern used by the
    STT/TTS factories: to support a new ``astream_events`` event type,
    add a handler function and one registry entry.
    """
    if not isinstance(event, dict):
        return
    event_type = event.get("event")
    if not isinstance(event_type, str):
        return
    handler = _EVENT_HANDLERS.get(event_type)
    if handler is None:
        return
    ctx = _EventContext(
        event=event,
        data=event.get("data") or {},
        name=event.get("name") or "",
        run_id=event.get("run_id") or "",
        recorder=recorder,
        state=state,
    )
    yield from handler(ctx)


# ── Per-event-type handlers ───────────────────────────────────────


def _on_chat_model_start(ctx: _EventContext) -> Iterator[AgentBridgeEvent]:
    # Track chain run_ids that have a chat-model descendant.  Chat-model
    # chunks are safe to stream directly and their parent/sibling chain
    # streams usually just forward the same tokens, so those chain streams
    # are suppressed in ``_on_chain_stream`` to avoid double-speak.
    # Non-chat LLM chunks are *not* treated the same (see
    # ``_on_llm_start``): when a BaseLLM sits inside an LCEL chain, a
    # downstream parser/redactor may transform its raw text before the root
    # chain output.  In that case the root/node ``on_chain_stream`` is the
    # first public-safe text source and raw ``on_llm_*`` text is skipped.
    if ctx.state is None:
        return
    bag = ctx.state.setdefault("chains_with_chat_model_descendants", set())
    if isinstance(bag, set):
        for pid in ctx.event.get("parent_ids") or ():
            bag.add(str(pid))
    yield from ()


def _on_llm_start(ctx: _EventContext) -> Iterator[AgentBridgeEvent]:
    if ctx.state is None or not ctx.run_id:
        return
    parents = ctx.event.get("parent_ids") or ()
    # Only redact a parented BaseLLM whose parent is an LCEL chain —
    # there a downstream parser/redactor may transform the raw tokens
    # before the root chain emits the public-safe text.  A LangGraph
    # node that *directly* invokes a BaseLLM is different: the node's
    # own ``on_chain_stream`` carries a state dict that
    # ``_dict_output_text`` filters out, so nothing would replace the
    # suppressed tokens and the node would go silent.  Leave those
    # node-direct runs audible by skipping redaction only when the
    # immediate parent is a tracked LangGraph node root.  LangChain v2
    # ``parent_ids`` contains the full ancestor chain (root first), so
    # checking every ancestor would misclassify a BaseLLM nested inside
    # a node's LCEL chain as node-direct and leak its raw tokens before
    # downstream redaction.
    #
    # The immediate-parent check is intentionally strict but
    # fail-*audible*: a node that invokes a BaseLLM through
    # ``.with_retry()`` (the sync ``invoke`` path inserts an extra
    # ``RunnableRetry`` span, so ``parent_ids`` becomes
    # ``[node, retry]`` and the immediate parent is the retry span,
    # not the node root) writing to a non-conventional state key is
    # still recorded here as parented, but its result text is stashed
    # in ``_on_llm_end`` so :meth:`LangGraphBridge._finalize_done`
    # can fall back to it when nothing else surfaced — restoring audio
    # without re-leaking raw tokens whenever the node/chain *did*
    # compose public-safe text (the secure common path).  The other
    # common wrappers — ``.with_config()`` / ``.bind()`` /
    # ``RunnableBinding`` — are inert: they don't insert an
    # intervening run, so the LLM's immediate parent stays the node
    # root and those calls remain audible outright.
    node_roots = ctx.state.get("langgraph_node_run_ids")
    node_root_ids = node_roots if isinstance(node_roots, set) else set()
    immediate_parent = str(parents[-1]) if parents else ""
    if parents and immediate_parent not in node_root_ids:
        parented = ctx.state.setdefault("parented_llm_run_ids", set())
        if isinstance(parented, set):
            parented.add(ctx.run_id)
    yield from ()


def _on_chain_start(ctx: _EventContext) -> Iterator[AgentBridgeEvent]:
    if ctx.state is None or not ctx.run_id:
        return
    # Track the root chain run — the outermost runnable, whose
    # ``on_chain_start`` carries no ``parent_ids``.  For an LCEL chain
    # *without* a model descendant
    # (``RunnableLambda(f) | RunnableLambda(g)``) LangChain emits an
    # ``on_chain_stream`` for every child runnable *and* for the parent
    # that forwards the composed result, so speaking every chunk narrates
    # intermediate values (``"a"``, ``"ab"``, then the final ``"ab"``
    # again).  Only the root run's stream carries the final composed
    # output, so non-root chain streams are deduped in
    # ``_on_chain_stream``.  Chains with chat-model descendants are
    # deduped by the ``chains_with_chat_model_descendants`` suppression;
    # chains with non-chat LLM descendants still use the root/node chain
    # stream so downstream transforms and redactors stay authoritative.
    if "root_chain_run_id" not in ctx.state and not (ctx.event.get("parent_ids") or ()):
        ctx.state["root_chain_run_id"] = ctx.run_id
    # LangGraph drives every graph node as a child runnable of the
    # graph's own root chain, so the LCEL root-chain dedup would
    # silently drop *all* node-level ``on_chain_stream`` text (a plain
    # ``RunnableLambda`` / LCEL node with no chat model produces no
    # other ``text_delta``).  A node entry is the ``on_chain_start``
    # whose run ``name`` equals its ``metadata["langgraph_node"]``;
    # record it as a *node root* so its own composed stream is treated
    # as root-equivalent (forwarded) while the node's deeper LCEL
    # children (``RunnableLambda(f) | RunnableLambda(g)`` intermediates)
    # are still deduped — otherwise the caller hears an intermediate
    # value instead of the final node response.
    metadata = ctx.event.get("metadata")
    node = metadata.get("langgraph_node") if isinstance(metadata, dict) else None
    if node and ctx.name == node:
        node_roots = ctx.state.setdefault("langgraph_node_run_ids", set())
        if isinstance(node_roots, set):
            node_roots.add(ctx.run_id)
    yield from ()


def _chain_stream_suppressed(ctx: _EventContext) -> bool:
    """True when this ``on_chain_stream`` must stay silent to avoid double-speak.

    Two dedup layers, both no-ops for the bare standalone-translator
    contract (``state is None``), which keeps emitting unconditionally:

    * chains with a chat-model descendant — the model's own stream
      already carried the tokens;
    * non-root LCEL child streams — only the root chain (or a LangGraph
      *node root*, treated as root-equivalent) forwards the final
      composed output; children re-yield intermediate/duplicate values.
    """
    if ctx.state is None:
        return False
    bag = ctx.state.get("chains_with_chat_model_descendants")
    if isinstance(bag, set) and bag:
        parents = ctx.event.get("parent_ids") or ()
        if ctx.run_id in bag or any(str(pid) in bag for pid in parents):
            return True
    root = ctx.state.get("root_chain_run_id")
    node_roots = ctx.state.get("langgraph_node_run_ids")
    is_node_root = isinstance(node_roots, set) and ctx.run_id in node_roots
    return bool(
        isinstance(root, str) and root and ctx.run_id and ctx.run_id != root and not is_node_root
    )


def _on_chain_stream(ctx: _EventContext) -> Iterator[AgentBridgeEvent]:
    # Non-chat Runnables (``RunnableLambda``, LCEL stages that stream
    # plain text, etc.) surface their output via ``on_chain_stream``
    # rather than ``on_chat_model_stream``.  Extract a string chunk
    # when one is present; skip silently for non-text chunks so chain
    # events that carry dicts / state objects don't leak into TTS.
    # The dedup rules live in ``_chain_stream_suppressed``.
    if _chain_stream_suppressed(ctx):
        return
    chunk = ctx.data.get("chunk") if isinstance(ctx.data, dict) else None
    text = _plain_chunk_text(chunk)
    if text:
        yield AgentBridgeEvent(kind="text_delta", text=text)


def _on_chat_model_stream(ctx: _EventContext) -> Iterator[AgentBridgeEvent]:
    chunk = ctx.data.get("chunk") if isinstance(ctx.data, dict) else None
    if chunk is None:
        return
    text = _chunk_text(chunk)
    if text and ctx.state is not None and ctx.run_id:
        # Track that this run actually streamed text so the
        # ``on_chat_model_end`` fallback doesn't double-emit on top
        # of the already-streamed tokens.  Tool-call-only chunks
        # (no text content) don't count: a model that only yields
        # tool calls and no text should still fall back to its
        # ``on_chat_model_end`` AIMessage text, which is normally
        # empty for pure tool calls anyway.
        streamed = ctx.state.setdefault("chat_streamed_run_ids", set())
        if isinstance(streamed, set):
            streamed.add(ctx.run_id)
    if text:
        yield AgentBridgeEvent(kind="text_delta", text=text)

    for tc_chunk in getattr(chunk, "tool_call_chunks", None) or []:
        if isinstance(tc_chunk, dict):
            yield from _translate_tool_call_chunk(ctx, tc_chunk)


def _translate_tool_call_chunk(
    ctx: _EventContext, tc_chunk: dict[str, Any]
) -> Iterator[AgentBridgeEvent]:
    """Translate one chat-model ``ToolCallChunk`` into tool start/delta events."""
    tc_name = tc_chunk.get("name") or ""
    tc_args = tc_chunk.get("args") or ""
    tc_id = tc_chunk.get("id") or ""
    tc_index = tc_chunk.get("index")
    is_call_start = bool(tc_chunk.get("name"))
    # Most streaming providers (OpenAI, Anthropic, ...) put the
    # tool-call ``id``/``name`` only on the *first* ToolCallChunk
    # of a call; subsequent argument chunks carry just ``index``.
    # Cache the id/name by (run_id, index) on first sight and
    # back-fill it onto the args-only chunks so ``tool_delta``
    # events and the journal delta phase stay associated with the
    # originating ``tool_started`` instead of getting empty
    # id/name.  The gate below still keys ``tool_started`` off the
    # raw ``name`` so a back-filled name never re-announces a
    # second start for the same call.
    if ctx.state is not None and tc_index is not None:
        idmap = ctx.state.setdefault("tool_chunk_id_by_index", {})
        ikey = (ctx.run_id, tc_index)
        if tc_id or tc_name:
            cached_id, cached_name = idmap.get(ikey, ("", ""))
            idmap[ikey] = (tc_id or cached_id, tc_name or cached_name)
        cached_id, cached_name = idmap.get(ikey, (tc_id, tc_name))
        tc_id = tc_id or cached_id
        tc_name = tc_name or cached_name
    if is_call_start:
        if ctx.state is not None and tc_name and tc_id:
            # FIFO queue per tool name so parallel calls to the
            # same tool (e.g. two ``search`` calls in one
            # response) don't overwrite each other — each later
            # ``on_tool_start`` for that name consumes the
            # next queued id rather than the last-seen one.
            chunk_started = ctx.state.setdefault("chunk_started_by_name", {})
            chunk_started.setdefault(tc_name, []).append(tc_id)
        if ctx.recorder is not None:
            ctx.recorder.record_tool_call(
                phase="start",
                name=tc_name,
                call_id=tc_id or None,
            )
        yield AgentBridgeEvent(
            kind="tool_started",
            tool_name=tc_name,
            call_id=tc_id,
        )
    if tc_args:
        if ctx.recorder is not None:
            ctx.recorder.record_tool_call(
                phase="delta",
                name=tc_name or "",
                call_id=tc_id or None,
            )
        yield AgentBridgeEvent(
            kind="tool_delta",
            tool_name=tc_name,
            call_id=tc_id,
            text=tc_args,
        )


def _on_llm_stream(ctx: _EventContext) -> Iterator[AgentBridgeEvent]:
    # Bare non-chat LLM Runnables (``BaseLLM`` subclasses such as
    # text-completion models) have no parent chain, so their
    # ``on_llm_stream`` tokens are the only text source.  When the LLM
    # is inside an LCEL chain, however, downstream components may
    # transform or redact the raw generation before the root chain
    # emits it.  Skip parented raw LLM tokens and let the chain-stream
    # path speak the composed output instead.
    if ctx.state is not None and ctx.run_id:
        parented = ctx.state.get("parented_llm_run_ids")
        if isinstance(parented, set) and ctx.run_id in parented:
            return
    chunk = ctx.data.get("chunk") if isinstance(ctx.data, dict) else None
    text = _generation_chunk_text(chunk)
    if text and ctx.state is not None and ctx.run_id:
        # Only mark the run as streamed once it has actually yielded
        # text.  Some non-chat LLMs emit an ``on_llm_stream`` with an
        # empty/metadata-only chunk and then deliver the completion
        # in ``on_llm_end``; marking the run streamed on the empty
        # chunk would make the ``on_llm_end`` fallback return early
        # and — with the parent chain stream suppressed for model
        # descendants — leave the response empty.  Mirrors
        # ``_on_chat_model_stream``.
        streamed = ctx.state.setdefault("llm_streamed_run_ids", set())
        if isinstance(streamed, set):
            streamed.add(ctx.run_id)
    if text:
        yield AgentBridgeEvent(kind="text_delta", text=text)


def _on_chat_model_end(ctx: _EventContext) -> Iterator[AgentBridgeEvent]:
    # Non-streaming chat models (any chat model that doesn't override
    # ``_stream`` / ``_astream``) only surface their AIMessage via
    # ``on_chat_model_end`` — no ``on_chat_model_stream`` events fire
    # and the parent chain's stream chunks carrying the same message
    # are suppressed by ``chains_with_chat_model_descendants``.  Without
    # this handler the assistant's text is dropped entirely and the
    # voice stays silent.  Skip runs that already streamed text so we
    # don't double-emit on top of their stream chunks.
    if ctx.state is not None and ctx.run_id:
        streamed = ctx.state.get("chat_streamed_run_ids")
        if isinstance(streamed, set) and ctx.run_id in streamed:
            return
    output = ctx.data.get("output") if isinstance(ctx.data, dict) else None
    text = _chunk_text(output) if output is not None else ""
    if text:
        yield AgentBridgeEvent(kind="text_delta", text=text)


def _on_llm_end(ctx: _EventContext) -> Iterator[AgentBridgeEvent]:
    # Bare non-streaming LLMs only surface their answer via
    # ``on_llm_end``.  For parented LLMs, use the surrounding chain's
    # composed stream/final output instead so downstream redaction or
    # output selection is not bypassed.  Skip streaming LLMs so we
    # don't double-emit on top of already-translated bare LLM chunks.
    output = ctx.data.get("output") if isinstance(ctx.data, dict) else None
    if ctx.state is not None and ctx.run_id:
        parented = ctx.state.get("parented_llm_run_ids")
        if isinstance(parented, set) and ctx.run_id in parented:
            # Don't yield the raw tokens (downstream redaction stays
            # authoritative), but stash the result text keyed by
            # ``run_id`` so :meth:`LangGraphBridge._finalize_done` can
            # use it as a fail-audible fallback when the node/chain
            # produced no public-safe text at all (a node-direct LLM
            # reached through an inert ``.with_retry()`` span writing
            # to a non-conventional state key).  The secure common
            # path — node/chain composed text — still wins, so this
            # never re-leaks raw tokens when redaction did run.
            suppressed = ctx.state.setdefault("suppressed_parented_llm_text", {})
            if isinstance(suppressed, dict):
                suppressed[ctx.run_id] = _llm_result_text(output)
            return
        streamed = ctx.state.get("llm_streamed_run_ids")
        if isinstance(streamed, set) and ctx.run_id in streamed:
            return
    text = _llm_result_text(output)
    if text:
        yield AgentBridgeEvent(kind="text_delta", text=text)


def _on_custom_event(ctx: _EventContext) -> Iterator[AgentBridgeEvent]:
    # Surfaced by LCEL ``dispatch_custom_event`` calls.  We only feed
    # it to TTS when the payload explicitly carries a text-shaped
    # field — bare progress dicts (UI telemetry) stay silent so
    # arbitrary chain instrumentation doesn't leak into audio.
    text = _custom_event_text(ctx.data)
    if text:
        yield AgentBridgeEvent(kind="text_delta", text=text)


def _on_tool_start(ctx: _EventContext) -> Iterator[AgentBridgeEvent]:
    tool_name = ctx.name
    # Default to the LangChain run_id; substitute the provider tool-call
    # id when the chat-model's chunk path already announced a started
    # tool of the same name so the matching ``on_tool_end`` can pair
    # with the original ``tool_started``.
    call_id = ctx.run_id
    chunk_call_id = ""
    if ctx.state is not None and tool_name:
        chunk_started = ctx.state.get("chunk_started_by_name")
        if isinstance(chunk_started, dict):
            chunk_ids = chunk_started.get(tool_name)
            if isinstance(chunk_ids, list) and chunk_ids:
                chunk_call_id = chunk_ids.pop(0)
                if not chunk_ids:
                    chunk_started.pop(tool_name, None)
    if chunk_call_id:
        if ctx.state is not None and ctx.run_id:
            run_to_call = ctx.state.setdefault("run_id_to_call_id", {})
            run_to_call[ctx.run_id] = chunk_call_id
        return
    args_input = ctx.data.get("input") if isinstance(ctx.data, dict) else None
    args_text = ""
    if isinstance(args_input, dict):
        # Best-effort JSON-ish preview for tool_started payload.
        try:
            args_text = json.dumps(args_input, default=str)
        except Exception:
            args_text = str(args_input)
    elif args_input is not None:
        args_text = str(args_input)
    if ctx.recorder is not None:
        ctx.recorder.record_tool_call(
            phase="start",
            name=tool_name,
            call_id=call_id or None,
        )
    yield AgentBridgeEvent(
        kind="tool_started",
        tool_name=tool_name,
        call_id=call_id,
        text=args_text,
    )


def _on_tool_end(ctx: _EventContext) -> Iterator[AgentBridgeEvent]:
    tool_name = ctx.name
    call_id = _resolve_tool_call_id(ctx.state, ctx.run_id)
    output = ctx.data.get("output") if isinstance(ctx.data, dict) else None
    result_text = ""
    if output is not None:
        content = getattr(output, "content", None)
        if isinstance(content, str):
            result_text = content
        elif isinstance(content, list):
            result_text = "".join(
                str(b.get("text", ""))
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        else:
            result_text = str(output)
    if ctx.recorder is not None:
        ctx.recorder.record_tool_call(
            phase="result",
            name=tool_name,
            call_id=call_id or None,
        )
    yield AgentBridgeEvent(
        kind="tool_result",
        tool_name=tool_name,
        call_id=call_id,
        result=result_text,
    )


def _on_tool_error(ctx: _EventContext) -> Iterator[AgentBridgeEvent]:
    tool_name = ctx.name
    call_id = _resolve_tool_call_id(ctx.state, ctx.run_id)
    if ctx.recorder is not None:
        ctx.recorder.record_tool_call(
            phase="error",
            name=tool_name,
            call_id=call_id or None,
        )
    # No dedicated event kind for tool errors in the public bridge
    # surface; surface it as a tool_result with empty result and a
    # reason carried on the event.
    yield AgentBridgeEvent(
        kind="tool_result",
        tool_name=tool_name,
        call_id=call_id,
        reason="tool_error",
    )


def _resolve_tool_call_id(state: dict[str, Any] | None, run_id: str) -> str:
    """Map a LangChain tool ``run_id`` back to the provider call-id, if known."""
    if state is None or not run_id:
        return run_id
    run_to_call = state.get("run_id_to_call_id")
    if isinstance(run_to_call, dict):
        mapped = run_to_call.pop(run_id, None)
        if isinstance(mapped, str) and mapped:
            return mapped
    return run_id


# Per-event-type dispatch registry (the ``_PROVIDER_TO_CONFIG`` pattern):
# adding support for a new ``astream_events`` event type is one handler
# function plus one entry here.
_EVENT_HANDLERS: dict[str, Callable[[_EventContext], Iterator[AgentBridgeEvent]]] = {
    "on_chat_model_start": _on_chat_model_start,
    "on_llm_start": _on_llm_start,
    "on_chain_start": _on_chain_start,
    "on_chain_stream": _on_chain_stream,
    "on_chat_model_stream": _on_chat_model_stream,
    "on_llm_stream": _on_llm_stream,
    "on_chat_model_end": _on_chat_model_end,
    "on_llm_end": _on_llm_end,
    "on_custom_event": _on_custom_event,
    "on_tool_start": _on_tool_start,
    "on_tool_end": _on_tool_end,
    "on_tool_error": _on_tool_error,
}
