"""Streaming agent consumption: translates an agent stream into TTS payloads.

Separates the "understanding agent stream events" concern from Session's
orchestration role.  Session wires this to a TTS queue and handles
concurrency; this module handles text buffering, sentence splitting,
markdown handling, and event emission.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from easycat._turn_context import TurnContext
from easycat.events import (
    AgentDelta,
    Error,
    ErrorStage,
    ToolCallDelta,
    ToolCallResult,
    ToolCallStarted,
)
from easycat.integrations.agents._text_stream import AgentTextStream, AgentTextUpdate
from easycat.session.text import (
    _FIRST_PHRASE_TARGET_CHARS,
    _split_first_phrase,
    markdown_open_state,
    split_at_sentence_boundaries,
    split_first_clause,
)
from easycat.strip_markdown import strip_markdown
from easycat.tts.input import TTSInput

logger = logging.getLogger(__name__)

# Characters that can make buffered text newly eligible for TTS sentence emission.
# When markdown stripping is enabled, avoid re-running delimiter checks, markdown
# regexes, and sentence segmentation on every tiny streamed delta; most deltas
# cannot complete a sentence and are therefore safe to buffer until one of these
# characters (or final stream flush) arrives.
#
# This MUST stay a strict superset of the sentence segmenter's terminal
# punctuation (".!?。！？．" — note the fullwidth full stop U+FF0E) so a delta that
# closes a sentence always triggers a recheck. The invariant is enforced by
# test_streaming_trigger_chars_superset_of_segmenter_terminators.
_STREAMING_SENTENCE_TRIGGER_CHARS = frozenset(".!?。！？．\n\r")

# Once a markdown construct is known to be open, sentence punctuation inside it
# is not enough to safely emit text.  Re-check the rolling markdown window only
# when a later delta contains a character that can plausibly close or otherwise
# disambiguate a markdown span.
_MARKDOWN_RECHECK_CHARS = frozenset("`*_~])")

# Mid-sentence clause boundaries used only for the first payload of a turn (see
# ``_SentenceStreamBuffer._first_payload_pending``).  Kept distinct from
# ``_STREAMING_SENTENCE_TRIGGER_CHARS`` so the segmenter-terminator superset
# invariant (test_streaming_trigger_chars_superset_of_segmenter_terminators)
# is unaffected; while a first clause is pending, a delta carrying one of these
# also warrants a markdown-mode recheck.
_FIRST_CLAUSE_TRIGGER_CHARS = frozenset(",;:")

# Places where the markdown buffer may drop already-spoken raw text (see
# ``_SentenceStreamBuffer._compact``), most preferred first: a paragraph
# break, a line ending, then a sentence end (the segmenter's terminators,
# with the trailing space the ASCII ones need to end a sentence mid-line).
_PARAGRAPH_BREAKS = ("\n\n", "\r\n\r\n")
_SENTENCE_ENDS = (". ", "! ", "? ", "。", "！", "？", "．")


def _compaction_cuts(raw: str, bound: int) -> list[int]:
    """Return up to three raw offsets to try compacting at, most preferred first.

    Only separators wholly inside ``raw[:bound]`` count.  A paragraph break
    or line ending leaves the tail at a real line start.  A sentence end does
    not, so it is offered only when the tail already starts with a letter
    (no later delta can turn it into a heading, list, quote or rule marker)
    and its line is not an ATX heading (whose closer, still to come, depends
    on the opener in the head).
    """
    cuts: list[int] = []
    paragraph = max(
        (raw.rfind(sep, 0, bound) + len(sep) for sep in _PARAGRAPH_BREAKS if sep in raw),
        default=0,
    )
    line = raw.rfind("\n", 0, bound) + 1
    for cut in (paragraph, line, _last_sentence_cut(raw, bound)):
        if cut > 0 and cut not in cuts:
            cuts.append(cut)
    return cuts


def _last_sentence_cut(raw: str, bound: int) -> int:
    """Return the offset after the last eligible sentence end before *bound*, or 0."""
    best = 0
    for sep in _SENTENCE_ENDS:
        end = bound
        while (found := raw.rfind(sep, best, end)) >= 0:
            cut = found + len(sep)
            if raw[cut : cut + 1].isalpha() and not raw.startswith(
                "#", raw.rfind("\n", 0, cut) + 1
            ):
                best = cut
                break
            end = found + len(sep) - 1
    return best


def _spoken_prefix_end(stripped: str, spoken: str) -> int | None:
    """Return where *stripped* continues past the *spoken* prefix, or ``None``.

    Tolerates a change confined to *spoken*'s trailing whitespace: a heading
    closer that arrives after ``# Title. `` was split off removes the space
    before it, so the match falls back to the spoken content and skips the
    whitespace that follows it.
    """
    if stripped.startswith(spoken):
        return len(spoken)
    content = spoken.rstrip()
    if not stripped.startswith(content):
        return None
    start = len(content)
    while start < len(stripped) and stripped[start].isspace():
        start += 1
    return start


@dataclass
class AgentStreamResult:
    """Result returned by :func:`consume_agent_stream`."""

    text: str = ""
    structured_output: Any = None
    # Only ``Exception`` is ever stored here (``consume_agent_stream``
    # lets CancelledError and other BaseExceptions propagate).
    error: Exception | None = None
    interrupted: bool = False


async def emit_tool_event(
    event: Any,
    kind: str | None,
    *,
    emit: Callable[[Any], Awaitable[None]],
    session_id: str | None = None,
    turn_id: str | None = None,
    tool_span: Callable[[], AbstractContextManager[Any]] | None = None,
) -> bool:
    """Translate a tool-related bridge event into an EasyCat tool event.

    This is the single source of truth for the
    ``tool_started`` / ``tool_delta`` / ``tool_result`` →
    ``ToolCallStarted`` / ``ToolCallDelta`` / ``ToolCallResult`` mapping,
    shared by the streaming voice path (:func:`consume_agent_stream`) and
    the text path (``TurnRunner._execute_text_turn``) so the two cannot
    drift.

    ``session_id`` / ``turn_id`` are stamped onto the emitted event when
    provided (the text path needs this because it runs outside the
    TurnManager's active-turn window, where ``Session._emit`` would
    otherwise stamp a ``None`` turn id).  ``tool_span`` is an optional
    zero-arg factory returning a context manager wrapped around the
    ``ToolCallStarted`` emit for per-tool observability.

    Returns ``True`` when the event was a tool kind (and was emitted),
    ``False`` otherwise.  Callers remain responsible for any pending
    tool-call bookkeeping around the emit.
    """
    if kind == "tool_started":
        span = tool_span() if tool_span is not None else contextlib.nullcontext()
        with span:
            await emit(
                ToolCallStarted(
                    tool_name=event.tool_name,
                    call_id=event.call_id,
                    session_id=session_id,
                    turn_id=turn_id,
                )
            )
        return True
    if kind == "tool_delta":
        await emit(
            ToolCallDelta(
                call_id=event.call_id,
                delta=event.text,
                session_id=session_id,
                turn_id=turn_id,
            )
        )
        return True
    if kind == "tool_result":
        await emit(
            ToolCallResult(
                call_id=event.call_id,
                result=event.result,
                session_id=session_id,
                turn_id=turn_id,
            )
        )
        return True
    return False


class _SentenceStreamBuffer:
    """Accumulates text deltas and queues complete sentences for TTS.

    Owns the buffering/sentence-splitting/markdown-window state that used
    to live as locals inside :func:`consume_agent_stream`:

    - plain mode: split at sentence boundaries on every delta;
    - markdown mode: defer the regex-heavy strip/split work until a delta
      could plausibly complete a sentence or close a markdown span.

    Markdown mode keeps the *raw* markdown of the turn in ``_text`` and the
    stripped text already split off for TTS in ``_spoken``.  Every recheck
    strips the raw text once and speaks only what follows ``_spoken``; it
    never strips text that was already stripped.  Re-stripping a stripped
    remainder (the old design) read a URL's ``_a_`` or an escaped ``\\_b\\_``
    as emphasis and lost a heading opener before its closer arrived.
    """

    def __init__(
        self,
        *,
        tts_queue: asyncio.Queue[TTSInput | None],
        prepare_tts_payload: Callable[..., TTSInput],
        strip_md: bool,
    ) -> None:
        self._tts_queue = tts_queue
        self._prepare = prepare_tts_payload
        self._strip_md = strip_md
        # Plain mode: text not yet queued.  Markdown mode: the raw markdown
        # of the turn, minus any already-spoken text ``_compact`` dropped.
        self._text = ""
        # Markdown mode only.  The stripped turn is ``_carry`` (already
        # stripped text, empty except after the fallback in
        # ``_unspoken_window``) followed by the stripped form of ``_text``;
        # ``_spoken`` is the prefix of it already split off for TTS.  The
        # snapshot records the last strip that lined up with ``_spoken``: raw
        # length and stripped turn (which starts with ``_spoken``), so the
        # fallback can rebuild what is left to speak.
        self._carry = ""
        self._spoken = ""
        self._snapshot_raw_len = 0
        self._snapshot_stripped = ""
        # The first payload of a turn is split at the first natural clause
        # boundary (comma/semicolon/colon) instead of a full sentence to cut
        # time-to-first-audio.  Cleared once the first payload is queued;
        # every later emission keeps full-sentence granularity.
        self._first_payload_pending = True
        self._markdown_window_open = False
        # True when ``_markdown_window_open`` is held open solely by a trailing
        # closed link/image label ``[label]`` awaiting its ``(destination)``.  A
        # non-space, non-``(`` continuation disambiguates that case, so we
        # recheck eagerly rather than waiting for a markdown-closer character.
        self._awaiting_link_dest = False
        self._payload_count = 0
        self._suppressed = False

    @property
    def has_payloads(self) -> bool:
        """Whether any text has crossed the replaceable TTS boundary."""
        return self._payload_count > 0

    def replace(self, text: str) -> None:
        """Replace the pending buffer wholesale (used by the ``done`` event).

        The consumer only calls this when no text delta arrived, so nothing
        has been spoken.  In markdown mode *text* is the turn's full raw
        markdown: ``flush`` speaks whatever of it follows ``_spoken``, and if
        it does not line up with ``_spoken`` the snapshot below makes the
        fallback speak *text* whole, as this method always did.
        """
        if not self._suppressed:
            self._text = text
            self._carry = ""
            self._snapshot_raw_len = 0
            self._snapshot_stripped = self._spoken

    async def replace_pending(self, text: str) -> bool:
        """Replace all uncommitted text and re-run streaming segmentation."""
        if self.has_payloads:
            raise RuntimeError("cannot replace text after a TTS payload was admitted")
        if self._suppressed:
            return False
        self._reset_text()
        self._first_payload_pending = True
        self._markdown_window_open = False
        self._awaiting_link_dest = False
        return await self.add_delta(text)

    def suppress(self) -> None:
        """Drop pending text and prevent further payloads for this turn."""
        self._suppressed = True
        self._reset_text()

    async def add_delta(self, delta: str) -> bool:
        """Buffer *delta* and report whether it queued a TTS payload."""
        if self._suppressed:
            return False
        if self._strip_md:
            return await self._add_markdown_delta(delta)
        self._text += delta
        ready, self._text = self._split_pending(self._text)
        if ready:
            return await self._put_payload(ready, is_final=False)
        return False

    async def _add_markdown_delta(self, delta: str) -> bool:
        had_trailing_numeric_separator = self._has_trailing_numeric_separator(self._text)
        self._text += delta

        # Markdown stripping is regex-heavy and sentence splitting scans the
        # whole pending window.  Do not repeat that work for tiny deltas
        # that cannot complete a sentence.  If markdown is already known
        # to be open, punctuation inside the open span is also not enough;
        # wait until a plausible markdown closer arrives before rechecking.
        #
        # Exception: when the window is open *only* because a trailing
        # ``[label]`` is awaiting its ``(destination)``, any non-space,
        # non-``(`` character proves it is not a link and re-opens
        # streaming.  Recheck eagerly in that case so an ordinary-prose
        # continuation does not stall emission until the final flush.
        if self._markdown_window_open:
            recheck = any(ch in delta for ch in _MARKDOWN_RECHECK_CHARS)
            if not recheck and self._awaiting_link_dest:
                recheck = any(not ch.isspace() and ch != "(" for ch in delta)
            if not recheck:
                return False
        else:
            triggers = _STREAMING_SENTENCE_TRIGGER_CHARS
            if self._first_payload_pending:
                # The first payload may emit at a clause boundary, so a delta
                # carrying ``,``/``;``/``:`` is also worth a recheck.
                triggers = triggers | _FIRST_CLAUSE_TRIGGER_CHARS
            # Before the first payload nothing is spoken, so ``_text`` is the
            # whole raw turn.  This is only a cheap pre-filter: raw length
            # approximates the stripped window, and the real split below
            # runs on stripped text.
            bounded_first_phrase_ready = (
                self._first_payload_pending
                and len(self._text) >= _FIRST_PHRASE_TARGET_CHARS
                and bool(_split_first_phrase(self._text)[0])
            )
            if (
                not bounded_first_phrase_ready
                and not had_trailing_numeric_separator
                and not any(ch in delta for ch in triggers)
            ):
                return False

        # Openness is judged on the raw turn text.  Its spoken part was closed
        # when it was split off, so only the unspoken tail can hold it open.
        self._markdown_window_open, self._awaiting_link_dest = markdown_open_state(self._text)
        if self._markdown_window_open:
            return False

        ready, _remaining = self._split_pending(self._unspoken_window())
        # Commit the split before queueing.  The first-payload handoff yields
        # after the payload is accepted, so cancellation in that window must
        # not leave the queued prefix outside ``_spoken`` for a later flush to
        # duplicate.
        self._spoken += ready
        if self._carry and self._spoken.startswith(self._carry):
            # The carried text is all spoken: fold it away.
            carried = len(self._carry)
            self._carry = ""
            self._spoken = self._spoken[carried:]
            self._snapshot_stripped = self._snapshot_stripped[carried:]
        self._compact()
        queued = False
        if ready:
            queued = await self._put_payload(ready, is_final=False)
        return queued

    async def flush(self) -> bool:
        """Queue remaining text and report whether it produced a payload."""
        if self._suppressed:
            self._reset_text()
            return False
        text = self._text if self._text.strip() else ""
        if text and self._strip_md:
            # Strip the raw turn once, as complete trimmed text (which also
            # drops a heading closer on the last line), and speak what
            # follows the spoken prefix.
            text = self._unspoken_window(final=True).strip()
        # Commit the flush before queueing. The first-payload handoff yields
        # after the payload is accepted, so cancellation in that window must
        # not leave the already-queued final text pending for a later flush to
        # duplicate.
        self._reset_text()
        if not text:
            return False
        return await self._put_payload(text, is_final=True)

    def _reset_text(self) -> None:
        self._text = ""
        self._carry = ""
        self._spoken = ""
        self._snapshot_raw_len = 0
        self._snapshot_stripped = ""

    def _strip_turn(self, *, final: bool) -> str:
        """Return the stripped turn: ``_carry`` plus ``_text`` stripped once.

        The final strip is complete and trimmed, as ``strip_markdown``'s
        default (``trim=True`` also drops a heading closer on the last line);
        the raw text's leading whitespace is kept where it meets the carry.
        """
        if not final:
            return self._carry + strip_markdown(self._text, trim=False, normalize_code_spans=True)
        stripped = strip_markdown(self._text, trim=True, normalize_code_spans=True)
        if self._carry:
            lead = self._text[: len(self._text) - len(self._text.lstrip())]
            stripped = f"{self._carry}{lead}{stripped}".lstrip()
        return stripped

    def _unspoken_window(self, *, final: bool = False) -> str:
        """Strip the raw markdown once and return the part not yet spoken.

        Stripping is prefix-stable once markdown is closed, so the stripped
        turn almost always starts with ``_spoken``.  Two exceptions are
        handled without ever speaking a spoken prefix twice:

        - Only ``_spoken``'s trailing whitespace changed (a heading closer
          arriving after ``# Title. `` was split off drops the space before
          it): line up on the spoken content and skip the whitespace.
        - Anything else (a delimiter pairing across the spoken boundary in a
          way ``markdown_open_state`` did not foresee, e.g. a later ``**``
          closing one that was spoken literally): move the stripped text
          left unspoken at the last consistent strip into ``_carry`` and keep
          only the raw text that arrived since in ``_text``.  The carry is
          never stripped again, so a URL or escaped marker in it survives;
          the price is that markdown in the new raw text can no longer pair
          with anything before the snapshot, so it may be spoken literally.
        """
        stripped = self._strip_turn(final=final)
        # The final strip is trimmed, so line up on the spoken text without
        # its leading whitespace.
        start = _spoken_prefix_end(stripped, self._spoken.lstrip() if final else self._spoken)
        if start is None:
            self._carry = self._snapshot_stripped[len(self._spoken) :]
            self._text = self._text[self._snapshot_raw_len :]
            self._spoken = ""
            stripped = self._strip_turn(final=final)
            start = 0
        self._spoken = stripped[:start]
        self._snapshot_raw_len = len(self._text)
        self._snapshot_stripped = stripped
        return stripped[start:]

    def _compact(self) -> None:
        """Drop fully spoken raw text, bounding the per-recheck strip cost.

        Each recheck strips the whole raw text, so without this a long turn
        costs O(n^2).  Up to three candidate cuts are tried (see
        ``_compaction_cuts``), and one is taken only when it is provably
        invisible: the stripped head is wholly inside ``_spoken`` and
        stripping head and tail apart gives exactly the current stripped
        turn, so no markdown construct pairs across the cut.
        """
        if self._carry or not self._spoken:
            return
        raw = self._text
        # Markdown usually makes raw text longer than its stripped form, so
        # the unspoken stripped tail maps to at least that many raw
        # characters; searching only before it is a hint, the checks are exact.
        bound = len(raw) - (len(self._snapshot_stripped) - len(self._spoken))
        for cut in _compaction_cuts(raw, bound):
            head = strip_markdown(raw[:cut], trim=False, normalize_code_spans=True)
            if not self._spoken.startswith(head):
                continue
            tail = raw[cut:]
            tail_stripped = strip_markdown(tail, trim=False, normalize_code_spans=True)
            if head + tail_stripped != self._snapshot_stripped:
                continue
            self._text = tail
            self._spoken = self._spoken[len(head) :]
            self._snapshot_raw_len = len(tail)
            self._snapshot_stripped = tail_stripped
            return

    def _split_pending(self, text: str) -> tuple[str, str]:
        """Split *text* for emission, honouring the first-payload window.

        While ``_first_payload_pending`` is set, the first emission of the
        turn is cut at the first natural clause boundary (cutting
        time-to-first-audio); once a non-empty clause is found the flag is
        cleared so every later emission keeps full-sentence granularity.
        Markdown state is always CLOSED at the call sites, so the
        clause-level split cannot rewrite an open span.
        """
        if self._first_payload_pending:
            ready, remaining = split_first_clause(text)
            if ready:
                self._first_payload_pending = False
                return ready, remaining
            ready, remaining = _split_first_phrase(text)
            if ready:
                self._first_payload_pending = False
                return ready, remaining
        return split_at_sentence_boundaries(text)

    @staticmethod
    def _has_trailing_numeric_separator(text: str) -> bool:
        # Mirrors the end-of-buffer lookahead branches in
        # ``_is_numeric_separator``; keep the character classes in sync.
        return len(text) >= 2 and text[-2].isdigit() and text[-1] in ".．:,，"

    async def _put_payload(self, text: str, *, is_final: bool) -> bool:
        payload = self._prepare(text, is_streaming=True, is_final=is_final)
        if payload.text.strip():
            await self._tts_queue.put(payload)
            first_payload = self._payload_count == 0
            self._payload_count += 1
            if first_payload:
                # Two turns are intentional. The first wakes the waiting TTS
                # consumer; that consumer creates the synthesis task and yields
                # once to start it. The second lets the new task enter the TTS
                # provider before this agent task consumes a terminal/done event.
                # Public lifecycle/audio remains held by first_tts_payload_ready.
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            return True
        return False


async def consume_agent_stream(
    stream_factory: Callable[[], AsyncIterator[Any]],
    *,
    cancel_token: Any | None,
    tts_queue: asyncio.Queue[TTSInput | None],
    emit: Callable[[Any], Awaitable[None]],
    prepare_tts_payload: Callable[..., TTSInput],
    strip_md: bool,
    turn: TurnContext,
    first_tts_payload_ready: asyncio.Future[bool] | None = None,
    abort_event: asyncio.Event | None = None,
    is_active: Callable[[], bool] | None = None,
    on_tts_replacement_conflict: Callable[[], Awaitable[None]] | None = None,
    consumer_gone: Callable[[], bool] | None = None,
) -> AgentStreamResult:
    """Consume an :class:`AgentBridgeEvent` stream and queue TTS payloads.

    This is the "translation layer" between bridge events and TTS
    payloads.  It accumulates text deltas, splits at sentence boundaries,
    handles markdown buffering, emits EasyCat-level events, and drains
    in-flight tool calls during cancellation.

    ``stream_factory`` is a zero-argument callable returning the async
    iterator to consume — typically
    ``lambda: agent_stage.execute_streaming(...)``.

    Returns an :class:`AgentStreamResult` with the accumulated text,
    structured output, and any error that occurred.
    """
    consumer = _AgentStreamConsumer(
        cancel_token=cancel_token,
        tts_queue=tts_queue,
        emit=emit,
        prepare_tts_payload=prepare_tts_payload,
        strip_md=strip_md,
        turn=turn,
        first_tts_payload_ready=first_tts_payload_ready,
        abort_event=abort_event,
        is_active=is_active,
        on_tts_replacement_conflict=on_tts_replacement_conflict,
        consumer_gone=consumer_gone,
    )
    return await consumer.run(stream_factory)


class _AgentStreamConsumer:
    """Stateful helper behind :func:`consume_agent_stream`.

    Splits the consumption loop into named phases — cancellation
    draining, text-delta buffering, tool-event forwarding, and final
    sentinel delivery — that all share the in-flight counters that used
    to live as closure locals.
    """

    def __init__(
        self,
        *,
        cancel_token: Any | None,
        tts_queue: asyncio.Queue[TTSInput | None],
        emit: Callable[[Any], Awaitable[None]],
        prepare_tts_payload: Callable[..., TTSInput],
        strip_md: bool,
        turn: TurnContext,
        first_tts_payload_ready: asyncio.Future[bool] | None,
        abort_event: asyncio.Event | None,
        is_active: Callable[[], bool] | None,
        on_tts_replacement_conflict: Callable[[], Awaitable[None]] | None,
        consumer_gone: Callable[[], bool] | None = None,
    ) -> None:
        self._cancel_token = cancel_token
        self._tts_queue = tts_queue
        self._emit = emit
        self._turn = turn
        self._first_tts_payload_ready = first_tts_payload_ready
        self._abort_event = abort_event
        self._is_active_callback = is_active
        self._on_tts_replacement_conflict = on_tts_replacement_conflict
        self._consumer_gone_callback = consumer_gone
        self._buffer = _SentenceStreamBuffer(
            tts_queue=tts_queue,
            prepare_tts_payload=prepare_tts_payload,
            strip_md=strip_md,
        )
        self.result = AgentStreamResult()
        self._text_stream = AgentTextStream()
        self._pending_tool_calls = 0
        self._done_received = False
        self._tts_suppressed_for_replacement = False

    async def run(self, stream_factory: Callable[[], AsyncIterator[Any]]) -> AgentStreamResult:
        stream: AsyncIterator[Any] | None = None
        try:
            stream = stream_factory()
            await self._consume(stream)
        except Exception as exc:
            self.result.error = exc
            logger.exception("Agent streaming error")
            await self._emit(Error(exception=exc, stage=ErrorStage.AGENT))
        finally:
            await self._close_stream(stream)
            await self._finish()
        return self.result

    # ── Phases ─────────────────────────────────────────────────────

    async def _consume(self, stream: AsyncIterator[Any]) -> None:
        async for event in stream:
            kind = getattr(event, "kind", None)
            if kind is None:
                continue
            if self._done_received:
                break
            if not self._is_active():
                break
            if self._cancel_token and self._cancel_token.is_cancelled:
                if not await self._consume_cancelled(event, kind):
                    break
                continue
            await self._consume_event(event, kind)
            if self._done_received:
                break

    async def _consume_cancelled(self, event: Any, kind: str) -> bool:
        """Drain in-flight tool calls after cancellation.

        Returns ``True`` to keep consuming (tool calls still pending) and
        ``False`` to stop.  Captures a trailing ``done`` payload either
        way so an interrupted stream still surfaces its partial result.
        """
        if not self.result.interrupted:
            self.result.interrupted = True
        if self._pending_tool_calls <= 0:
            if kind == "done":
                self._capture_done_payload(event)
            return False
        if kind == "tool_result":
            self._pending_tool_calls = max(0, self._pending_tool_calls - 1)
            await emit_tool_event(event, kind, emit=self._emit)
            return self._pending_tool_calls > 0
        if kind in ("tool_started", "tool_delta"):
            if kind == "tool_started":
                self._pending_tool_calls += 1
            await emit_tool_event(event, kind, emit=self._emit)
            return True
        if kind == "done":
            self._capture_done_payload(event)
            return False
        return True

    async def _consume_event(self, event: Any, kind: str) -> None:
        if kind in {"text_delta", "text_replace"}:
            await self._consume_text_update(event)
        elif kind == "tool_started":
            self._pending_tool_calls += 1
            await emit_tool_event(event, kind, emit=self._emit)
        elif kind in ("tool_delta", "tool_result"):
            if kind == "tool_result":
                self._pending_tool_calls = max(0, self._pending_tool_calls - 1)
            await emit_tool_event(event, kind, emit=self._emit)
        elif kind == "done":
            await self._consume_done(event)

    async def _consume_text_update(self, event: Any) -> None:
        update = self._text_stream.apply(event)
        if update is None:  # pragma: no cover - guarded by _consume_event
            return
        self.result.text = update.text
        if update.text == update.previous_text:
            return
        delta_event = AgentDelta(
            text=event.text,
            part_index=update.part_index,
            replacement=update.operation == "replace",
        )
        gate = self._first_tts_payload_ready
        if gate is None or gate.done():
            await self._consume_ungated_text_update(update, delta_event)
            return

        await self._consume_gated_text_update(update, delta_event, gate)

    async def _consume_ungated_text_update(
        self,
        update: AgentTextUpdate,
        delta_event: AgentDelta,
    ) -> None:
        await self._emit(delta_event)
        if not self._is_active():
            return
        if self._turn.first_agent_time is None:
            self._turn.first_agent_time = time.monotonic()
        await self._queue_text_update(update)

    async def _consume_gated_text_update(
        self,
        update: AgentTextUpdate,
        delta_event: AgentDelta,
        gate: asyncio.Future[bool],
    ) -> None:
        if self._turn.first_agent_time is None:
            self._turn.first_agent_time = delta_event.timestamp

        queued = False
        try:
            # Admit the first complete clause before dispatching observers so
            # the provider can overlap its TTFB with async AgentDelta handlers.
            # The TTS consumer holds public lifecycle/audio events behind the
            # gate until dispatch completes below.
            queued = await self._queue_text_update(update)
            await self._emit(delta_event)
        except BaseException:
            if queued and not gate.done():
                gate.set_result(False)
            raise
        else:
            if queued and not gate.done():
                gate.set_result(True)

    async def _queue_text_update(self, update: AgentTextUpdate) -> bool:
        if self._tts_suppressed_for_replacement:
            return False
        if update.operation == "append" and update.appended_text is not None:
            return await self._buffer.add_delta(update.appended_text)
        if not self._buffer.has_payloads:
            return await self._buffer.replace_pending(update.text)
        if update.appended_text is not None:
            # A full-part replacement that only extends the current response
            # is still safe after speech started; synthesize only its suffix.
            return await self._buffer.add_delta(update.appended_text)
        await self._suppress_tts_after_replacement()
        return False

    async def _suppress_tts_after_replacement(self) -> None:
        """Fail closed when a replacement crosses admitted speech.

        Audio already heard cannot be retracted. Clear queued/current playback
        and suppress the rest of this turn rather than replaying corrected
        text over the stale prefix or continuing stale pending speech.
        """
        if self._tts_suppressed_for_replacement:
            return
        self._tts_suppressed_for_replacement = True
        self._buffer.suppress()
        while not self._tts_queue.empty():
            self._tts_queue.get_nowait()
        logger.warning("Agent text replacement crossed the admitted TTS boundary; speech cut off")
        callback = self._on_tts_replacement_conflict
        if callback is not None:
            try:
                await callback()
            except Exception:
                logger.exception("Failed to cut off TTS after an agent text replacement")

    async def _consume_done(self, event: Any) -> None:
        if event.text:
            if not self.result.text:
                self._buffer.replace(event.text)
            self.result.text = event.text
            self._text_stream.replace_final(event.text)
        if getattr(event, "structured_output", None) is not None:
            self.result.structured_output = event.structured_output
        queued = await self._buffer.flush()
        self._resolve_first_tts_payload_gate(queued)
        self._done_received = True

    def _capture_done_payload(self, event: Any) -> None:
        if event.text:
            self.result.text = event.text
            self._text_stream.replace_final(event.text)
        if getattr(event, "structured_output", None) is not None:
            self.result.structured_output = event.structured_output

    # ── Teardown ───────────────────────────────────────────────────

    @staticmethod
    async def _close_stream(stream: AsyncIterator[Any] | None) -> None:
        # Defensively close the agent stream so a generator abandoned mid-
        # iteration (e.g. on barge-in/cancellation) is finalized promptly
        # rather than waiting for GC. Bridges already close their own upstream
        # connections via async with/finally on cancel; this is hygiene that
        # tightens the race window where the bridge frame is left suspended.
        if stream is None:
            return
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()

    async def _finish(self) -> None:
        stream_succeeded = (
            self.result.error is None
            and (not self._cancel_token or not self._cancel_token.is_cancelled)
            and not (self._abort_event and self._abort_event.is_set())
            and self._is_active()
        )
        if stream_succeeded:
            queued = await self._buffer.flush()
            self._resolve_first_tts_payload_gate(queued)
        else:
            gate = self._first_tts_payload_ready
            if gate is not None and not gate.done():
                gate.set_result(False)
        # Sentinel to stop the TTS task.
        #
        # On a clean completion the consumer is still actively draining the
        # bounded queue, so we must guarantee delivery of the sentinel: a
        # non-blocking put could drop it while the consumer's final ``get()``
        # blocks forever waiting for the stop signal.  Use a blocking ``put``
        # here — backpressure resolves as the consumer drains.
        #
        # On cancellation / error the TTS task *may* have been cancelled
        # alongside this producer (barge-in), leaving no consumer to make
        # room.  A blocking put on a full queue would then hang in this
        # finally block, so that case falls back to a non-blocking put and
        # swallows ``QueueFull`` — the consumer is gone, so the sentinel is
        # moot.
        #
        # But the agent-failure/timeout path cancels only this producer: the
        # consumer is still alive and draining.  Dropping the sentinel there
        # left it blocked on ``queue.get()`` forever — no BotStoppedSpeaking,
        # no IDLE transition, and the turn never finalized on a quiet line
        # (gh 1063).  So the fallback is used only when the consumer really
        # is gone; otherwise the sentinel is delivered with the same blocking
        # put the success branch uses, and backpressure resolves as the
        # consumer drains.
        if stream_succeeded or not self._consumer_is_gone():
            await self._tts_queue.put(None)
        else:
            try:
                self._tts_queue.put_nowait(None)
            except asyncio.QueueFull:
                logger.debug("tts_queue full; skipping stop sentinel (consumer already stopped)")

    def _resolve_first_tts_payload_gate(self, queued: bool) -> None:
        gate = self._first_tts_payload_ready
        if queued and gate is not None and not gate.done():
            gate.set_result(True)

    def _is_active(self) -> bool:
        callback = self._is_active_callback
        return callback is None or callback()

    def _consumer_is_gone(self) -> bool:
        """Whether the TTS consumer has stopped, or is being torn down.

        Without a callback the caller has not published a consumer, so the
        conservative answer is "gone": that keeps the historical
        non-blocking behaviour for any caller that has not opted in.
        """
        callback = self._consumer_gone_callback
        return callback is None or callback()
