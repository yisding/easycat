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

from collections.abc import Iterator
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
    spoken should label it explicitly via a ``"text"`` / ``"speak"``
    key so we don't accidentally narrate progress dicts or state diffs.
    """
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("text", "speak", "say"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _plain_chunk_text(chunk: Any) -> str:
    """Extract a string text delta from a non-chat ``on_chain_stream`` chunk.

    ``RunnableLambda`` / generic LCEL stages stream whatever value they
    yield — a ``str``, an ``AIMessageChunk``-like object, or arbitrary
    state (dict, BaseModel, ...).  Only the first two shapes carry TTS-
    safe text; returning ``""`` for everything else keeps non-text chain
    payloads (graph state dicts, Pydantic models, ...) out of the audio
    stream.
    """
    if chunk is None:
        return ""
    if isinstance(chunk, str):
        return chunk
    if hasattr(chunk, "content"):
        return _chunk_text(chunk)
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
    """
    if not isinstance(event, dict):
        return
    event_type = event.get("event")
    if not isinstance(event_type, str):
        return

    data = event.get("data") or {}
    name = event.get("name") or ""
    run_id = event.get("run_id") or ""

    # Track chain run_ids that have a chat_model / non-chat LLM as a
    # descendant.  Their own ``on_chain_stream`` chunks (and the chunks
    # of their *downstream sibling* runnables — ``StrOutputParser``,
    # ``RunnableLambda``, anything LCEL composes after the model) forward
    # the same tokens already emitted via ``on_chat_model_stream`` /
    # ``on_llm_stream``.  Without suppression a chain like
    # ``prompt | model | parser | lambda`` would speak ``abcABC`` —
    # once from the model stream, once from each downstream stage that
    # re-yields the parsed string.  This also handles nested LCEL inside
    # LangGraph nodes (their state ``on_chain_stream`` chunks are
    # filtered out separately by ``_plain_chunk_text``).
    if state is not None and event_type in ("on_chat_model_start", "on_llm_start"):
        bag = state.setdefault("chains_with_model_descendants", set())
        if isinstance(bag, set):
            for pid in event.get("parent_ids") or ():
                bag.add(str(pid))
    # Track the root chain run — the outermost runnable, whose
    # ``on_chain_start`` carries no ``parent_ids``.  For an LCEL chain
    # *without* a model descendant
    # (``RunnableLambda(f) | RunnableLambda(g)``) LangChain emits an
    # ``on_chain_stream`` for every child runnable *and* for the parent
    # that forwards the composed result, so speaking every chunk narrates
    # intermediate values (``"a"``, ``"ab"``, then the final ``"ab"``
    # again).  Only the root run's stream carries the final composed
    # output, so non-root chain streams are deduped in the
    # ``on_chain_stream`` branch below.  (A chain *with* a model
    # descendant is instead deduped by the suppression just below — the
    # model stream is the canonical token source there.)
    if (
        state is not None
        and event_type == "on_chain_start"
        and "root_chain_run_id" not in state
        and not (event.get("parent_ids") or ())
        and run_id
    ):
        state["root_chain_run_id"] = run_id
    if event_type == "on_chain_stream" and state is not None:
        bag = state.get("chains_with_model_descendants")
        if isinstance(bag, set) and bag:
            parents = event.get("parent_ids") or ()
            if run_id in bag or any(str(pid) in bag for pid in parents):
                return

    if event_type == "on_chat_model_stream":
        chunk = data.get("chunk") if isinstance(data, dict) else None
        if chunk is None:
            return
        text = _chunk_text(chunk)
        if text and state is not None and run_id:
            # Track that this run actually streamed text so the
            # ``on_chat_model_end`` fallback doesn't double-emit on top
            # of the already-streamed tokens.  Tool-call-only chunks
            # (no text content) don't count: a model that only yields
            # tool calls and no text should still fall back to its
            # ``on_chat_model_end`` AIMessage text, which is normally
            # empty for pure tool calls anyway.
            streamed = state.setdefault("chat_streamed_run_ids", set())
            if isinstance(streamed, set):
                streamed.add(run_id)
        if text:
            yield AgentBridgeEvent(kind="text_delta", text=text)

        tool_call_chunks = getattr(chunk, "tool_call_chunks", None) or []
        for tc_chunk in tool_call_chunks:
            if not isinstance(tc_chunk, dict):
                continue
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
            if state is not None and tc_index is not None:
                idmap = state.setdefault("tool_chunk_id_by_index", {})
                ikey = (run_id, tc_index)
                if tc_id or tc_name:
                    cached_id, cached_name = idmap.get(ikey, ("", ""))
                    idmap[ikey] = (tc_id or cached_id, tc_name or cached_name)
                cached_id, cached_name = idmap.get(ikey, (tc_id, tc_name))
                tc_id = tc_id or cached_id
                tc_name = tc_name or cached_name
            if is_call_start:
                if state is not None and tc_name and tc_id:
                    # FIFO queue per tool name so parallel calls to the
                    # same tool (e.g. two ``search`` calls in one
                    # response) don't overwrite each other — each later
                    # ``on_tool_start`` for that name consumes the
                    # next queued id rather than the last-seen one.
                    chunk_started = state.setdefault("chunk_started_by_name", {})
                    chunk_started.setdefault(tc_name, []).append(tc_id)
                if recorder is not None:
                    recorder.record_tool_call(
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
                if recorder is not None:
                    recorder.record_tool_call(
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

    elif event_type == "on_chain_stream":
        # Non-chat Runnables (``RunnableLambda``, LCEL stages that stream
        # plain text, etc.) surface their output via ``on_chain_stream``
        # rather than ``on_chat_model_stream``.  Extract a string chunk
        # when one is present; skip silently for non-text chunks so chain
        # events that carry dicts / state objects don't leak into TTS.
        #
        # Dedupe nested LCEL streams: once the root chain run is known,
        # only it forwards the final composed output — child runnables
        # re-yield intermediate/duplicate values that would double-speak.
        # A bare ``translate_stream_event`` call with no ``state`` (the
        # standalone-translator contract used by the unit tests) keeps
        # emitting unconditionally.
        if state is not None:
            root = state.get("root_chain_run_id")
            if isinstance(root, str) and root and run_id and run_id != root:
                return
        chunk = data.get("chunk") if isinstance(data, dict) else None
        text = _plain_chunk_text(chunk)
        if text:
            yield AgentBridgeEvent(kind="text_delta", text=text)

    elif event_type == "on_llm_stream":
        # Non-chat LLM Runnables (``BaseLLM`` subclasses such as
        # ``OpenAI`` text-completion) emit ``on_llm_stream`` instead of
        # ``on_chat_model_stream``.  The bridge marks the parent chain
        # run as having a model descendant so its forwarded
        # ``on_chain_stream`` chunks are suppressed (avoiding double-
        # emit); without translating these events the LLM's text would
        # be silently dropped.
        chunk = data.get("chunk") if isinstance(data, dict) else None
        text = _generation_chunk_text(chunk)
        if state is not None and run_id:
            streamed = state.setdefault("llm_streamed_run_ids", set())
            if isinstance(streamed, set):
                streamed.add(run_id)
        if text:
            yield AgentBridgeEvent(kind="text_delta", text=text)

    elif event_type == "on_chat_model_end":
        # Non-streaming chat models (any chat model that doesn't override
        # ``_stream`` / ``_astream``) only surface their AIMessage via
        # ``on_chat_model_end`` — no ``on_chat_model_stream`` events fire
        # and the parent chain's stream chunks carrying the same message
        # are suppressed by ``chains_with_model_descendants``.  Without
        # this handler the assistant's text is dropped entirely and the
        # voice stays silent.  Skip runs that already streamed text so we
        # don't double-emit on top of their stream chunks.
        if state is not None and run_id:
            streamed = state.get("chat_streamed_run_ids")
            if isinstance(streamed, set) and run_id in streamed:
                return
        output = data.get("output") if isinstance(data, dict) else None
        text = _chunk_text(output) if output is not None else ""
        if text:
            yield AgentBridgeEvent(kind="text_delta", text=text)

    elif event_type == "on_llm_end":
        # Non-streaming LLMs (``FakeStreamingListLLM``, any ``BaseLLM``
        # that doesn't override ``_stream``) only surface their output
        # via ``on_llm_end`` — no ``on_llm_stream`` events fire and the
        # parent chain's chunks are suppressed by the bridge.  Without
        # this handler the LLM's result is dropped entirely.  Skip
        # streaming LLMs so we don't double-emit on top of their
        # already-translated stream chunks.
        if state is not None and run_id:
            streamed = state.get("llm_streamed_run_ids")
            if isinstance(streamed, set) and run_id in streamed:
                return
        output = data.get("output") if isinstance(data, dict) else None
        text = _llm_result_text(output)
        if text:
            yield AgentBridgeEvent(kind="text_delta", text=text)

    elif event_type == "on_custom_event":
        # Surfaced by LCEL ``dispatch_custom_event`` calls.  We only feed
        # it to TTS when the payload explicitly carries a text-shaped
        # field — bare progress dicts (UI telemetry) stay silent so
        # arbitrary chain instrumentation doesn't leak into audio.
        text = _custom_event_text(data)
        if text:
            yield AgentBridgeEvent(kind="text_delta", text=text)

    elif event_type == "on_tool_start":
        tool_name = name
        # Default to the LangChain run_id; substitute the provider tool-call
        # id when the chat-model's chunk path already announced a started
        # tool of the same name so the matching ``on_tool_end`` can pair
        # with the original ``tool_started``.
        call_id = run_id
        chunk_call_id = ""
        if state is not None and tool_name:
            chunk_started = state.get("chunk_started_by_name")
            if isinstance(chunk_started, dict):
                chunk_ids = chunk_started.get(tool_name)
                if isinstance(chunk_ids, list) and chunk_ids:
                    chunk_call_id = chunk_ids.pop(0)
                    if not chunk_ids:
                        chunk_started.pop(tool_name, None)
        if chunk_call_id:
            if state is not None and run_id:
                run_to_call = state.setdefault("run_id_to_call_id", {})
                run_to_call[run_id] = chunk_call_id
            return
        args_input = data.get("input") if isinstance(data, dict) else None
        args_text = ""
        if isinstance(args_input, dict):
            # Best-effort JSON-ish preview for tool_started payload.
            try:
                import json

                args_text = json.dumps(args_input, default=str)
            except Exception:
                args_text = str(args_input)
        elif args_input is not None:
            args_text = str(args_input)
        if recorder is not None:
            recorder.record_tool_call(
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

    elif event_type == "on_tool_end":
        tool_name = name
        call_id = _resolve_tool_call_id(state, run_id)
        output = data.get("output") if isinstance(data, dict) else None
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
        if recorder is not None:
            recorder.record_tool_call(
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

    elif event_type == "on_tool_error":
        tool_name = name
        call_id = _resolve_tool_call_id(state, run_id)
        if recorder is not None:
            recorder.record_tool_call(
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
