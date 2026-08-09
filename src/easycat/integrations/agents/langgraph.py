"""LangGraph bridge — wraps a ``CompiledStateGraph`` with checkpointer support.

A compiled LangGraph graph is itself a LangChain ``Runnable``, so the
bridge drives it via ``graph.astream_events(input, version="v2")``
exactly the way :class:`LangChainBridge` does.  The per-event
``metadata`` dict carries ``langgraph_node``, ``langgraph_step``,
``thread_id`` and ``checkpoint_id`` fields that we hoist into
``workflow_node`` cursors and ``state_snapshot`` records.

Two LangGraph-specific signals are not visible through plain
``astream_events`` events: ``get_stream_writer`` writes (consumed via
``stream_mode="custom"``) and ``interrupt()`` payloads (consumed via
``stream_mode="updates"`` as ``__interrupt__``).  Passing
``stream_mode=["custom", "updates"]`` to ``astream_events`` causes
LangGraph to fold both channels into top-level ``on_chain_stream``
events as ``(mode_name, payload)`` chunks, so the bridge can surface
``get_stream_writer`` writes via :func:`_custom_event_text` and fail
loudly when a graph uses ``interrupt()`` (voice runtimes have no path
to resume a paused graph — the human-in-the-loop *is* the caller).

Interruption patches the last AI message via LangGraph's native
``update_state``.  Because LangGraph's ``add_messages`` reducer dedupes
by message ``id``, we re-send the edited message under the same id so
it replaces instead of appending. State access is async-first; synchronous
savers run in worker threads, while the bridge's synchronous history hooks
stage their writes for an async flush before the next turn or bridge close.
"""

from __future__ import annotations

import asyncio
import copy
import functools
import inspect
import logging
import time
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar
from uuid import uuid4

from easycat.cancel import CancelToken
from easycat.integrations.agents._context import normalize_context_messages
from easycat.integrations.agents._helpers import (
    aclose_quietly,
    split_replacement_by_original_parts,
)
from easycat.integrations.agents._langchain_events import (
    _custom_event_text,
    cancellation_drain_state,
    close_top_ended_cursors,
    route_tool_cancellation_events,
    translate_stream_event,
)
from easycat.integrations.agents._state_serialization import serialize_framework_state
from easycat.integrations.agents.base import (
    NULL_RECORDER,
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
    apply_standard_interruption,
)
from easycat.runtime.records import ErrorInfo

# ``stream_mode`` values whose payloads LangGraph folds into top-level
# ``on_chain_stream`` events as ``(mode_name, payload)`` chunks when
# ``stream_mode`` is passed to ``astream_events``.  Used to detect graph-
# meta chunks so the translator's generic text-extraction path doesn't
# narrate them as plain text.
_GRAPH_STREAM_MODES: frozenset[str] = frozenset(
    {"values", "updates", "messages", "custom", "debug"}
)
# stream_mode channels we ask LangGraph to surface: ``custom`` carries
# ``get_stream_writer`` writes and ``updates`` carries ``__interrupt__``
# markers for human-in-the-loop graphs.
_DEFAULT_STREAM_MODES: tuple[str, ...] = ("custom", "updates")

logger = logging.getLogger(__name__)


def _stream_event_object(event: Any) -> dict[str, Any] | None:
    """Return a stream event object, dropping malformed provider items."""
    if not isinstance(event, dict):
        logger.warning("Ignoring malformed LangGraph stream event: expected an object")
        return None
    parent_ids = event.get("parent_ids")
    if parent_ids is not None and (
        not isinstance(parent_ids, Sequence) or isinstance(parent_ids, (str, bytes, bytearray))
    ):
        logger.warning("Ignoring malformed LangGraph stream event: parent_ids must be an array")
        return None
    metadata = event.get("metadata")
    checkpoint_ns = metadata.get("langgraph_checkpoint_ns") if isinstance(metadata, dict) else None
    if checkpoint_ns is not None and not isinstance(checkpoint_ns, str):
        logger.warning(
            "Ignoring malformed LangGraph stream event: langgraph_checkpoint_ns must be a string"
        )
        return None
    return event


# No default ``include_types`` filter.  LangChain's ``astream_events``
# filter keys ``on_custom_event`` on the *custom event's name* (not a
# runnable ``run_type``), so any non-``None`` ``include_types`` silently
# drops every ``dispatch_custom_event`` / ``adispatch_custom_event``
# payload — breaking the speakable custom-event TTS path a graph node
# can use for progress/status narration (see :func:`_custom_event_text`).
# An unfiltered stream is a strict superset that still surfaces the
# events the bridge depends on: ``on_chain_start`` / ``on_chain_end``
# (workflow_node cursors), ``on_chat_model_*`` / ``on_tool_*`` (token +
# tool visibility) and ``on_llm_*`` (without these a graph answering
# from a non-chat ``BaseLLM`` completes with an empty ``done.text`` and
# the voice goes silent).  ``translate_stream_event`` dispatches on the
# event *type* string, so the extra unfiltered events are harmless
# no-ops.  ``LangChainBridge`` makes the same tradeoff; callers that
# need to narrow the surface for performance on a very chatty graph can
# still opt in via ``include_types=``.
_DEFAULT_INCLUDE_TYPES: tuple[str, ...] | None = None


@dataclass
class _LangGraphTurnAccumulator:
    """Mutable per-turn streaming state shared across the invoke split.

    :meth:`LangGraphBridge._drive_stream` writes the streamed text and
    the "model tokens streamed" flag onto this holder so
    :meth:`LangGraphBridge._finalize_done` can read them after the
    stream generator returns (a generator's locals don't survive its
    return, so the state has to live on a shared object instead).
    """

    accumulated: str = ""
    # Whether the *model/chain* path (``translate_stream_event``)
    # streamed any text, tracked separately from ``get_stream_writer``
    # custom-chunk text.  A graph can speak a progress chunk via
    # ``get_stream_writer({"text": ...})`` and then write its real answer
    # as a final ``AIMessage`` without streaming model tokens;
    # ``accumulated`` would be non-empty (the progress text) so the
    # final-message fallback would be skipped and the caller would only
    # hear the progress narration.
    model_text_streamed: bool = False


@dataclass(frozen=True)
class _PendingStateMutation:
    kind: str
    payload: Any
    config: dict[str, Any]
    interruption: _PendingInterruptionJournal | None = None


@dataclass(frozen=True)
class _PendingInterruptionJournal:
    plan: InterruptionPlan
    mode: CancellationMode
    recorder: AgentRecorder
    actual_pre_ref: str
    caused_by_signal_id: str | None


class LangGraphBridge:
    """Wraps a LangGraph ``CompiledStateGraph``.

    Parameters
    ----------
    graph:
        A LangGraph compiled graph (``langgraph.graph.state.
        CompiledStateGraph``).  The graph **must** be compiled with a
        checkpointer — without one, ``update_state`` / ``get_state`` are
        unavailable and interruption patching cannot work.
    thread_id:
        Optional existing thread id to resume an earlier conversation.
        Defaults to a fresh UUID.
    messages_key:
        Key under which to inject the user's utterance into the graph's
        initial input dict.  Defaults to ``"messages"``.  Set to
        ``None`` to pass the text as a bare string input instead.
    display_name:
        Optional label for the outer ``agent`` cursor (defaults to
        ``type(graph).__name__``).
    include_types:
        Optional ``astream_events(include_types=...)`` filter.  Defaults
        to ``None`` (surface every event) — narrowing the filter drops
        ``on_custom_event`` from ``dispatch_custom_event`` (LangChain
        keys it on the event name, not a runnable type), silently
        disabling the custom-event TTS path, and would also have to keep
        ``chain`` / ``llm`` or workflow_node cursors and non-chat
        ``BaseLLM`` nodes are lost.  Pass an explicit tuple only when
        performance demands it for a very chatty graph.
    """

    COMMITTABLE_BOUNDARIES: ClassVar[Mapping[UnitKind | str, CommitRule]] = {
        UnitKind.AGENT: CommitRule.BETWEEN_TURNS,
        UnitKind.WORKFLOW_NODE: CommitRule.BETWEEN_NODES,
        UnitKind.MODEL_NODE: CommitRule.NON_COMMITTABLE,
        UnitKind.TOOL_CALL: CommitRule.BETWEEN_PHASES,
    }

    def __init__(
        self,
        graph: Any,
        *,
        thread_id: str | None = None,
        messages_key: str | None = "messages",
        display_name: str | None = None,
        include_types: Sequence[str] | None = _DEFAULT_INCLUDE_TYPES,
    ) -> None:
        if graph is None:
            raise BridgeInputError("LangGraphBridge requires a non-None graph=")
        if not hasattr(graph, "astream_events"):
            raise BridgeInputError(
                "LangGraphBridge requires a compiled LangGraph graph with "
                "astream_events() — got: " + type(graph).__name__
            )
        checkpointer = getattr(graph, "checkpointer", None)
        # ``graph.compile(checkpointer=False)`` explicitly disables
        # persistence (used for subgraphs): LangGraph sets
        # ``graph.checkpointer`` to ``False``, not ``None``, but
        # ``get_state()`` / ``update_state()`` still raise "No
        # checkpointer set".  Treat ``False`` (and any other falsy
        # value) the same as a missing checkpointer so the bridge fails
        # loudly at construction instead of later producing empty
        # ``done`` output and silently dropping interruption rewrites.
        #
        # ``graph.compile(checkpointer=True)`` is the *inherit-from-parent*
        # sentinel: LangGraph stores the literal ``True`` (not a real
        # checkpointer) so a subgraph reuses its parent's persistence.
        # As a root graph it has no parent, so ``invoke()`` raises
        # ``RuntimeError: checkpointer=True cannot be used for root
        # graphs``.  ``not True`` is ``False``, so reject ``True``
        # explicitly here with the same actionable error rather than
        # letting the first turn blow up.
        if not checkpointer or checkpointer is True:
            raise BridgeInputError(
                "LangGraphBridge requires a graph compiled with a checkpointer. "
                "Call graph.compile(checkpointer=InMemorySaver()) (or another "
                "checkpointer) before passing it to LangGraphBridge."
            )
        try:
            from langgraph.checkpoint.base import BaseCheckpointSaver

            if isinstance(checkpointer, BaseCheckpointSaver):
                saver_type = type(checkpointer)
                has_sync = saver_type.get_tuple is not BaseCheckpointSaver.get_tuple
                has_async = saver_type.aget_tuple is not BaseCheckpointSaver.aget_tuple
                if not has_sync and not has_async:
                    raise BridgeInputError(
                        f"{saver_type.__name__} implements neither get_tuple() nor "
                        "aget_tuple(); use a functional sync or async LangGraph checkpointer."
                    )
        except ImportError:
            # Duck-typed tests and optional-SDK import surfaces are validated
            # by the graph state-method probes below.
            pass
        self._graph = graph
        # An explicit ``thread_id=`` wins; otherwise fall back to a
        # thread id the caller bound onto the graph via
        # ``graph.with_config(configurable={"thread_id": ...})`` (a
        # common resume pattern that ``auto_adapt_agent`` cannot
        # otherwise carry through) before minting a fresh UUID.
        bound_thread_id = _bound_thread_id(graph)
        self._thread_id = thread_id or bound_thread_id or str(uuid.uuid4())
        self._messages_key = messages_key
        self._display_name = display_name or type(graph).__name__
        self._include_types = list(include_types) if include_types is not None else None
        self._last_output: Any = None
        # Final checkpoint id captured at the end of the previous turn.
        # Used as the pre-turn baseline for the post-turn checkpoint
        # trail walk (see :meth:`_record_checkpoint_trail`) so prior
        # turns aren't re-recorded — without an extra ``get_state``
        # round-trip to the checkpointer at turn start.
        self._last_checkpoint_id: str | None = None
        self._last_state_snapshot: Any = None
        self._pending_state_mutations: list[_PendingStateMutation] = []
        # Real compiled graphs expose async state methods even when their
        # saver is synchronous. Use that surface to keep sync protocol hooks
        # I/O-free; duck-typed graphs without it retain their immediate
        # in-memory behaviour for compatibility.
        self._async_state_surface = callable(getattr(graph, "aget_state", None)) and callable(
            getattr(graph, "aupdate_state", None)
        )
        # A caller can bind ``configurable.checkpoint_id`` onto the graph
        # via ``graph.with_config(...)`` (a LangGraph resume / time-travel
        # config: "run from this checkpoint").  It is a *one-shot* resume
        # cursor: honoured for the construction-time baseline seed and the
        # first turn's stream, then dropped.  Carrying it into every later
        # ``invoke()`` / ``get_state()`` would make LangGraph keep forking
        # from that original snapshot and read stale state, losing all
        # conversation progress after the first resumed turn.  Cleared in
        # :meth:`invoke` once consumed (and in :meth:`reset`).
        self._resume_checkpoint_id: str | None = _bound_checkpoint_id(graph)
        # Resuming an existing thread: the checkpointer may already hold
        # an arbitrarily long history.  Seed the trail baseline from the
        # thread's current checkpoint once before the first turn
        # (construction-time only for sync-only duck-typed graphs; real
        # compiled graphs seed asynchronously at invoke start)
        # so the first turn's ``_record_checkpoint_trail`` walk stops at
        # the already-persisted history instead of re-recording every
        # prior checkpoint as if this turn created it (O(total history)
        # work + duplicate snapshots on a persistent checkpointer).
        # Best-effort: a transient/missing-thread checkpointer error just
        # leaves the baseline ``None`` (degrades to the pre-fix
        # behaviour, not a hard failure).  Skipped only for a genuinely
        # fresh thread (no explicit *and* no graph-bound ``thread_id``) —
        # there is no prior history, so ``None`` is already correct and
        # the round-trip would be wasted.  A graph-bound thread id is a
        # resume just like an explicit one, so seed its baseline too.
        self._checkpoint_baseline_needs_seed = thread_id is not None or bound_thread_id is not None
        if self._checkpoint_baseline_needs_seed and not self._async_state_surface:
            try:
                # Seed against the resume cursor (if any) so a time-travel
                # resume baselines at the pinned checkpoint — otherwise the
                # first turn's trail walk re-records the forked-from history.
                existing_state = self._graph.get_state(self._resume_config())
                self._last_checkpoint_id = _get_checkpoint_id(existing_state)
                self._last_state_snapshot = existing_state
                self._checkpoint_baseline_needs_seed = False
            except Exception:
                logger.debug(
                    "Failed to seed checkpoint baseline for resumed thread",
                    exc_info=True,
                )
        # Message ids of the transient per-turn context (caller-id /
        # system-prefix) injected for the *current* turn.  Tracked so it
        # can be deleted from checkpointed graph state once the turn
        # ends — otherwise ``add_messages`` persists a fresh system
        # message every turn and stale/duplicated context leaks forward.
        self._transient_context_ids: list[str] = []
        # Set when the most recent turn ended having produced no
        # assistant output at all (cancelled before its first token).
        # The cancelled node never wrote an ``AIMessage`` and
        # ``_commit_partial_assistant`` skips an empty commit, so the
        # checkpoint holds no current-turn AI message — a follow-up
        # ``apply_interruption`` rewrite must *not* walk back and
        # truncate the *previous* turn's already-delivered reply.  Reset
        # at each turn start; left ``False`` for a direct
        # ``apply_interruption`` with no preceding turn so the
        # standalone-call behaviour is preserved.
        self._turn_produced_no_assistant = False

    # ── ExternalAgentBridge interface ─────────────────────────────

    def _config(self) -> dict[str, Any]:
        # Start from whatever the caller bound onto the graph via
        # ``graph.with_config(...)`` — tags, recursion_limit, and *every*
        # ``configurable`` key (tenant ids, auth tokens, feature flags
        # consumed by nodes), not just the thread id.  ``astream_events``
        # uses this supplied config *instead of* the graph's bound one,
        # so rebuilding it with only ``thread_id`` would silently drop
        # those values and make such graphs fail or run with defaults.
        # Override only the thread id (already resolved with the correct
        # explicit > bound > fresh-UUID precedence in ``__init__``) and
        # neutralise any bound ``checkpoint_id``.
        #
        # A bound ``configurable.checkpoint_id`` is LangGraph's one-shot
        # resume/time-travel cursor ("run from this checkpoint").  This is
        # the *current-state* config used by every post-turn
        # ``get_state`` / ``update_state`` / ``get_state_history`` read,
        # which must always target the thread's *latest* checkpoint — pin
        # it and later turns keep forking the original snapshot and
        # ``get_state`` reads stale state, losing all conversation
        # progress after the first resumed turn.  The resume cursor is
        # applied (one-shot) only by :meth:`_resume_config` for the
        # construction baseline seed and the first turn's stream.
        #
        # Set ``checkpoint_id`` to ``None`` rather than dropping the key:
        # when the graph is a ``RunnableBinding`` wrapper its attribute
        # proxy re-merges the wrapper's bound config (re-injecting the
        # pinned id) for an *omitted* key, but an explicit ``None`` wins
        # the merge and LangGraph treats it as "latest checkpoint".
        config = _bound_config(self._graph)
        configurable = dict(config.get("configurable") or {})
        configurable["thread_id"] = self._thread_id
        configurable["checkpoint_id"] = None
        config["configurable"] = configurable
        return config

    def _resume_config(self) -> dict[str, Any]:
        """:meth:`_config` plus the one-shot bound resume ``checkpoint_id``.

        Used only for the construction-time baseline seed and the first
        turn's ``astream_events`` input, so a resume/time-travel run forks
        from the caller-pinned checkpoint.  Once :meth:`invoke` consumes
        the cursor (clears ``self._resume_checkpoint_id``) this is
        identical to :meth:`_config` (latest checkpoint).
        """
        config = self._config()
        if self._resume_checkpoint_id is not None:
            configurable = dict(config.get("configurable") or {})
            configurable["checkpoint_id"] = self._resume_checkpoint_id
            config["configurable"] = configurable
        return config

    def _messages_key_uses_add_messages(self) -> bool:
        """Whether the messages channel uses LangGraph's ``add_messages``.

        The transient-context purge, the partial-turn commit and the
        interruption rewrites all rely on ``add_messages`` *merge*
        semantics: a ``RemoveMessage`` marker deletes the message with
        that id and an id-keyed re-send replaces it in place.  Those
        semantics are unique to ``add_messages``:

        * A plain ``LastValue`` channel (``messages: list`` with no
          ``Annotated[...]``) makes ``update_state`` *replace* the whole
          list, so issuing those markers there wipes the conversation.
        * A *generic* reducer (``Annotated[list, operator.add]`` or any
          custom accumulator) only ever *appends* what it is given, so a
          ``RemoveMessage`` marker / id-keyed re-send is appended as a
          fresh list tail — polluting checkpointed history and possibly
          leaving the marker itself as the final message (an empty
          ``done.text``).

        Only a positively identified ``add_messages`` channel may use
        the machinery.  ``Annotated[list, add_messages]`` compiles to a
        ``BinaryOperatorAggregate`` whose ``.operator`` *is* LangGraph's
        ``add_messages`` reducer.  When the channel can't be introspected
        (duck-typed test graphs, older/newer LangGraph internals) assume
        ``add_messages`` so existing behaviour is preserved — only a
        positively identified non-``add_messages`` channel disables it.
        """
        key = self._messages_key or "messages"
        channels = getattr(self._graph, "channels", None)
        if not isinstance(channels, dict) or key not in channels:
            return True
        channel = channels[key]
        if type(channel).__name__ == "LastValue":
            return False
        operator = getattr(channel, "operator", None)
        if operator is None:
            # No reducer operator and not a recognised ``LastValue`` —
            # an opaque/duck-typed channel.  Assume ``add_messages`` so
            # existing behaviour is preserved (only a positively
            # identified non-``add_messages`` channel disables it).
            return True
        return _is_add_messages_reducer(operator)

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        await self._seed_checkpoint_baseline(recorder)
        await self._flush_pending_state_mutations(recorder)

        agent_cursor = ExecutionCursor(
            unit_id=f"agent-{uuid4().hex[:8]}",
            unit_kind=UnitKind.AGENT,
            display_name=self._display_name,
            entered_at=time.monotonic_ns(),
            committable=False,
        )
        recorder.record_unit_entered(agent_cursor)

        # Drop the previous turn's captured tail.  ``_last_output`` is only
        # re-set below when ``get_state()`` succeeds; clearing it here means
        # a transient/custom checkpointer failure on this turn can't make
        # ``done.text``/``structured_output`` (or the fallback that speaks
        # the final ``AIMessage`` when nothing streamed) replay the prior
        # turn's response — the fallback degrades to this turn's streamed
        # text instead.
        self._last_output = None
        # ``snapshot_state`` / interruption serialization use this cache so
        # synchronous protocol hooks never block on a remote checkpointer.
        # Clear it with the output: a failed final-state read must not expose
        # the previous turn's checkpoint as if it belonged to this turn.
        self._last_state_snapshot = None
        # Cleared each turn; re-armed only if this turn ends before it
        # produces any assistant output (see the cancel paths below).
        self._turn_produced_no_assistant = False

        acc = _LangGraphTurnAccumulator()
        # Cursors open inside this turn, keyed by LangChain ``run_id``.
        # Each node entry opens a workflow_node cursor; each chat_model
        # call opens a model_node cursor.  Closing is driven by the
        # matching ``_end`` event for the same run_id.
        open_cursors: dict[str, ExecutionCursor] = {}
        # Previously seen ``(node, langgraph_step)`` at each subgraph
        # namespace, so we can emit handoff triples when a node changes
        # at the same level *across* super-steps.  The step is tracked
        # alongside the node so a fan-out's sibling nodes — which share a
        # parent namespace within one super-step but have no edge between
        # them — don't get an invented prev→sibling handoff.
        last_node_by_ns: dict[tuple[str, ...], tuple[str, Any]] = {}
        # Checkpoint ids we've already emitted state_snapshot records
        # for, so the post-turn history walk records each id once.
        seen_checkpoints: set[str] = set()

        config = self._config()
        # Pre-turn baseline for the post-turn checkpoint trail: the
        # checkpoint that already existed when this turn started (this
        # bridge's prior-turn final, or ``None`` on the first turn).
        # Read from instance state rather than a ``get_state`` probe so
        # the turn doesn't pay an extra checkpointer round-trip.
        baseline_checkpoint_id = self._last_checkpoint_id

        drive = self._drive_stream(
            turn_input,
            recorder,
            agent_cursor,
            cancel_token,
            acc,
            open_cursors,
            last_node_by_ns,
        )
        try:
            async for bridge_event in drive:
                yield bridge_event
        finally:
            # An early consumer ``aclose()`` (barge-in) injects
            # ``GeneratorExit`` at the ``yield`` above; ``async for`` does not
            # forward it into ``_drive_stream``, so its ``BaseException``
            # cleanup (which commits the partial turn) would only run on GC.
            # Close it explicitly so the cleanup runs synchronously before a
            # follow-up ``apply_interruption()``.  A no-op once exhausted.
            await aclose_quietly(drive)

        for cursor in reversed(list(open_cursors.values())):
            recorder.record_unit_exited(cursor.with_committable(True), reason=None)
        open_cursors.clear()
        # Drop this turn's transient context from checkpointed state
        # before the checkpoint trail is read so the recorded post-turn
        # state (and every following turn) is free of the per-turn
        # system prefix.
        await self._purge_transient_context(recorder)

        # Record the real per-step checkpoint trail + capture the last
        # message for ``structured_output``.  Best-effort: a graph
        # compiled without a checkpointer would have been rejected at
        # construction, but ``get_state`` can still fail on transient
        # checkpointer errors.  Also belt-and-suspenders: if the graph
        # paused on ``interrupt()`` in a path that didn't surface
        # through the ``updates`` channel (custom checkpointers, older
        # LangGraph versions), inspect ``state.tasks[i].interrupts`` and
        # fail loudly so the voice doesn't go silently dead.
        try:
            final_state = await self._get_state(config)
            self._last_state_snapshot = final_state
            await self._record_checkpoint_trail(
                config, baseline_checkpoint_id, recorder, seen_checkpoints
            )
            # Advance the baseline so the *next* turn's trail starts
            # after this turn's last checkpoint.
            self._last_checkpoint_id = _get_checkpoint_id(final_state)
            self._last_output = _messages_tail(final_state, self._messages_key or "messages")
            pending = _pending_interrupts(final_state)
            if pending:
                self._raise_hitl_unsupported(pending, agent_cursor, open_cursors, recorder)
        except BridgeInputError:
            await self._purge_transient_context(recorder)
            raise
        except Exception as exc:  # pragma: no cover — best-effort.
            recorder.record_framework_error(ErrorInfo.from_exception(exc))
            logger.warning("Failed to fetch final LangGraph state", exc_info=True)

        finalize = self._finalize_done(acc, agent_cursor, recorder)
        try:
            async for bridge_event in finalize:
                yield bridge_event
        finally:
            await aclose_quietly(finalize)

    async def _drive_stream(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        agent_cursor: ExecutionCursor,
        cancel_token: CancelToken | None,
        acc: _LangGraphTurnAccumulator,
        open_cursors: dict[str, ExecutionCursor],
        last_node_by_ns: dict[tuple[str, ...], tuple[str, Any]],
    ) -> AsyncIterator[AgentBridgeEvent]:
        """Drive ``astream_events`` for one turn, yielding bridge events.

        Holds the streaming loop plus its ``except Exception`` /
        ``except BaseException`` cleanup.  Accumulated text and the
        "model tokens streamed" flag are written onto ``acc`` so the
        post-stream finalize step (:meth:`_finalize_done`) can read them
        after the generator returns; ``open_cursors`` is mutated in place
        for the same reason.  Behaviour is identical to the inline loop —
        the cancellation/commit ordering documented in the cancel-path
        comments below is load-bearing and unchanged.
        """
        # Run ids whose ``_end`` event arrived while a sibling cursor
        # was still on top of the recorder stack (parallel nodes / models
        # running concurrently) — closed in LIFO order once the
        # obstructing sibling(s) also end so the recorder's strict stack
        # invariant holds.
        ended_runs: set[str] = set()
        # Shared state for tool-call deduplication and LCEL root-chain
        # dedup — see :func:`translate_stream_event` for details.  Under
        # LangGraph the outermost ``on_chain_start`` is the graph (not an
        # LCEL root), so the translator instead treats each LangGraph
        # *node entry* as root-equivalent: a plain ``RunnableLambda`` /
        # LCEL node's own composed stream reaches the translator while
        # the node's deeper LCEL children stay deduped (so a node that is
        # ``RunnableLambda(f) | RunnableLambda(g)`` doesn't narrate its
        # intermediate value).  Model double-speak is still handled by
        # ``chains_with_chat_model_descendants``.
        tool_state: dict[str, Any] = {}
        cancelled = False

        async def commit_cancelled_turn() -> None:
            await self._commit_partial_assistant(acc.accumulated, recorder)
            if not acc.accumulated:
                self._turn_produced_no_assistant = True

        input_payload = self._build_input(turn_input.text, turn_input.context)
        # Honour a caller-pinned resume/time-travel checkpoint for *this*
        # turn's stream only, then consume the cursor: LangGraph treats
        # ``configurable.checkpoint_id`` as "run from this checkpoint", so
        # carrying it forward (or into this turn's post-stream
        # ``get_state``) would keep forking the original snapshot and read
        # stale state.  ``config`` (from ``_config()``, unpinned) is used
        # for every post-turn read so they see the freshly forked head.
        stream_config = self._resume_config()
        self._resume_checkpoint_id = None
        stream_kwargs: dict[str, Any] = {
            "version": "v2",
            "config": stream_config,
            "stream_mode": list(_DEFAULT_STREAM_MODES),
        }
        if self._include_types is not None:
            stream_kwargs["include_types"] = self._include_types

        stream: AsyncIterator[dict[str, Any]] | None = None
        try:
            stream = self._graph.astream_events(input_payload, **stream_kwargs)
            async for event in stream:
                requested = bool(cancel_token and cancel_token.is_cancelled)
                cancelled, stop_now = cancellation_drain_state(
                    requested=requested,
                    active=cancelled,
                    state=tool_state,
                    recorder=recorder,
                )
                if stop_now:
                    # A cancel token set mid-stream (barge-in) breaks out
                    # through the *normal* completion path below, not the
                    # ``BaseException`` cleanup — so the cancelled node's
                    # partial assistant text the caller already heard would
                    # never be written to the checkpoint.  Commit it now
                    # (before the post-loop transient-context purge and
                    # checkpoint-trail walk, mirroring the BaseException
                    # path) so a follow-up ``apply_interruption()``
                    # truncates *this* turn's AI message instead of
                    # rewriting the previous turn's and corrupting prior
                    # LangGraph conversation state.
                    await commit_cancelled_turn()
                    break

                # ``astream_events`` is a third-party streaming boundary;
                # graph-chunk extraction and lifecycle helpers expect objects.
                if (event := _stream_event_object(event)) is None:
                    continue

                bridge_events, stop_after_event = self._route_stream_event(
                    event=event,
                    recorder=recorder,
                    agent_cursor=agent_cursor,
                    open_cursors=open_cursors,
                    last_node_by_ns=last_node_by_ns,
                    ended_runs=ended_runs,
                    tool_state=tool_state,
                    acc=acc,
                    cancelled=cancelled,
                )
                for bridge_event in bridge_events:
                    yield bridge_event
                if stop_after_event:
                    await commit_cancelled_turn()
                    break
        except Exception as exc:
            for cursor in reversed(list(open_cursors.values())):
                recorder.safe_exit_cursor(cursor)
            recorder.record_framework_error(ErrorInfo.from_exception(exc))
            recorder.record_unit_exited(agent_cursor, reason="error")
            await self._purge_transient_context(recorder)
            raise
        except BaseException:
            # The default ``AgentRunner`` enforces its timeout by
            # cancelling the pending ``__anext__()`` (and then calling
            # ``aclose()``), injecting ``asyncio.CancelledError`` /
            # ``GeneratorExit`` here.  Neither is an ``Exception`` so the
            # block above is skipped and any open workflow/model/agent
            # cursors would be left unexited (breaking the recorder's
            # stack invariant) and this turn's transient context would
            # leak into the checkpointed graph state.  Clean both up
            # defensively before re-raising; no ``record_framework_error``
            # since a cancelled turn isn't a framework fault.
            for cursor in reversed(list(open_cursors.values())):
                recorder.safe_exit_cursor(cursor)
            recorder.safe_exit_cursor(agent_cursor)
            # The cancelled node never returned, so its partial assistant
            # output isn't in the checkpoint.  Commit it now (before the
            # purge) so a follow-up ``apply_interruption()`` truncates
            # *this* turn's AI message — without this the rewrite targets
            # the previous turn's last AI message and barge-in corrupts
            # prior conversation state.
            await self._commit_partial_assistant(acc.accumulated, recorder)
            if not acc.accumulated:
                # Cancelled before the first token: nothing committed and
                # the node never wrote an AIMessage, so a follow-up
                # interruption rewrite must no-op (the only AI message in
                # the checkpoint belongs to the *previous* turn).
                self._turn_produced_no_assistant = True
            await self._purge_transient_context(recorder)
            raise
        finally:
            # Like LangChain, a consumer's ``aclose()`` only reaches this
            # driver. Explicitly close the graph-owned iterator as well so
            # its callback tasks and transports do not outlive the cancelled
            # voice turn.
            await aclose_quietly(stream)

    def _route_stream_event(
        self,
        *,
        event: dict[str, Any],
        recorder: AgentRecorder,
        agent_cursor: ExecutionCursor,
        open_cursors: dict[str, ExecutionCursor],
        last_node_by_ns: dict[tuple[str, ...], tuple[str, Any]],
        ended_runs: set[str],
        tool_state: dict[str, Any],
        acc: _LangGraphTurnAccumulator,
        cancelled: bool,
    ) -> tuple[tuple[AgentBridgeEvent, ...], bool]:
        if graph_chunk := self._extract_graph_stream_chunk(event):
            if cancelled:
                return (), False
            mode_name, payload = graph_chunk
            bridge_events = tuple(self._handle_graph_stream_chunk(mode_name, payload, recorder))
            for bridge_event in bridge_events:
                if bridge_event.kind == "text_delta":
                    acc.accumulated += bridge_event.text
            return bridge_events, False

        self._handle_cursor_lifecycle(
            event,
            recorder,
            agent_cursor,
            open_cursors,
            last_node_by_ns,
            ended_runs,
        )
        bridge_events, stop_after_event = route_tool_cancellation_events(
            translate_stream_event(event, recorder, state=tool_state),
            tool_state,
            cancelled=cancelled,
        )
        for bridge_event in bridge_events:
            if bridge_event.kind == "text_delta":
                acc.accumulated += bridge_event.text
                acc.model_text_streamed = True
        return bridge_events, stop_after_event

    async def _finalize_done(
        self,
        acc: _LangGraphTurnAccumulator,
        agent_cursor: ExecutionCursor,
        recorder: AgentRecorder,
    ) -> AsyncIterator[AgentBridgeEvent]:
        """Decide the recorded ``done.text`` and emit the terminal event.

        Reads the streamed text / ``model_text_streamed`` flag off
        ``acc`` and ``self._last_output`` (set by the post-stream
        ``get_state``).  Three shapes:

        * Chat-model tokens streamed.  Usually those *are* the answer,
          but a node can stream a model call and then write a
          *transformed* ``AIMessage`` to state
          (``AIMessage(content=f"Final: {reply.content}")``).  When the
          graph's final AI message differs from the raw streamed text
          the streamed tokens were internal model output, so prefer the
          final message — otherwise ``done.text``/``structured_output``
          record the unmodified internal output instead of the graph's
          actual reply.  (Live TTS already spoke the raw tokens; that
          speculative-streaming gap is unavoidable without buffering
          every node's output, and re-emitting here would double-speak,
          so we only correct the recorded transcript.)
        * Nothing streamed but a node wrote a final ``AIMessage``
          (synchronous LLM, transformed output, plain
          ``RunnableLambda``).  Fall back to that message's text — but
          only when the tail is actually an AI message, else a graph
          that completes without appending an assistant reply (a
          conditional path returning ``{}``, an edge straight to END)
          would surface the user's own utterance and TTS would repeat
          the caller's voice back.
        * Otherwise speak whatever streamed (custom chunks, or nothing).
          Suppressed parented ``BaseLLM`` output is not used as a fallback:
          downstream chains/nodes may have intentionally redacted, selected,
          or dropped that raw text.
        """
        accumulated = acc.accumulated
        if acc.model_text_streamed:
            if _message_is_ai(self._last_output):
                final_text = _extract_message_text(self._last_output)
                spoken_text = final_text if final_text else accumulated
            else:
                spoken_text = accumulated
        elif _message_is_ai(self._last_output):
            final_text = _extract_message_text(self._last_output)
            if accumulated:
                # Custom progress chunks were already streamed/spoken but
                # the model answer never streamed — emit the final AI
                # text as a delta so the real answer is also spoken (the
                # consumer only falls back to ``done.text`` when *nothing*
                # streamed, so a non-empty ``done.text`` alone would be
                # silently dropped here).
                if final_text:
                    yield AgentBridgeEvent(kind="text_delta", text=final_text)
                spoken_text = accumulated + final_text
            else:
                spoken_text = final_text
        else:
            spoken_text = accumulated
        recorder.record_unit_exited(agent_cursor.with_committable(True), reason=None)
        yield AgentBridgeEvent(
            kind="done",
            text=spoken_text,
            structured_output=self._last_output,
        )

    def snapshot_state(self) -> FrameworkStateSnapshot:
        fields: dict[str, Any] = {
            "framework": "langgraph",
            "graph": self._display_name,
            "thread_id": self._thread_id,
        }
        try:
            state = self._last_state_snapshot
            if state is None and not self._async_state_surface:
                state = self._graph.get_state(self._config())
            if state is not None:
                fields["checkpoint_id"] = _get_checkpoint_id(state)
                next_nodes = getattr(state, "next", None)
                if next_nodes is not None:
                    fields["next_nodes"] = list(next_nodes)
                metadata = getattr(state, "metadata", None) or {}
                if isinstance(metadata, dict):
                    fields["step"] = metadata.get("step")
        except Exception:  # pragma: no cover — missing checkpointer or fresh graph.
            logger.debug("snapshot_state: failed to fetch graph state", exc_info=True)
        return FrameworkStateSnapshot(
            fields=fields,
            kind="langgraph",
        )

    def apply_interruption(
        self,
        delivered_text: str,
        mode: CancellationMode,
        recorder: AgentRecorder | None = None,
        caused_by_signal_id: str | None = None,
    ) -> None:
        if not self._async_state_surface:
            apply_standard_interruption(self, delivered_text, mode, recorder, caused_by_signal_id)
            return

        plan = self._plan_interruption(delivered_text, mode)
        if recorder is None:
            self._apply_planned_mutation(plan)
            return

        actual_pre_ref = recorder.record_state_snapshot(
            plan.pre_state_ref,
            payload=self._serialize_framework_state(),
        )
        try:
            recorder.record_state_committed(
                mutation_kind=plan.mutation_kind,
                pre_state_ref=actual_pre_ref,
                post_state_ref=plan.post_state_ref,
            )
        except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
            # Match the standard protocol: a degraded journal prevents the
            # framework mutation from being scheduled.
            return

        pending_before = len(self._pending_state_mutations)
        try:
            self._apply_planned_mutation(plan)
        except Exception as exc:
            recorder.record_interruption_apply_failed(
                mutation_kind=plan.mutation_kind,
                pre_state_ref=actual_pre_ref,
                post_state_ref=plan.post_state_ref,
                failure_error=ErrorInfo.from_exception(exc),
            )
            raise

        journal = _PendingInterruptionJournal(
            plan=plan,
            mode=mode,
            recorder=recorder,
            actual_pre_ref=actual_pre_ref,
            caused_by_signal_id=caused_by_signal_id,
        )
        if len(self._pending_state_mutations) == pending_before:
            # The rewrite was a legitimate no-op (for example, this turn has
            # no assistant message). No async persistence remains outstanding.
            self._record_interruption_success(
                journal,
                payload=self._serialize_framework_state(),
            )
            return
        mutation = self._pending_state_mutations[-1]
        self._pending_state_mutations[-1] = _PendingStateMutation(
            mutation.kind,
            mutation.payload,
            mutation.config,
            interruption=journal,
        )

    def reset(self) -> None:
        self._thread_id = str(uuid.uuid4())
        # New thread → the old thread's one-shot resume cursor must not
        # apply (it would pin the fresh thread to a foreign checkpoint).
        self._resume_checkpoint_id = None
        self._last_output = None
        self._last_state_snapshot = None
        # Pending async writes retain the old thread's captured config and must
        # survive reset. The next invoke or aclose flushes them to that original
        # thread before the bridge can be discarded.
        self._checkpoint_baseline_needs_seed = False
        # New thread → no prior checkpoint, so the next turn's trail
        # starts from scratch instead of stopping at a stale baseline.
        self._last_checkpoint_id = None
        # Drop any not-yet-purged ids: they belonged to the old thread,
        # and a later purge against the rotated thread_id would be wrong.
        self._transient_context_ids = []

    # ── History post-processing ───────────────────────────────────

    def replace_last_assistant_text(self, text: str) -> None:
        """Rewrite the last AI message in graph state to ``text``."""
        if self._async_state_surface:
            state = self._last_state_snapshot
            if state is None:
                self._pending_state_mutations.append(
                    _PendingStateMutation("rewrite", text, self._config())
                )
                return
            update = self._rewrite_update_for_state(state, text)
            if update is not None:
                self._pending_state_mutations.append(
                    _PendingStateMutation("state_update", update, self._config())
                )
        else:
            self._rewrite_last_ai_message(text)

    def append_interruption_note(self, note: str) -> None:
        """Append an interruption note to graph history so the next turn sees it."""
        # On a plain ``LastValue`` channel ``update_state`` would replace
        # the whole messages list with just this note, dropping the
        # conversation; only append when the channel accumulates.
        if not self._messages_key_uses_add_messages():
            return
        if self._async_state_surface:
            self._pending_state_mutations.append(
                _PendingStateMutation("append_note", note, self._config())
            )
            return
        try:
            from langchain_core.messages import SystemMessage

            new_msg = SystemMessage(content=note)
            updated = self._graph.update_state(
                self._config(), {self._messages_key or "messages": [new_msg]}
            )
            self._advance_checkpoint_baseline(updated)
        except ImportError:
            # Fallback — use a plain dict message; LangGraph accepts these
            # for the ``add_messages`` reducer too.
            try:
                updated = self._graph.update_state(
                    self._config(),
                    {self._messages_key or "messages": [{"role": "system", "content": note}]},
                )
                self._advance_checkpoint_baseline(updated)
            except Exception:
                logger.debug("Failed to append interruption note via update_state", exc_info=True)
        except Exception:
            logger.debug("Failed to append interruption note to LangGraph", exc_info=True)

    async def aclose(self) -> None:
        """Persist staged history mutations before bridge teardown."""
        await self._flush_pending_state_mutations(NULL_RECORDER)

    # ── Internal ─────────────────────────────────────────────────

    async def _get_state(self, config: dict[str, Any]) -> Any:
        async_get = getattr(self._graph, "aget_state", None)
        if callable(async_get):
            try:
                result = async_get(config)
                return await result if inspect.isawaitable(result) else result
            except NotImplementedError:
                pass
        return await asyncio.to_thread(self._graph.get_state, config)

    async def _update_state(self, config: dict[str, Any], values: dict[str, Any]) -> Any:
        async_update = getattr(self._graph, "aupdate_state", None)
        if callable(async_update):
            try:
                result = async_update(config, values)
                return await result if inspect.isawaitable(result) else result
            except NotImplementedError:
                pass
        return await asyncio.to_thread(self._graph.update_state, config, values)

    async def _checkpoint_ids_since(
        self, config: dict[str, Any], baseline_checkpoint_id: str | None
    ) -> list[str]:
        async_history = getattr(self._graph, "aget_state_history", None)
        if callable(async_history):
            turn_ids: list[str] = []
            try:
                result = async_history(config)
                if inspect.isawaitable(result):
                    result = await result
                async for snapshot in result:
                    checkpoint_id = _get_checkpoint_id(snapshot)
                    if not checkpoint_id or checkpoint_id == baseline_checkpoint_id:
                        break
                    turn_ids.append(checkpoint_id)
                return turn_ids
            except NotImplementedError:
                if turn_ids:
                    raise

        return await asyncio.to_thread(
            _sync_checkpoint_ids_since,
            self._graph,
            config,
            baseline_checkpoint_id,
        )

    async def _seed_checkpoint_baseline(self, recorder: AgentRecorder) -> None:
        if not self._checkpoint_baseline_needs_seed:
            return
        self._checkpoint_baseline_needs_seed = False
        try:
            existing_state = await self._get_state(self._resume_config())
        except Exception as exc:
            recorder.record_framework_error(ErrorInfo.from_exception(exc))
            logger.warning(
                "Failed to seed checkpoint baseline for resumed thread",
                exc_info=True,
            )
            return
        self._last_checkpoint_id = _get_checkpoint_id(existing_state)
        self._last_state_snapshot = existing_state

    async def _flush_pending_state_mutations(self, recorder: AgentRecorder) -> None:
        pending = self._pending_state_mutations
        if not pending:
            return
        self._pending_state_mutations = []

        for index, mutation in enumerate(pending):
            try:
                if mutation.kind == "rewrite":
                    state = await self._get_state(mutation.config)
                    if _config_thread_id(mutation.config) == self._thread_id:
                        self._last_state_snapshot = state
                    update = self._rewrite_update_for_state(state, str(mutation.payload))
                    if update is None:
                        if mutation.interruption is not None:
                            self._record_interruption_success(
                                mutation.interruption,
                                payload=_serialize_state_values(state),
                            )
                        continue
                elif mutation.kind == "append_note":
                    update = self._interruption_note_update(str(mutation.payload))
                    if update is None:
                        if mutation.interruption is not None:
                            self._record_interruption_success(
                                mutation.interruption,
                                payload=self._serialize_framework_state(),
                            )
                        continue
                else:
                    update = mutation.payload

                updated = await self._update_state(mutation.config, update)
            except Exception as exc:
                if mutation.interruption is not None:
                    # This commit intent is now terminally paired with a
                    # failure record. Do not later attach a success boundary
                    # to the same intent via a blind retry; a caller may submit
                    # a fresh interruption plan explicitly.
                    mutation.interruption.recorder.record_interruption_apply_failed(
                        mutation_kind=mutation.interruption.plan.mutation_kind,
                        pre_state_ref=mutation.interruption.actual_pre_ref,
                        post_state_ref=mutation.interruption.plan.post_state_ref,
                        failure_error=ErrorInfo.from_exception(exc),
                    )
                    self._pending_state_mutations = (
                        pending[index + 1 :] + self._pending_state_mutations
                    )
                else:
                    # Preserve ordinary history rewrites and every later
                    # operation for retry instead of silently discarding them.
                    self._pending_state_mutations = pending[index:] + self._pending_state_mutations
                recorder.record_framework_error(ErrorInfo.from_exception(exc))
                logger.exception("Failed to flush pending LangGraph state mutation")
                raise

            try:
                if _config_thread_id(mutation.config) == self._thread_id:
                    await self._advance_checkpoint_baseline_async(updated)
            except Exception as exc:
                # The state write already succeeded. Do not retry it (an
                # append would duplicate); only the trail baseline is
                # degraded, and the next final-state read can recover it.
                recorder.record_framework_error(ErrorInfo.from_exception(exc))
                logger.warning(
                    "Failed to advance LangGraph checkpoint baseline after state write",
                    exc_info=True,
                )

            if mutation.interruption is not None:
                post_payload = b"{}"
                try:
                    state = await self._get_state(mutation.config)
                    if _config_thread_id(mutation.config) == self._thread_id:
                        self._last_state_snapshot = state
                    post_payload = _serialize_state_values(state)
                except Exception as exc:
                    recorder.record_framework_error(ErrorInfo.from_exception(exc))
                    logger.warning(
                        "Failed to read LangGraph state after interruption write",
                        exc_info=True,
                    )
                self._record_interruption_success(
                    mutation.interruption,
                    payload=post_payload,
                )

    @staticmethod
    def _record_interruption_success(
        journal: _PendingInterruptionJournal,
        *,
        payload: bytes,
    ) -> None:
        try:
            journal.recorder.record_state_snapshot(
                journal.plan.post_state_ref,
                payload=payload,
            )
            journal.recorder.record_cancellation_boundary(
                mode=journal.mode,
                reason=journal.plan.mutation_kind,
                caused_by_signal_id=journal.caused_by_signal_id,
            )
        except Exception:
            logger.debug(
                "interruption post-snapshot/boundary journal write failed; "
                "mutation already applied",
                exc_info=True,
            )

    def _interruption_note_update(self, note: str) -> dict[str, Any] | None:
        if not self._messages_key_uses_add_messages():
            return None
        try:
            from langchain_core.messages import SystemMessage

            new_msg: Any = SystemMessage(content=note)
        except ImportError:
            new_msg = {"role": "system", "content": note}
        return {self._messages_key or "messages": [new_msg]}

    def _build_input(
        self,
        text: str,
        context: list[dict[str, str]] | None = None,
    ) -> Any:
        if self._messages_key is None:
            return text
        # Per-turn context (caller-id, system prefix, ``AgentTurnInput.context``)
        # is forwarded as messages prefixed to the user turn so messages-state
        # graphs see session-provided instructions.  The graph already owns
        # conversation history via its checkpointer, so user/assistant items
        # from the caller's context are filtered to avoid duplicating state
        # that will land via ``add_messages`` anyway.
        #
        # This context is *transient* — it describes the current turn's
        # environment, not durable history.  ``add_messages`` would
        # checkpoint it permanently (and a fresh one would be appended
        # every turn), so each injected message gets a stable id we track
        # and delete from graph state once the turn ends (see
        # :meth:`_purge_transient_context`).  ``add_messages`` preserves
        # an explicit ``id`` on dict-form messages, which is what makes
        # the later ``RemoveMessage`` removal possible.
        context_msgs = normalize_context_messages(context, own_history=True)
        self._transient_context_ids = []
        # Only track ids for a later ``RemoveMessage`` purge when the
        # channel actually accumulates via a reducer.  On a plain
        # ``LastValue`` messages channel ``add_messages`` semantics don't
        # apply (the graph manages/overwrites its own list and nothing
        # leaks forward), and a ``RemoveMessage`` purge there would
        # *replace* — wipe — the whole conversation.  Still forward the
        # per-turn context for this turn, just untracked.
        track = self._messages_key_uses_add_messages()
        messages: list[Any] = []
        for item in context_msgs:
            msg: dict[str, Any] = {"role": item["role"], "content": item["content"]}
            if track:
                msg_id = f"easycat-ctx-{uuid4().hex}"
                self._transient_context_ids.append(msg_id)
                msg["id"] = msg_id
            messages.append(msg)
        # Use a dict message rather than a ``("user", text)`` tuple so the
        # user turn matches the dict-form context items above.  On a plain
        # ``LastValue`` messages channel LangGraph stores the value
        # verbatim (no ``add_messages`` normalization), so a raw tuple
        # would crash nodes that read ``state["messages"][-1]["content"]``;
        # a reducer channel normalizes a dict and a tuple identically.
        messages.append({"role": "user", "content": text})
        return {self._messages_key: messages}

    async def _purge_transient_context(self, recorder: AgentRecorder) -> None:
        """Delete this turn's injected transient context from graph state.

        The per-turn system/developer context forwarded by
        :meth:`_build_input` would otherwise be checkpointed permanently
        by ``add_messages``, so every turn would append another stale
        copy.  LangGraph's reducer deletes a message when a
        ``RemoveMessage`` carrying its id is applied, so we re-send one
        per injected id.  Best-effort: a transient checkpointer error
        must not fail an otherwise-successful turn.
        """
        ids = self._transient_context_ids
        if not ids:
            return
        self._transient_context_ids = []
        # Belt-and-suspenders: ids are only assigned when the channel has
        # a reducer (see :meth:`_build_input`), but never issue a bare
        # ``RemoveMessage`` list against a plain ``LastValue`` channel —
        # ``update_state`` would replace the whole messages list with the
        # markers and lose the checkpointed conversation.
        if not self._messages_key_uses_add_messages():
            return
        try:
            from langchain_core.messages import RemoveMessage

            removals: list[Any] = [RemoveMessage(id=mid) for mid in ids]
        except ImportError:
            # ``langgraph`` always installs ``langchain-core``; this only
            # trips under duck-typed tests, where the mock graph's
            # ``update_state`` dedupes by id and the removal markers are
            # still observable for assertions.
            removals = [{"role": "system", "content": "", "id": mid} for mid in ids]
        try:
            await self._update_state(self._config(), {self._messages_key or "messages": removals})
        except Exception as exc:
            recorder.record_framework_error(ErrorInfo.from_exception(exc))
            logger.warning("Failed to purge transient context from LangGraph", exc_info=True)

    async def _commit_partial_assistant(self, text: str, recorder: AgentRecorder) -> None:
        """Append the cancelled turn's partial assistant text to state.

        A turn cancelled mid-stream never let its node return, so the
        partial output the caller already heard isn't in the checkpoint.
        Writing it as an ``AIMessage`` (with a stable id so the
        ``add_messages`` reducer dedupe-replaces it) makes it the last AI
        message, so a follow-up :meth:`apply_interruption` truncates
        *this* turn rather than rewriting the previous turn's reply.

        Skipped on a plain ``LastValue`` channel — ``update_state`` there
        replaces the whole list, which would drop prior turns; and
        best-effort, so a transient checkpointer error can't mask the
        cancellation being propagated.
        """
        if not text:
            return
        if not self._messages_key_uses_add_messages():
            return
        msg_id = f"easycat-partial-{uuid4().hex}"
        try:
            from langchain_core.messages import AIMessage

            msg: Any = AIMessage(content=text, id=msg_id)
        except ImportError:
            msg = {"role": "assistant", "content": text, "id": msg_id}
        try:
            await self._update_state(self._config(), {self._messages_key or "messages": [msg]})
        except Exception as exc:
            recorder.record_framework_error(ErrorInfo.from_exception(exc))
            logger.warning("Failed to commit partial LangGraph turn on cancel", exc_info=True)

    def _extract_graph_stream_chunk(self, event: dict[str, Any]) -> tuple[str, Any] | None:
        """Return ``(mode_name, payload)`` when ``event`` carries a graph-level
        ``stream_mode`` chunk, else ``None``.

        LangGraph wraps ``get_stream_writer`` writes and ``__interrupt__``
        markers as ``(mode_name, payload)`` tuples on the top-level graph's
        ``on_chain_stream`` events when ``stream_mode`` is passed to
        ``astream_events``.  Node-level ``on_chain_stream`` events keep
        their normal chunk shape so the regular translator still picks up
        plain-text deltas from ``RunnableLambda`` nodes.
        """
        if event.get("event") != "on_chain_stream":
            return None
        data = event.get("data")
        if not isinstance(data, dict):
            return None
        chunk = data.get("chunk")
        if not isinstance(chunk, tuple) or len(chunk) != 2:
            return None
        mode_name, payload = chunk
        if not isinstance(mode_name, str) or mode_name not in _GRAPH_STREAM_MODES:
            return None
        return mode_name, payload

    def _handle_graph_stream_chunk(
        self,
        mode_name: str,
        payload: Any,
        recorder: AgentRecorder,
    ) -> Iterator[AgentBridgeEvent]:
        """Translate a ``(mode_name, payload)`` graph-level stream chunk.

        ``custom`` payloads (``get_stream_writer`` writes) surface as
        ``text_delta`` when they carry a ``text`` / ``speak`` / ``say``
        field — unmarked telemetry payloads stay silent.  ``updates``
        payloads carrying ``__interrupt__`` short-circuit into a loud
        :class:`BridgeInputError` because voice runtimes cannot resume a
        paused graph (no UI to collect the human response).
        """
        if mode_name == "custom":
            text = _custom_event_text(payload)
            if text:
                yield AgentBridgeEvent(kind="text_delta", text=text)
            return
        if mode_name == "updates" and isinstance(payload, dict):
            interrupts = payload.get("__interrupt__")
            if interrupts:
                self._raise_hitl_unsupported(interrupts)
            return

    def _raise_hitl_unsupported(
        self,
        interrupts: Any,
        agent_cursor: ExecutionCursor | None = None,
        open_cursors: dict[str, ExecutionCursor] | None = None,
        recorder: AgentRecorder | None = None,
    ) -> None:
        """Raise a :class:`BridgeInputError` describing the HITL mismatch.

        Closes any still-open cursors before raising so the recorder's
        invariant ("every entered unit must be exited") is preserved when
        the error propagates up through ``invoke()``.
        """
        if recorder is not None and open_cursors is not None:
            for cursor in reversed(list(open_cursors.values())):
                recorder.safe_exit_cursor(cursor)
            open_cursors.clear()
        if recorder is not None and agent_cursor is not None:
            recorder.safe_exit_cursor(agent_cursor)
        previews: list[str] = []
        try:
            for it in interrupts:
                value = getattr(it, "value", it)
                previews.append(repr(value)[:120])
        except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
            previews = [repr(interrupts)[:200]]
        raise BridgeInputError(
            "LangGraph graph paused on interrupt() — voice runtimes cannot "
            "resume human-in-the-loop graphs (no UI to collect the human "
            "response).  Rework the graph to avoid interrupt() / "
            "Command(resume=...) when running through LangGraphBridge, or "
            "construct your own bridge that drives astream(stream_mode=[...]) "
            "and surfaces interrupts to your application layer.  "
            f"Pending interrupts: {previews}"
        )

    def _handle_cursor_lifecycle(
        self,
        event: dict[str, Any],
        recorder: AgentRecorder,
        agent_cursor: ExecutionCursor,
        open_cursors: dict[str, ExecutionCursor],
        last_node_by_ns: dict[tuple[str, ...], tuple[str, Any]],
        ended_runs: set[str],
    ) -> None:
        """Open / close workflow_node + model_node cursors for one event.

        Each node invocation in a LangGraph run appears as an
        ``on_chain_start`` event whose ``metadata`` carries
        ``langgraph_node``, ``langgraph_checkpoint_ns`` and
        ``langgraph_step``.  We open a workflow_node cursor keyed by
        ``run_id`` and record a framework handoff whenever the active node
        at a given checkpoint_ns changes.

        Chat-model calls open a ``model_node`` cursor nested inside the
        enclosing workflow_node (or the outer agent_cursor for
        plain-runnable events).  ``_end`` events close the matching
        cursor by ``run_id`` — parallel branches whose ends arrive while
        a sibling cursor is still the stack top are deferred via
        ``ended_runs`` and flushed in LIFO order so the recorder's
        strict stack invariant is preserved.  All transitions are journaled
        via the recorder; this method yields no stream events.
        """
        event_type = event.get("event")
        metadata_raw = event.get("metadata")
        metadata = metadata_raw if isinstance(metadata_raw, dict) else {}
        node_name = metadata.get("langgraph_node")
        step = metadata.get("langgraph_step")
        ns_raw = metadata.get("langgraph_checkpoint_ns", "")
        # Each ``|``-separated segment in ``langgraph_checkpoint_ns`` looks like
        # ``<node>:<run_uuid>``, so two sequential top-level nodes get distinct
        # ns values.  Key handoff tracking by the *parent* namespace (i.e. drop
        # the current node's own segment) so siblings under the same parent
        # share a key and we can detect "previous sibling → next sibling".
        ns: tuple[str, ...] = tuple(ns_raw.split("|")) if ns_raw else ()
        parent_ns: tuple[str, ...] = ns[:-1] if ns else ()

        if event_type == "on_chain_start":
            # Only node-entry chain_starts have langgraph_node set AND a
            # name that matches the node name (other chain_starts are
            # internal runnables inside the node).
            if (
                isinstance(node_name, str)
                and node_name
                and event.get("name") == node_name
                and node_name not in ("__start__", "__end__")
            ):
                run_id = str(event.get("run_id") or uuid4().hex[:8])
                if run_id in open_cursors:
                    return

                prev_entry = last_node_by_ns.get(parent_ns)
                if prev_entry is not None:
                    prev_node, prev_step = prev_entry
                    # A real LangGraph edge moves between super-steps.
                    # Sibling nodes fanned out in the *same* super-step
                    # share this parent namespace but have no edge
                    # between them, so emitting prev→current there would
                    # invent a handoff that never happened.  Suppress it
                    # when both steps are known and equal; still emit
                    # when the step advanced (a real edge) or when step
                    # metadata is absent (preserve prior behaviour).
                    same_step_sibling = (
                        prev_step is not None and step is not None and prev_step == step
                    )
                    if prev_node != node_name and not same_step_sibling:
                        recorder.record_framework_handoff(
                            from_unit=prev_node,
                            to_unit=node_name,
                            reason="langgraph_edge",
                        )

                cursor = ExecutionCursor(
                    unit_id=f"node-{run_id}",
                    unit_kind=UnitKind.WORKFLOW_NODE,
                    display_name=node_name,
                    parent_unit_id=self._nearest_parent_id(
                        open_cursors, event.get("parent_ids") or (), agent_cursor.unit_id
                    ),
                    entered_at=time.monotonic_ns(),
                    committable=False,
                )
                recorder.record_unit_entered(cursor)
                open_cursors[run_id] = cursor
                last_node_by_ns[parent_ns] = (node_name, step)

        elif event_type in ("on_chat_model_start", "on_llm_start"):
            run_id = str(event.get("run_id") or uuid4().hex[:8])
            if run_id in open_cursors:
                return
            cursor = ExecutionCursor(
                unit_id=f"model-{run_id}",
                unit_kind=UnitKind.MODEL_NODE,
                display_name=str(event.get("name") or "model"),
                parent_unit_id=self._nearest_parent_id(
                    open_cursors, event.get("parent_ids") or (), agent_cursor.unit_id
                ),
                entered_at=time.monotonic_ns(),
                committable=False,
            )
            recorder.record_unit_entered(cursor)
            open_cursors[run_id] = cursor

        elif event_type in ("on_chain_end", "on_chat_model_end", "on_llm_end"):
            run_id = str(event.get("run_id") or "")
            if run_id and run_id in open_cursors:
                ended_runs.add(run_id)
                close_top_ended_cursors(recorder, open_cursors, ended_runs)

    def _nearest_parent_id(
        self,
        open_cursors: dict[str, ExecutionCursor],
        parent_ids: Sequence[Any],
        default: str,
    ) -> str:
        """Parent for a new cursor = the deepest ancestor with an open cursor.

        LangChain/LangGraph events carry ``parent_ids`` ordered
        root→leaf.  During a fan-out two sibling nodes (or a sibling
        node and a model) can be open at the same time, so picking "the
        most recent open workflow_node" would parent parallel top-level
        siblings to *each other* and could attach a model run to the
        wrong sibling.  ``open_cursors`` is keyed by LangChain
        ``run_id``; walking ``parent_ids`` from the immediate parent
        outward and matching against it picks the real enclosing node
        (internal runnables aren't in ``open_cursors`` so they're
        skipped), falling back to the agent cursor for top-level runs.
        """
        for pid in reversed(list(parent_ids or ())):
            cursor = open_cursors.get(str(pid))
            if cursor is not None:
                return cursor.unit_id
        return default

    async def _record_checkpoint_trail(
        self,
        config: dict[str, Any],
        baseline_checkpoint_id: str | None,
        recorder: AgentRecorder,
        seen: set[str],
    ) -> None:
        """Record a ``state_snapshot`` per real checkpoint created this turn.

        The locked LangGraph 1.1.x ``astream_events`` schema puts
        ``langgraph_step`` / ``langgraph_checkpoint_ns`` on node-event
        ``metadata`` but no ``checkpoint_id``, so per-step snapshots
        cannot be derived from the event stream (the previous
        event-metadata approach only ever recorded the final
        ``get_state`` snapshot for multi-node graphs).  Instead we read
        the checkpointer's real history: ``get_state_history`` yields
        ``StateSnapshot`` objects newest→oldest, each carrying its real
        ``checkpoint_id``.  We walk back only as far as the checkpoint
        that already existed when the turn started so prior turns aren't
        re-recorded, then emit this turn's checkpoints in chronological
        order.  Dedupe via ``seen`` keeps each id to one record.
        """
        # ``get_state_history`` yields newest→oldest and may be backed by
        # a persistent/remote checkpointer that fetches each checkpoint
        # lazily.  Iterate (don't ``list()``) and stop at the turn's
        # baseline so a long or resumed thread doesn't pay O(total
        # history) fetches and memory every turn — only this turn's new
        # checkpoints are read.
        try:
            turn_ids = await self._checkpoint_ids_since(config, baseline_checkpoint_id)
        except Exception as exc:  # pragma: no cover — best-effort; remote checkpointer error.
            recorder.record_framework_error(ErrorInfo.from_exception(exc))
            logger.warning(
                "Checkpoint history walk failed; skipping checkpoint trail",
                exc_info=True,
            )
            turn_ids = []
        for cid in reversed(turn_ids):  # chronological
            if cid in seen:
                continue
            seen.add(cid)
            recorder.record_state_snapshot(ref=f"langgraph:{cid}")

    def _serialize_framework_state(self) -> bytes:
        state = self._last_state_snapshot
        if state is None and not self._async_state_surface:
            try:
                state = self._graph.get_state(self._config())
            except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
                return b"{}"
        if state is None:
            return b"{}"
        return _serialize_state_values(state)

    def _plan_interruption(self, delivered_text: str, mode: CancellationMode) -> InterruptionPlan:
        replacement = delivered_text + "..." if delivered_text else ""
        pre_ref = f"langgraph-pre-{self._thread_id}"
        post_ref = f"langgraph-post-{self._thread_id}"
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
        self.replace_last_assistant_text(replacement)

    def _rewrite_last_ai_message(self, replacement: str) -> None:
        """Replace the last AI message in graph state with ``replacement``.

        LangGraph's ``add_messages`` reducer dedupes by message ``id``,
        so re-sending the same AI message with an edited ``content``
        field replaces it in place instead of appending.  If no AI
        message exists yet (e.g. the graph hasn't produced one), this
        is a no-op.

        Skipped on a plain ``LastValue`` channel: re-sending ``[msg]``
        there would *replace* the whole messages list (dropping every
        other turn) instead of dedupe-replacing by id.

        Also skipped when the most recent turn produced no assistant
        output at all (cancelled before its first token): the checkpoint
        holds no current-turn AI message, so the backward walk below
        would otherwise truncate the *previous* turn's already-delivered
        reply and corrupt prior conversation state.

        As a further guard the backward walk stops at the most recent
        human message: a *successful* turn can also leave no AI message
        in the checkpoint (a router branch that only emits custom
        ``get_stream_writer`` text or returns ``{}``), which the
        cancelled-turn flag above does not cover.  Bounding the scan at
        the latest user turn keeps the rewrite from reaching back into a
        prior turn's reply in that case too.
        """
        if self._turn_produced_no_assistant:
            logger.debug(
                "rewrite_last_ai: last turn produced no assistant output; "
                "skipping so the prior turn's reply isn't corrupted"
            )
            return
        if not self._messages_key_uses_add_messages():
            logger.debug("rewrite_last_ai: messages channel has no reducer; skipping")
            return
        try:
            state = self._graph.get_state(self._config())
        except Exception:
            logger.debug("rewrite_last_ai: get_state failed", exc_info=True)
            return
        update = self._rewrite_update_for_state(state, replacement)
        if update is None:
            return
        updated = self._graph.update_state(self._config(), update)
        self._advance_checkpoint_baseline(updated)

    def _rewrite_update_for_state(self, state: Any, replacement: str) -> dict[str, Any] | None:
        """Return a state update without mutating the cached snapshot."""
        if self._turn_produced_no_assistant:
            logger.debug(
                "rewrite_last_ai: last turn produced no assistant output; "
                "skipping so the prior turn's reply isn't corrupted"
            )
            return None
        if not self._messages_key_uses_add_messages():
            logger.debug("rewrite_last_ai: messages channel has no reducer; skipping")
            return None
        values = getattr(state, "values", None) or {}
        key = self._messages_key or "messages"
        messages = values.get(key) if isinstance(values, dict) else None
        if not messages:
            return None

        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if _message_is_human(msg):
                # Reached the latest user turn without finding an AI
                # message after it: this turn appended no assistant
                # message (e.g. a router branch that only emits custom
                # ``get_stream_writer`` text or returns ``{}``).  The
                # newest AI message belongs to a prior, already-delivered
                # turn — rewriting it would corrupt checkpointed history
                # that future turns condition on.  No-op instead.
                logger.debug(
                    "rewrite_last_ai: no AI message after the latest user "
                    "message; skipping so prior history isn't corrupted"
                )
                return None
            if _message_is_ai(msg):
                msg = copy.deepcopy(msg)
                content = _content_of(msg)
                if isinstance(content, list):
                    text_parts = [
                        p for p in content if isinstance(p, dict) and p.get("type") == "text"
                    ]
                    if text_parts:
                        originals = [str(p.get("text", "")) for p in text_parts]
                        splits = split_replacement_by_original_parts(originals, replacement)
                        for part, repl in zip(text_parts, splits):
                            part["text"] = repl
                    else:
                        _set_content(msg, replacement)
                else:
                    _set_content(msg, replacement)
                return {key: [msg]}
        return None

    def _advance_checkpoint_baseline(self, updated_config: Any) -> None:
        """Move the checkpoint-trail baseline past a between-turn write.

        ``replace_last_assistant_text`` / ``apply_interruption`` (via
        :meth:`_rewrite_last_ai_message`) and :meth:`append_interruption_note`
        call ``update_state`` *between* turns, which creates a fresh
        checkpoint.  ``_last_checkpoint_id`` still points at the prior
        turn's final checkpoint, so the next turn's
        :meth:`_record_checkpoint_trail` walk — which stops at that
        baseline — would re-emit this rewrite/interruption checkpoint as
        a ``state_snapshot`` belonging to the *following* user turn (and
        the interruption already recorded its own snapshot via the
        recorder, so it would be a duplicate).  Advancing the baseline to
        the just-created checkpoint keeps the trail correctly attributed.

        ``update_state`` returns the new ``RunnableConfig`` carrying the
        new ``checkpoint_id``; prefer it to avoid an extra checkpointer
        round-trip, fall back to a ``get_state`` probe for duck-typed
        graphs, and leave the baseline untouched on any failure (degrades
        to the pre-fix behaviour, never a hard error).
        """
        cid = _checkpoint_id_from_config(updated_config)
        if cid is None:
            try:
                cid = _get_checkpoint_id(self._graph.get_state(self._config()))
            except Exception:
                logger.debug(
                    "Failed to refresh checkpoint baseline after state write",
                    exc_info=True,
                )
                return
        if cid:
            self._last_checkpoint_id = cid

    async def _advance_checkpoint_baseline_async(self, updated_config: Any) -> None:
        cid = _checkpoint_id_from_config(updated_config)
        if cid is None:
            state = await self._get_state(self._config())
            self._last_state_snapshot = state
            cid = _get_checkpoint_id(state)
        if cid:
            self._last_checkpoint_id = cid


# ── Helpers ──────────────────────────────────────────────────────


def _serialize_state_values(state: Any) -> bytes:
    values = getattr(state, "values", None)
    if values is None:
        return b"{}"
    return serialize_framework_state(_safe_values_for_serialization(values))


def _sync_checkpoint_ids_since(
    graph: Any,
    config: dict[str, Any],
    baseline_checkpoint_id: str | None,
) -> list[str]:
    """Walk a lazy sync history entirely in one worker thread."""
    turn_ids: list[str] = []
    for snapshot in graph.get_state_history(config):
        checkpoint_id = _get_checkpoint_id(snapshot)
        if not checkpoint_id or checkpoint_id == baseline_checkpoint_id:
            break
        turn_ids.append(checkpoint_id)
    return turn_ids


def _checkpoint_id_from_config(config: Any) -> str | None:
    if not isinstance(config, dict):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    checkpoint_id = configurable.get("checkpoint_id")
    return str(checkpoint_id) if checkpoint_id else None


def _is_add_messages_reducer(operator: Any) -> bool:
    """Whether ``operator`` is LangGraph's ``add_messages`` reducer.

    ``Annotated[list, add_messages]`` compiles to a channel whose
    ``.operator`` is *identically* LangGraph's ``add_messages`` function
    (``langgraph.graph.message``), so an identity check is exact for the
    supported path.  Fall back to a module/name match so a re-exported
    or lightly wrapped ``add_messages`` (and the duck-typed test
    reducer) is still recognised, while a generic ``operator.add`` or a
    custom accumulator is not — those only append, so the
    ``RemoveMessage`` / id-keyed-replace machinery must stay off.

    The documented ``Annotated[..., add_messages(format="langchain-openai")]``
    form calls ``add_messages`` with only keyword args, which returns a
    ``functools.partial(add_messages, ...)`` — still genuine
    ``add_messages`` merge semantics — so unwrap the partial chain
    before matching, otherwise a valid channel is misread as a generic
    reducer and the machinery is wrongly disabled.
    """
    for _ in range(5):  # bounded unwrap of nested functools.partial wrappers
        if not isinstance(operator, functools.partial):
            break
        operator = operator.func
    try:
        from langgraph.graph.message import add_messages

        if operator is add_messages:
            return True
    except Exception:  # noqa: BLE001, S110 intentional boundary or best-effort cleanup
        pass
    if getattr(operator, "__module__", "") == "langgraph.graph.message":
        return True
    name = getattr(operator, "__name__", "") or ""
    qualname = getattr(operator, "__qualname__", "") or ""
    return "add_messages" in name or "add_messages" in qualname


def _pending_interrupts(state: Any) -> tuple[Any, ...]:
    """Return any ``Interrupt`` objects on the graph state's tasks.

    LangGraph surfaces pending HITL interrupts as ``state.tasks[i].interrupts``
    after ``astream`` / ``astream_events`` completes.  Custom checkpointers
    or older LangGraph versions may not fold ``__interrupt__`` into the
    ``updates`` channel during streaming, so this post-stream sweep is the
    belt-and-suspenders to the in-stream detection in
    :meth:`LangGraphBridge._handle_graph_stream_chunk`.
    """
    tasks = getattr(state, "tasks", None)
    if not tasks:
        return ()
    collected: list[Any] = []
    try:
        for task in tasks:
            for interrupt in getattr(task, "interrupts", ()) or ():
                collected.append(interrupt)  # noqa: PERF402 flatten nested interrupt collections
    except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
        return ()
    return tuple(collected)


def _bound_thread_id(graph: Any) -> str | None:
    """Thread id bound onto the graph via ``graph.with_config(...)``.

    ``CompiledStateGraph.with_config(configurable={"thread_id": ...})``
    returns a copy of the graph carrying the merged config on
    ``graph.config``; LangChain's generic ``RunnableBinding`` wrapper
    stores it the same way and nests the real graph under ``.bound``.
    Resuming a conversation by binding the thread id this way is a
    common LangGraph pattern and the *only* channel ``auto_adapt_agent``
    has to carry a resume thread through — so honour it instead of
    minting a fresh UUID and writing to an empty checkpoint.  Returns
    ``None`` (fresh-thread behaviour) when nothing is bound.
    """
    obj = graph
    for _ in range(5):  # bounded walk through nested RunnableBinding wrappers
        if obj is None:
            break
        config = getattr(obj, "config", None)
        if isinstance(config, dict):
            configurable = config.get("configurable")
            if isinstance(configurable, dict):
                tid = configurable.get("thread_id")
                if isinstance(tid, str) and tid:
                    return tid
        obj = getattr(obj, "bound", None)
    return None


def _bound_config(graph: Any) -> dict[str, Any]:
    """Full config bound onto the graph via ``graph.with_config(...)``.

    The sibling of :func:`_bound_thread_id`, but returns the *entire*
    bound config (tags, recursion_limit, and every ``configurable`` key —
    tenant ids, auth tokens, feature flags read by nodes) rather than
    just the thread id.  Walks the same bounded ``RunnableBinding``
    chain; configs are merged inner→outer so an outer wrapper's value
    wins (matching the outer-first precedence of :func:`_bound_thread_id`
    and LangChain ``with_config`` merge order), and the ``configurable``
    sub-dicts are deep-merged rather than replaced.  Returns a fresh,
    independently-mutable ``{}`` when nothing is bound.
    """
    layers: list[dict[str, Any]] = []
    obj = graph
    for _ in range(5):  # bounded walk through nested RunnableBinding wrappers
        if obj is None:
            break
        config = getattr(obj, "config", None)
        if isinstance(config, dict):
            layers.append(config)
        obj = getattr(obj, "bound", None)
    merged: dict[str, Any] = {}
    configurable: dict[str, Any] = {}
    # Innermost first so an outer wrapper's value overrides an inner one.
    for config in reversed(layers):
        for key, value in config.items():
            if key == "configurable" and isinstance(value, dict):
                configurable.update(value)
            else:
                merged[key] = value
    if configurable:
        merged["configurable"] = configurable
    return merged


def _config_thread_id(config: dict[str, Any]) -> str | None:
    """Return the thread id captured in a pending mutation's config."""
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    thread_id = configurable.get("thread_id")
    return str(thread_id) if thread_id else None


def _bound_checkpoint_id(graph: Any) -> str | None:
    """``checkpoint_id`` bound onto the graph via ``graph.with_config(...)``.

    A bound ``configurable.checkpoint_id`` is LangGraph's resume /
    time-travel cursor.  Derived from :func:`_bound_config` so it is
    found whether the caller bound it on the graph copy or on an
    enclosing ``RunnableBinding`` wrapper.  Returns ``None`` (fresh-run
    behaviour) when nothing is bound.  The bridge treats it as one-shot
    (see :class:`LangGraphBridge` ``_resume_checkpoint_id``).
    """
    configurable = _bound_config(graph).get("configurable")
    if isinstance(configurable, dict):
        cp = configurable.get("checkpoint_id")
        if cp:
            return str(cp)
    return None


def _get_checkpoint_id(state: Any) -> str | None:
    config = getattr(state, "config", None)
    if not isinstance(config, dict):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    cp = configurable.get("checkpoint_id")
    return str(cp) if cp else None


def _messages_tail(state: Any, key: str = "messages") -> Any:
    values = getattr(state, "values", None)
    if isinstance(values, dict):
        msgs = values.get(key)
        if msgs:
            return msgs[-1]
    return None


def _extract_message_text(message: Any) -> str:
    """Best-effort text extraction from an ``AIMessage``-like object.

    Used as the spoken-text fallback when a node produces a final
    message but no streaming chat-model tokens (e.g. a node that builds
    an ``AIMessage`` from a non-streaming model or that transforms the
    model output).  ``AIMessage.text`` is the framework-provided shortcut
    that flattens ``content_blocks``; we prefer it when available and
    fall back to walking raw ``content`` for duck-typed messages.
    """
    if message is None:
        return ""
    text = getattr(message, "text", None)
    if isinstance(text, str) and text:
        return text
    content = _content_of(message)
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


def _message_is_ai(msg: Any) -> bool:
    msg_type = getattr(msg, "type", None)
    if msg_type == "ai":
        return True
    if isinstance(msg, dict):
        return msg.get("role") == "assistant" or msg.get("type") == "ai"
    return False


def _message_is_human(msg: Any) -> bool:
    msg_type = getattr(msg, "type", None)
    if msg_type == "human":
        return True
    if isinstance(msg, dict):
        return msg.get("role") == "user" or msg.get("type") == "human"
    return False


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


def _safe_values_for_serialization(values: Any) -> Any:
    """Reduce a LangGraph state-values dict to JSON-safe primitives.

    Messages are converted to ``{"role", "content"}`` dicts; other
    values are passed through and ``default=str`` in the caller handles
    the rest.
    """
    if isinstance(values, dict):
        out: dict[str, Any] = {}
        for k, v in values.items():
            if isinstance(v, list) and v and any(hasattr(m, "type") for m in v):
                out[k] = [_message_summary(m) for m in v]
            else:
                out[k] = v
        return out
    return values


def _message_summary(msg: Any) -> dict[str, Any]:
    role = ""
    msg_type = getattr(msg, "type", None)
    if msg_type == "ai":
        role = "assistant"
    elif msg_type == "human":
        role = "user"
    elif msg_type == "system":
        role = "system"
    elif msg_type == "tool":
        role = "tool"
    elif isinstance(msg, dict):
        role = msg.get("role") or msg.get("type") or ""
    content = _content_of(msg)
    return {"role": role, "content": content}


__all__: Sequence[str] = ["LangGraphBridge"]
