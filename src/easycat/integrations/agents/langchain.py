"""LangChain bridge — wraps a ``Runnable`` via ``astream_events(version="v2")``.

Shallow integration suitable for LCEL chains, LangChain agents, or any
other composition that is *not* built with LangGraph (LangGraph graphs
get the deeper :class:`LangGraphBridge`).  The bridge surfaces text
deltas, tool calls, and unit transitions into the EasyCat journal so
voice-side debugging and barge-in work uniformly across agent
frameworks.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any
from uuid import uuid4

from easycat.cancel import CancelToken
from easycat.integrations.agents._context import normalize_context_messages
from easycat.integrations.agents._helpers import split_replacement_by_original_parts
from easycat.integrations.agents._langchain_events import (
    _plain_chunk_text,
    translate_stream_event,
)
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
    BridgeInputError,
    CancellationMode,
    CommitRule,
    ExecutionCursor,
    FrameworkStateSnapshot,
    InterruptionPlan,
    UnitKind,
)
from easycat.runtime.records import ErrorInfo

logger = logging.getLogger(__name__)


# No default ``include_types`` filter — LangChain's ``astream_events``
# filter drops ``on_custom_event`` (``dispatch_custom_event`` /
# ``adispatch_custom_event``) when ``include_types`` is set, which would
# silently break the custom-event TTS path documented in
# :func:`_custom_event_text`.  ``translate_stream_event`` already
# dispatches on the event *type* string, so unfiltered streams just
# produce more no-op events rather than spurious behaviour.  Callers
# that want to narrow the surface for performance can opt in via
# ``include_types=``.
_DEFAULT_INCLUDE_TYPES: tuple[str, ...] | None = None


class LangChainBridge:
    """Wraps a LangChain ``Runnable`` via ``astream_events(version="v2")``.

    Parameters
    ----------
    runnable:
        Any object implementing the LangChain ``Runnable`` protocol — an
        LCEL chain, a ``RunnableLambda``, a LangChain ``AgentExecutor``,
        etc.  Objects that are actually LangGraph ``CompiledStateGraph``
        instances should go through :class:`LangGraphBridge` instead;
        ``auto_adapt_agent()`` dispatches on the concrete type.
    display_name:
        Optional override for the top-level ``agent`` cursor display
        name (defaults to ``type(runnable).__name__``).
    input_key:
        Key under which ``turn_input.text`` is placed in the runnable's
        input dict.  Defaults to ``"input"`` (matching the LangChain
        Hub convention).  Pass ``None`` to pass the text as a bare
        string (useful for single-prompt runnables).
    history_key:
        Key under which the prior turn messages are placed.  Defaults
        to ``"history"``.  Set to ``None`` to disable history passing.
    messages_input:
        When ``True`` the runnable is fed a bare message *sequence*
        (``[*history, HumanMessage(text)]``) instead of a dict or string.
        Bare LangChain language models (``BaseChatModel`` / ``BaseLLM``
        such as ``ChatOpenAI(...)``) reject dict inputs, so
        ``auto_adapt_agent()`` enables this for them; ``input_key`` /
        ``history_key`` are ignored in this mode (history is threaded as
        messages instead).
    include_types:
        Optional ``astream_events(include_types=...)`` filter.  Defaults
        to ``None`` (surface every event) — narrowing the filter drops
        ``on_custom_event`` from ``dispatch_custom_event``, which would
        silently disable the custom-event TTS path.  Pass an explicit
        tuple only when performance demands it for very chatty chains.
    session_id:
        Explicit ``configurable.session_id`` threaded into every
        ``astream_events`` call.  ``RunnableWithMessageHistory`` (and any
        runnable carrying a ``history_factory_config``) *requires* this
        key — without it the first turn raises ``ValueError: Missing
        keys ['session_id']`` before any event is produced.  Defaults to
        the recorder's session/run id (stable for the life of a
        Session); plain runnables ignore the unknown ``configurable``
        key, so threading it through is always safe.
    config:
        Optional base ``RunnableConfig`` merged into every
        ``astream_events`` call.  Use it for runnables wrapped with a
        custom ``history_factory_config`` (e.g. ``user_id`` /
        ``conversation_id`` instead of ``session_id``); a
        caller-supplied ``configurable.session_id`` is preserved rather
        than overwritten by the resolved default.
    """

    COMMITTABLE_BOUNDARIES = {
        UnitKind.AGENT: CommitRule.BETWEEN_TURNS,
        UnitKind.MODEL_NODE: CommitRule.NON_COMMITTABLE,
        UnitKind.TOOL_CALL: CommitRule.BETWEEN_PHASES,
    }

    def __init__(
        self,
        runnable: Any,
        *,
        display_name: str | None = None,
        input_key: str | None = "input",
        history_key: str | None = "history",
        messages_input: bool = False,
        include_types: Sequence[str] | None = _DEFAULT_INCLUDE_TYPES,
        session_id: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        if runnable is None:
            raise BridgeInputError("LangChainBridge requires a non-None runnable=")
        if not hasattr(runnable, "astream_events"):
            raise BridgeInputError(
                "LangChainBridge requires a LangChain Runnable with astream_events(). "
                "Got: " + type(runnable).__name__
            )
        self._runnable = runnable
        self._display_name = display_name or type(runnable).__name__
        self._input_key = input_key
        self._history_key = history_key
        self._messages_input = messages_input
        self._include_types = list(include_types) if include_types is not None else None
        self._session_id = session_id
        self._base_config = dict(config) if config else None
        # Stable per-bridge fallback so a ``RunnableWithMessageHistory``
        # still threads its history correctly across turns when the
        # bridge is driven without a journal (NullAgentRecorder →
        # ``session_id`` is "").
        self._fallback_session_id = f"easycat-{uuid4().hex}"
        self._message_history: list[Any] = []
        self._last_output: Any = None
        # The ``configurable.session_id`` resolved on the first turn.
        # ``RunnableWithMessageHistory`` keys its own message store by
        # this id; cached so post-hoc history edits can be mirrored into
        # that store (see :meth:`_history_store`) even though
        # ``replace_last_assistant_text`` / ``reset`` get no recorder.
        self._resolved_session_id: str | None = None
        # The full ``configurable`` dict resolved on the first turn.
        # ``RunnableWithMessageHistory`` wrapped with a custom
        # ``history_factory_config`` (e.g. ``user_id`` /
        # ``conversation_id``) calls ``get_session_history`` with those
        # spec ids as keyword args; cached so :meth:`_history_store` can
        # rebuild that exact call and reach the real backing store.
        self._resolved_configurable: dict[str, Any] | None = None

    # ── ExternalAgentBridge interface ─────────────────────────────

    def _stream_config(self, recorder: AgentRecorder) -> dict[str, Any]:
        """Build the ``astream_events`` config for one turn.

        ``RunnableWithMessageHistory`` — explicitly called out as a
        supported runnable — requires ``configurable.session_id`` on
        *every* invoke/stream; without it the first turn raises
        ``ValueError: Missing keys ['session_id']`` before any event is
        produced.  We resolve a stable id (explicit ``session_id=``
        override → recorder session id → per-bridge fallback) and
        thread it through.  Plain runnables ignore unknown
        ``configurable`` keys, so this is always safe.

        The recorder ``run_id`` is deliberately *not* in the chain: it
        rotates every turn (``AgentStage`` mints a fresh ``run-<hex>``
        per turn) and is the shared literal ``"null"`` under
        ``NULL_RECORDER``.  Using it would re-key a wrapped
        ``RunnableWithMessageHistory`` each turn (losing prior
        conversation) and let independent bridges collide.  Whenever
        there is no real session id we fall back to the stable
        per-bridge id instead.

        A caller-supplied ``config=`` is the merge base; we only fill in
        ``session_id`` when the caller didn't already provide one, so
        runnables wrapped with a custom ``history_factory_config`` keep
        their own keys.
        """
        ctx = recorder.context
        resolved = self._session_id or (ctx.session_id or None) or self._fallback_session_id
        config: dict[str, Any] = dict(self._base_config) if self._base_config else {}
        configurable = dict(config.get("configurable") or {})
        configurable.setdefault("session_id", resolved)
        config["configurable"] = configurable
        # Remember the id (and the full configurable) the wrapped history
        # runnable (if any) stores under, so later shadow-list edits can
        # be mirrored into it — including custom multi-key
        # ``history_factory_config`` setups (see :meth:`_history_store`).
        self._resolved_session_id = configurable.get("session_id")
        self._resolved_configurable = configurable
        return config

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        agent_cursor = ExecutionCursor(
            unit_id=f"agent-{uuid4().hex[:8]}",
            unit_kind=UnitKind.AGENT,
            display_name=self._display_name,
            entered_at=time.monotonic_ns(),
            committable=False,
        )
        recorder.record_unit_entered(agent_cursor)

        accumulated = ""
        # Open ``model_node`` cursors for ``on_chat_model_*`` events, keyed
        # by LangChain ``run_id`` so start/end always pair even when the
        # runnable interleaves multiple model calls.
        open_cursors: dict[str, ExecutionCursor] = {}
        # Run ids whose ``_end`` event arrived while a sibling cursor was
        # still on top of the recorder stack — closed in LIFO order once
        # the obstructing sibling(s) also end, preserving the recorder's
        # strict stack invariant for ``RunnableParallel`` / concurrent
        # runs.
        ended_runs: set[str] = set()
        # Track the top-level chain's ``run_id`` so we can capture its
        # ``on_chain_end.data.output`` as ``structured_output`` — non-text
        # runnables (``RunnableLambda(lambda _: {"answer": 42})``,
        # ``.with_structured_output(...)``) produce no ``text_delta`` and
        # would otherwise expose an empty string here.
        root_run_id: str | None = None
        captured_output: Any = None
        captured_output_set = False
        # Shared state for translator-side bookkeeping: tool-call dedup
        # across the chat_model ``tool_call_chunks`` path and the
        # ``on_tool_start`` / ``on_tool_end`` path, plus the set of chain
        # run-ids that have a model descendant (used to suppress chain
        # streams that would otherwise duplicate model tokens).
        tool_state: dict[str, Any] = {}
        # Set when the loop breaks because the cancel token was tripped
        # mid-stream (barge-in).  Unlike a timeout/``aclose()`` (which
        # raises into the ``BaseException`` cleanup), this break falls
        # through to the normal completion path, so the wrapped-store
        # mirroring that path skips must be done explicitly below.
        cancelled = False

        input_payload = self._build_input(turn_input.text, turn_input.context)
        stream_kwargs: dict[str, Any] = {
            "version": "v2",
            "config": self._stream_config(recorder),
        }
        if self._include_types is not None:
            stream_kwargs["include_types"] = self._include_types

        try:
            stream = self._runnable.astream_events(input_payload, **stream_kwargs)
            async for event in stream:
                if cancel_token and cancel_token.is_cancelled:
                    recorder.record_cancellation_boundary(
                        mode=CancellationMode.IMMEDIATE_STOP,
                        reason="cancel_token_set",
                    )
                    cancelled = True
                    break

                event_type = event.get("event") if isinstance(event, dict) else None
                if event_type == "on_chain_start" and not (event.get("parent_ids") or ()):
                    if root_run_id is None:
                        rid = str(event.get("run_id") or "")
                        if rid:
                            root_run_id = rid
                if event_type == "on_chain_end":
                    rid = str(event.get("run_id") or "")
                    if root_run_id is not None and rid == root_run_id:
                        raw_data = event.get("data")
                        data_dict = raw_data if isinstance(raw_data, dict) else {}
                        captured_output = data_dict.get("output") if data_dict else None
                        captured_output_set = True

                self._handle_cursor_lifecycle(
                    event, recorder, agent_cursor, open_cursors, ended_runs
                )
                for bridge_event in translate_stream_event(event, recorder, state=tool_state):
                    if bridge_event.kind == "text_delta":
                        accumulated += bridge_event.text
                    yield bridge_event
        except Exception as exc:
            for cursor in reversed(list(open_cursors.values())):
                try:
                    recorder.record_unit_exited(cursor, reason="error")
                except Exception:
                    logger.debug("Failed to close cursor during error cleanup", exc_info=True)
            recorder.record_framework_error(ErrorInfo.from_exception(exc))
            recorder.record_unit_exited(agent_cursor, reason="error")
            raise
        except BaseException:
            # The default ``AgentRunner`` enforces its timeout by
            # cancelling the pending ``__anext__()`` (and then calling
            # ``aclose()``), injecting ``asyncio.CancelledError`` /
            # ``GeneratorExit`` here — neither is an ``Exception`` so the
            # block above is skipped and the still-open agent/model
            # cursors would be left without ``unit_exited`` records,
            # breaking the recorder's stack invariant for the postmortem
            # journal.  Close them (defensively, so a recorder error
            # can't mask the cancellation) before re-raising.  No
            # ``record_framework_error``: a cancelled turn isn't a
            # framework fault.
            for cursor in reversed(list(open_cursors.values())):
                try:
                    recorder.record_unit_exited(cursor, reason="error")
                except Exception:
                    logger.debug("Failed to close cursor during cancel cleanup", exc_info=True)
            try:
                recorder.record_unit_exited(agent_cursor, reason="error")
            except Exception:
                logger.debug("Failed to close agent cursor during cancel cleanup", exc_info=True)
            # The normal completion path below records this turn into
            # history; a mid-stream cancel (timeout / barge-in aclose())
            # skips it.  Persist the partial turn here — mirroring the
            # OpenAI/PydanticAI bridges, which keep partial output on
            # cancellation — so a follow-up ``apply_interruption()``
            # truncates *this* turn's assistant message instead of
            # rewriting the previous turn's (or no-opping on turn one),
            # which would corrupt/lose conversation history on barge-in.
            try:
                self._append_to_history(turn_input.text, accumulated)
                # ``RunnableWithMessageHistory`` reloads its own per-session
                # store next turn (ignoring the shadow list); the wrapper's
                # save listener never ran on this cancelled turn, so also
                # persist the partial turn there or the store-mirrored
                # ``apply_interruption()`` rewrite would hit the prior turn.
                self._mirror_partial_turn_to_store(turn_input.text, accumulated)
            except Exception:
                logger.debug("Failed to preserve partial LangChain turn on cancel", exc_info=True)
            raise

        for cursor in reversed(list(open_cursors.values())):
            recorder.record_unit_exited(cursor.with_committable(True), reason=None)
        open_cursors.clear()

        # Prefer the top-level chain's actual output for ``structured_output``
        # (a dict / BaseModel / arbitrary value).  Fall back to the
        # accumulated text only when no ``on_chain_end`` was observed — e.g.
        # bare-chat-model runnables that never emit a chain event.
        self._last_output = captured_output if captured_output_set else accumulated

        # When the top-level chain output is text that differs from the
        # raw model tokens, an LCEL stage *after* the model transformed
        # it — e.g. ``model | StrOutputParser() | RunnableLambda(str.upper)``.
        # Those downstream-sibling ``on_chain_stream`` chunks are
        # suppressed (they'd otherwise double-speak the model tokens),
        # so ``accumulated`` holds the pre-transform text.  Record the
        # chain's real output as the final ``done.text`` and history so
        # the response and next-turn conditioning aren't the unmodified
        # internal model output.  Live TTS already streamed the raw
        # tokens; re-emitting here would double-speak, so (like the
        # LangGraph bridge) we only correct the recorded transcript —
        # except when nothing streamed, where ``done.text`` is the
        # consumer's only spoken text and must carry the real answer.
        final_text = accumulated
        if captured_output_set:
            output_text = _plain_chunk_text(captured_output)
            if output_text and output_text != accumulated:
                final_text = output_text

        self._append_to_history(turn_input.text, final_text)
        if cancelled:
            # A cancel-token break stops the stream before a wrapped
            # ``RunnableWithMessageHistory``'s end-of-run save listener
            # fires, so this turn never reaches the backing store (only
            # the ``BaseException`` cancel cleanup mirrors it today).
            # Mirror it here too, or a follow-up ``apply_interruption()``
            # rewrite — which targets the same store — truncates the
            # *previous* turn's assistant message while the wrapped
            # runnable reloads stale history.  Plain runnables expose no
            # store (``_history_store`` → ``None``) and are unaffected;
            # a normally-completed turn already persisted via the
            # wrapper's listener so it is intentionally not mirrored.
            self._mirror_partial_turn_to_store(turn_input.text, final_text)
        recorder.record_unit_exited(agent_cursor.with_committable(True), reason=None)
        yield AgentBridgeEvent(
            kind="done",
            text=final_text,
            structured_output=self._last_output,
        )

    def snapshot_state(self) -> FrameworkStateSnapshot:
        return FrameworkStateSnapshot(
            fields={
                "framework": "langchain",
                "runnable": self._display_name,
                "history_length": len(self._message_history),
            },
            kind="langchain",
        )

    def apply_interruption(
        self,
        delivered_text: str,
        mode: CancellationMode,
        recorder: AgentRecorder | None = None,
        caused_by_signal_id: str | None = None,
    ) -> None:
        plan = self._plan_interruption(delivered_text, mode)

        actual_pre_ref = plan.pre_state_ref
        if recorder is not None:
            actual_pre_ref = recorder.record_state_snapshot(
                plan.pre_state_ref,
                payload=self._serialize_framework_state(),
            )

        if recorder is not None:
            try:
                recorder.record_state_committed(
                    mutation_kind=plan.mutation_kind,
                    pre_state_ref=actual_pre_ref,
                    post_state_ref=plan.post_state_ref,
                )
            except Exception:
                return

        try:
            self._apply_planned_mutation(plan)
        except Exception as exc:
            if recorder is not None:
                recorder.record_interruption_apply_failed(
                    mutation_kind=plan.mutation_kind,
                    pre_state_ref=actual_pre_ref,
                    post_state_ref=plan.post_state_ref,
                    failure_error=ErrorInfo.from_exception(exc),
                )
            raise

        if recorder is not None:
            recorder.record_state_snapshot(
                plan.post_state_ref,
                payload=self._serialize_framework_state(),
            )
            recorder.record_cancellation_boundary(
                mode=mode,
                reason=plan.mutation_kind,
                caused_by_signal_id=caused_by_signal_id,
            )

    def reset(self) -> None:
        self._message_history.clear()
        self._last_output = None
        store = self._history_store()
        if store is not None:
            try:
                store.clear()
            except Exception:
                logger.debug("Failed to clear wrapped history store on reset", exc_info=True)

    # ── History post-processing ───────────────────────────────────

    def replace_last_assistant_text(self, text: str) -> None:
        """Rewrite the last assistant message in history.

        Called by Session after post-processing (e.g. Markdown stripping)
        so the next turn conditions on cleaned text rather than raw LLM
        output.
        """
        self._rewrite_last_ai_content(text)

    def append_interruption_note(self, note: str) -> None:
        """Append an interruption note so the next turn sees it."""
        try:
            from langchain_core.messages import SystemMessage

            new_msg: Any = SystemMessage(content=note)
            self._message_history.append(new_msg)
        except ImportError:
            new_msg = {"role": "system", "content": note}
            self._message_history.append(new_msg)
        except Exception:
            logger.debug("Failed to append interruption note to LangChain history", exc_info=True)
            return
        # ``RunnableWithMessageHistory`` rebuilds the prompt from its own
        # session store and overwrites the bridge's ``history`` key, so
        # the shadow-list append above is invisible to the next turn
        # unless the note is also added to that store.
        store = self._history_store()
        if store is not None:
            try:
                store.add_message(new_msg)
            except Exception:
                logger.debug(
                    "Failed to mirror interruption note into wrapped history store",
                    exc_info=True,
                )

    # ── Internal ─────────────────────────────────────────────────

    def _build_input(
        self,
        text: str,
        context: list[dict[str, str]] | None = None,
    ) -> Any:
        if self._messages_input:
            # Bare ``BaseChatModel`` / ``BaseLLM`` runnables only accept a
            # string or a message sequence — a dict raises ``Invalid input
            # type``.  Thread prior turns (and per-turn context) as
            # messages so the auto-adapted model stays conversational.
            history = self._history_with_context(context)
            return [*history, _context_to_message({"role": "user", "content": text})]
        if self._input_key is None:
            return text
        payload: dict[str, Any] = {self._input_key: text}
        if self._history_key is not None:
            payload[self._history_key] = self._history_with_context(context)
        return payload

    def _history_with_context(self, context: list[dict[str, str]] | None) -> list[Any]:
        """Prepend per-turn system/developer context messages to history.

        The bridge already owns prior conversation state via
        ``_message_history``; per-turn context from Session (caller-id
        metadata, system-prefix instructions, ``AgentTurnInput.context``)
        is forwarded for this single turn so prompts and agents that
        condition on it can see it.  User/assistant items in the caller's
        context are filtered out by ``normalize_context_messages`` to
        avoid duplicating our own history.
        """
        context_msgs = normalize_context_messages(context, own_history=True)
        if not context_msgs:
            return list(self._message_history)
        converted = [_context_to_message(item) for item in context_msgs]
        return [*converted, *self._message_history]

    def _append_to_history(self, user_text: str, assistant_text: str) -> None:
        """Extend message history after a successful turn.

        Uses LangChain's typed message classes when available, falling
        back to plain dicts otherwise.  The fallback path lets this
        bridge function against duck-typed test doubles that don't
        depend on ``langchain_core``.
        """
        try:
            from langchain_core.messages import AIMessage, HumanMessage

            self._message_history.append(HumanMessage(content=user_text))
            if assistant_text:
                self._message_history.append(AIMessage(content=assistant_text))
        except ImportError:
            self._message_history.append({"role": "user", "content": user_text})
            if assistant_text:
                self._message_history.append({"role": "assistant", "content": assistant_text})

    def _mirror_partial_turn_to_store(self, user_text: str, assistant_text: str) -> None:
        """Persist a cancel-interrupted turn into a wrapped history store.

        A *completed* turn is written to a ``RunnableWithMessageHistory``
        store by the wrapper's own end-of-run save listener; a mid-stream
        cancel (timeout / barge-in ``aclose()``) aborts before that
        listener fires, so the partial user/assistant turn never reaches
        the backing store.  Without mirroring it here the next turn
        reloads stale history and a follow-up ``apply_interruption()``
        (which mirrors its rewrite into the same store) truncates the
        *previous* turn's assistant message — or no-ops on turn one —
        corrupting/losing conversation history on barge-in.  Plain
        runnables expose no store (``_history_store`` → ``None``) and are
        unaffected.  Best-effort: any store failure is swallowed.  Uses
        typed messages when ``langchain_core`` is importable, falling
        back to plain dicts (matching :meth:`_append_to_history`) for
        duck-typed stores.
        """
        store = self._history_store()
        if store is None:
            return
        try:
            try:
                from langchain_core.messages import AIMessage, HumanMessage

                user_msg: Any = HumanMessage(content=user_text)
                assistant_msg: Any = AIMessage(content=assistant_text)
            except ImportError:
                user_msg = {"role": "user", "content": user_text}
                assistant_msg = {"role": "assistant", "content": assistant_text}
            store.add_message(user_msg)
            if assistant_text:
                store.add_message(assistant_msg)
        except Exception:
            logger.debug(
                "Failed to mirror partial turn into wrapped history store",
                exc_info=True,
            )

    def _rewrite_last_ai_content(self, replacement: str) -> None:
        _rewrite_last_ai_in(self._message_history, replacement)
        # ``RunnableWithMessageHistory`` ignores the shadow list above —
        # it reloads the prompt's history from its own per-session store
        # and overwrites the bridge's ``history`` key.  Markdown cleanup
        # and interruption truncation both flow through here, so mirror
        # the rewrite into that store or later turns keep conditioning on
        # the raw / un-truncated assistant text.
        store = self._history_store()
        if store is None:
            return
        try:
            msgs = getattr(store, "messages", None)
            if isinstance(msgs, list):
                _rewrite_last_ai_in(msgs, replacement)
        except Exception:
            logger.debug("Failed to mirror rewrite into wrapped history store", exc_info=True)

    def _history_store(self) -> Any | None:
        """Best-effort underlying chat-message store for a
        ``RunnableWithMessageHistory``-wrapped runnable, else ``None``.

        Such a runnable rebuilds the prompt history from this
        per-session store every turn (it overwrites the bridge's
        ``history`` key), so post-hoc shadow-list edits are invisible to
        the next turn unless mirrored here.  Plain runnables expose no
        ``get_session_history`` and take the shadow-list-only path.
        Entirely defensive: any failure (no wrapper, store backend
        error) yields ``None`` so plain-runnable behaviour is never
        regressed.

        ``RunnableWithMessageHistory`` resolves the store by its
        ``history_factory_config`` ids pulled from the turn's
        ``configurable`` (``langchain_core.runnables.history``): a
        *single* spec is passed positionally as
        ``get_session_history(configurable[id])``, multiple specs as
        keyword args ``get_session_history(**{id: configurable[id]})``.
        We mirror that exact convention so post-hoc edits / ``reset()``
        hit the *same* store LangChain writes to.  Probing with the
        synthesized ``session_id`` instead (the prior approach) silently
        resolved a *different* store whenever a custom single key such
        as ``conversation_id`` was configured — ``factory(session_id)``
        succeeds for any one-arg factory, so the keyed store kept the
        raw/untruncated assistant message.
        """
        factory = getattr(self._runnable, "get_session_history", None)
        if not callable(factory):
            return None
        configurable = self._resolved_configurable or {}
        specs = getattr(self._runnable, "history_factory_config", None)
        if specs:
            try:
                spec_ids = [getattr(s, "id", None) for s in specs]
            except TypeError:
                spec_ids = []
            if spec_ids and all(isinstance(k, str) for k in spec_ids):
                values = [configurable.get(k) for k in spec_ids]
                if all(v is not None for v in values):
                    try:
                        if len(spec_ids) == 1:
                            return factory(values[0])
                        return factory(**dict(zip(spec_ids, values)))
                    except Exception:
                        logger.debug(
                            "Failed to resolve wrapped history store from history_factory_config",
                            exc_info=True,
                        )
                        return None
            # ``history_factory_config`` present but unusable (no ids,
            # non-str id, or a value missing from ``configurable``): fall
            # through to the single-arg ``session_id`` probe below.
        sid = self._resolved_session_id
        if sid is None:
            return None
        try:
            return factory(sid)
        except TypeError:
            # No ``history_factory_config`` and the factory rejects a
            # bare positional arg — nothing reliable to reconstruct.
            return None
        except Exception:
            logger.debug("Failed to resolve wrapped history store", exc_info=True)
            return None

    def _serialize_framework_state(self) -> bytes:
        try:
            payload = [
                {"role": _role_of(m), "content": _content_of(m)} for m in self._message_history
            ]
            return json.dumps(payload, default=str).encode()
        except (TypeError, ValueError):
            return b"[]"

    def _plan_interruption(self, delivered_text: str, mode: CancellationMode) -> InterruptionPlan:
        replacement = delivered_text + "..." if delivered_text else ""
        pre_ref = f"langchain-pre-{id(self._message_history):x}"
        post_ref = f"langchain-post-{id(self._message_history):x}"
        return InterruptionPlan(
            mutation_kind="interrupt_truncate",
            pre_state_ref=pre_ref,
            post_state_ref=post_ref,
            framework_instructions={
                "replacement": replacement,
                "delivered_text": delivered_text,
                "mode": mode.value,
            },
        )

    def _apply_planned_mutation(self, plan: InterruptionPlan) -> None:
        replacement = plan.framework_instructions["replacement"]
        self._rewrite_last_ai_content(replacement)

    def _handle_cursor_lifecycle(
        self,
        event: dict[str, Any],
        recorder: AgentRecorder,
        agent_cursor: ExecutionCursor,
        open_cursors: dict[str, ExecutionCursor],
        ended_runs: set[str],
    ) -> None:
        """Open / close ``model_node`` cursors from chat-model events.

        Tool calls are recorded as ``tool_phase_changed`` records by the
        translator; they don't open a cursor.  ``_end`` events that arrive
        out of LIFO order (e.g. ``RunnableParallel`` running two chat
        models concurrently) are deferred via ``ended_runs`` and flushed
        through ``_close_top_ended_cursors`` so the recorder's strict
        stack invariant holds.
        """
        event_type = event.get("event")
        if event_type in ("on_chat_model_start", "on_llm_start"):
            run_id = str(event.get("run_id") or uuid4().hex[:8])
            if run_id in open_cursors:
                return
            cursor = ExecutionCursor(
                unit_id=f"model-{run_id}",
                unit_kind=UnitKind.MODEL_NODE,
                display_name=str(event.get("name") or "model"),
                parent_unit_id=agent_cursor.unit_id,
                entered_at=time.monotonic_ns(),
                committable=False,
            )
            recorder.record_unit_entered(cursor)
            open_cursors[run_id] = cursor
        elif event_type in ("on_chat_model_end", "on_llm_end"):
            run_id = str(event.get("run_id") or "")
            if run_id and run_id in open_cursors:
                ended_runs.add(run_id)
                _close_top_ended_cursors(recorder, open_cursors, ended_runs)


# ── Helpers ──────────────────────────────────────────────────────


def _close_top_ended_cursors(
    recorder: AgentRecorder,
    open_cursors: dict[str, ExecutionCursor],
    ended_runs: set[str],
) -> None:
    """Pop cursors from the top of the stack while they're marked ended.

    LangChain emits start/end events in chronological order, so for
    parallel branches (``RunnableParallel``, parallel LangGraph nodes)
    an ``_end`` event can arrive while a sibling cursor is still the
    top of ``JournalAgentRecorder``'s stack.  We hold each non-top close
    in ``ended_runs`` and flush them in LIFO order once the obstructing
    siblings above also end, so the recorder's strict stack invariant
    is preserved without dropping the cursor.
    """
    while open_cursors:
        last_run_id = next(reversed(open_cursors))
        if last_run_id not in ended_runs:
            break
        cursor = open_cursors.pop(last_run_id)
        ended_runs.discard(last_run_id)
        recorder.record_unit_exited(cursor.with_committable(True), reason=None)


def _rewrite_last_ai_in(messages: list[Any], replacement: str) -> None:
    """Rewrite the last assistant message in ``messages`` in place.

    Shared by the bridge's shadow history and a wrapped
    ``RunnableWithMessageHistory`` store so both stay consistent after
    markdown cleanup / interruption truncation.  List-content messages
    have their text parts re-split; plain/empty content is overwritten.
    """
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if _role_of(msg) != "assistant":
            continue
        content = _content_of(msg)
        if isinstance(content, list):
            text_parts = [p for p in content if isinstance(p, dict) and p.get("type") == "text"]
            if text_parts:
                originals = [str(p.get("text", "")) for p in text_parts]
                splits = split_replacement_by_original_parts(originals, replacement)
                for part, repl in zip(text_parts, splits):
                    part["text"] = repl
                return
        # Plain string or empty content — overwrite.
        _set_content(msg, replacement)
        return


def _role_of(msg: Any) -> str:
    """Best-effort role extraction for both dict and typed messages."""
    if isinstance(msg, dict):
        return str(msg.get("role") or msg.get("type") or "")
    msg_type = getattr(msg, "type", None)
    if msg_type == "ai":
        return "assistant"
    if msg_type == "human":
        return "user"
    if msg_type == "system":
        return "system"
    if msg_type == "tool":
        return "tool"
    return getattr(msg, "role", "") or ""


def _content_of(msg: Any) -> Any:
    if isinstance(msg, dict):
        return msg.get("content", "")
    return getattr(msg, "content", "")


def _set_content(msg: Any, value: Any) -> None:
    if isinstance(msg, dict):
        msg["content"] = value
        return
    try:
        msg.content = value
    except (AttributeError, TypeError):
        object.__setattr__(msg, "content", value)


def _context_to_message(item: dict[str, str]) -> Any:
    """Convert a normalized ``{"role", "content"}`` dict to a LangChain message.

    Falls back to the dict itself when ``langchain_core`` is not
    importable — LangChain prompt templates accept both shapes for the
    placeholder-history pattern, and tests run without ``langchain_core``
    installed.
    """
    role = item.get("role", "system")
    content = item.get("content", "")
    try:
        from langchain_core.messages import (
            AIMessage,
            HumanMessage,
            SystemMessage,
            ToolMessage,
        )
    except ImportError:
        return {"role": role, "content": content}
    if role == "system" or role == "developer":
        return SystemMessage(content=content)
    if role == "user" or role == "human":
        return HumanMessage(content=content)
    if role == "assistant" or role == "ai":
        return AIMessage(content=content)
    if role == "tool":
        return ToolMessage(content=content, tool_call_id="")
    return SystemMessage(content=content)
