"""Journal record filtering, full-text search, and pagination helpers.

Pure, aiohttp-free projections split out of :mod:`easycat.debugger.server`
(QS3): record filtering/pagination for ``/api/records``, the bounded
grep-style regex compiler with its catastrophic-backtracking analyzer, the
full-text search scan shared with ``easycat journal grep``, and the transcript
projection.

JournalRecord → JSON-dict coercion lives in the canonical generic dataclass
walk in :mod:`easycat.debug._serialize`.

"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from re import _parser as re_parser  # type: ignore[attr-defined]
from typing import Any

from easycat.debug._turn_timeline import extract_turn_transcripts as _extract_turn_transcripts

# Hard cap on records scanned by full-text search (``/api/records?q=`` and
# ``easycat journal grep``) so a pathological journal can't pin the event
# loop / CLI on a single request. Past this many records the scan stops and
# callers see ``scan_truncated`` so the cap is visible rather than silent.
_SEARCH_SCAN_LIMIT = 50000

# Upper bound on the search query string. The debugger binds loopback-only and
# the query comes from the developer searching their own journal (no privilege
# boundary), but bounding the length is cheap defense-in-depth that keeps a
# pathological user-supplied regex from compiling into a huge automaton.
_SEARCH_MAX_QUERY_LEN = 500

# Regex searches are intentionally a developer convenience, not an arbitrary
# pattern execution surface. Reject constructs that commonly trigger
# catastrophic backtracking in Python's backtracking ``re`` engine before the
# pattern is run against journal-controlled text.
_UNSAFE_REGEX_MESSAGE = "unsafe regex"


def _filter_records(
    records: list[dict[str, Any]],
    *,
    stage: str | None,
    turn_id: str | None,
    name: str | Iterable[str] | None,
    from_seq: int | None,
    to_seq: int | None,
    errors_only: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Filter records.  Slicing happens here for callers that want a
    single combined operation; pagination on the HTTP API goes through
    :func:`_filter_and_paginate` so the response can carry both the
    page slice and the full match count.

    ``name`` may be a single string (exact match) or an iterable of
    strings (membership match).  The HTTP handler surfaces the latter
    via repeated ``name=`` query params so the Live view can fetch only
    the event names it renders without being capped by ``limit``.
    """
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be > 0")
    name_set: frozenset[str] | None
    if name is None:
        name_set = None
    elif isinstance(name, str):
        name_set = frozenset({name})
    else:
        collected = frozenset(name)
        name_set = collected or None
    out = []
    for r in records:
        seq = r.get("sequence")
        if isinstance(seq, bool) or not isinstance(seq, int):
            continue
        if from_seq is not None and seq < from_seq:
            continue
        if to_seq is not None and seq > to_seq:
            continue
        if turn_id is not None and r.get("turn_id") != turn_id:
            continue
        if name_set is not None and r.get("name") not in name_set:
            continue
        if stage is not None:
            data = r.get("data") or {}
            if not isinstance(data, dict):
                continue
            if data.get("stage") != stage and data.get("observed_stage") != stage:
                continue
        if errors_only and not r.get("error"):
            continue
        out.append(r)
    if offset:
        out = out[offset:]
    if limit is not None:
        out = out[:limit]
    return out


def _filter_and_paginate(
    records: list[dict[str, Any]],
    *,
    stage: str | None,
    turn_id: str | None,
    name: str | Iterable[str] | None,
    from_seq: int | None,
    to_seq: int | None,
    errors_only: bool,
    limit: int | None,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    """Return ``(page, total)`` so the UI can render "X of N".

    ``limit`` and ``offset`` are validated by the HTTP boundary before this
    helper runs. The previous endpoint returned ``page_size`` as ``total``,
    which made it impossible to render a real pager and confused tooling.
    """
    full = _filter_records(
        records,
        stage=stage,
        turn_id=turn_id,
        name=name,
        from_seq=from_seq,
        to_seq=to_seq,
        errors_only=errors_only,
        limit=None,
        offset=0,
    )
    total = len(full)
    if offset:
        full = full[offset:]
    if limit is not None:
        full = full[:limit]
    return full, total


_REGEX_REPEAT_OPS = frozenset({"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"})
_REGEX_FIXED_WIDTH_OPS = frozenset({"LITERAL", "NOT_LITERAL", "IN", "ANY"})
# Category pairs that can never match the same character, so two adjacent
# repeats over them have exactly one way to split any input.
_REGEX_DISJOINT_CATEGORIES = frozenset(
    {
        frozenset({"CATEGORY_SPACE", "CATEGORY_DIGIT"}),
        frozenset({"CATEGORY_SPACE", "CATEGORY_WORD"}),
    }
)


class _RepeatScope:
    """Repeat bookkeeping for one sibling sequence of regex tokens."""

    __slots__ = ("consumed", "leading_open", "optional_repeats", "pending_required")

    def __init__(self) -> None:
        self.optional_repeats = 0
        # Category of the last required variable-width repeat that no
        # mandatory token has separated from what follows; ``None`` when there
        # is none and ``""`` when its character set is not a known category.
        self.pending_required: str | None = None
        # Whether a token that always consumes input has been seen, and
        # whether a required variable-width repeat came before any such token
        # (so it can trade input with whatever precedes this sequence).
        self.consumed = False
        self.leading_open = False

    def mandatory(self) -> None:
        self.pending_required = None
        self.consumed = True


def _regex_min_width(tokens: Any) -> int:
    try:
        return int(tokens.getwidth()[0])
    except (AttributeError, TypeError, ValueError):
        return 0


def _regex_repeat_category(child: Any) -> str:
    """Return the lone character category a repeat's body matches, else ``""``."""
    if len(child) != 1:
        return ""
    op, arg = child[0]
    if str(op) != "IN" or len(arg) != 1 or str(arg[0][0]) != "CATEGORY":
        return ""
    return str(arg[0][1])


def _regex_repeat_is_unsafe(
    min_repeat: int, max_repeat: int, child: Any, scope: _RepeatScope
) -> bool:
    """Record one top-level repeat in *scope*; true when it makes the sequence unsafe."""
    if min_repeat == max_repeat:
        # An empty body (``(?:){1}``) consumes nothing and separates nothing.
        if min_repeat and _regex_min_width(child) >= 1:
            scope.mandatory()
        return False
    if min_repeat == 0:
        scope.optional_repeats += 1
        return scope.optional_repeats > 1
    category = _regex_repeat_category(child)
    previous = scope.pending_required
    scope.pending_required = category
    if not scope.consumed:
        scope.leading_open = True
    scope.consumed = True
    if previous is None:
        return False
    return frozenset({previous, category}) not in _REGEX_DISJOINT_CATEGORIES


def _regex_branch_is_unsafe(branches: Any, scope: _RepeatScope, *, inside_repeat: bool) -> bool:
    """Check each alternative, then fold the alternation into *scope*.

    An alternative that starts or ends with a required variable-width repeat
    (``(?:a+|b+)``) leaves that repeat adjacent to the alternation's
    neighbours, so the alternation only separates repeats on the sides where
    every alternative has a mandatory token.
    """
    leading = trailing = False
    for branch in branches:
        sub = _RepeatScope()
        if _regex_tree_has_unsafe_backtracking(branch, inside_repeat=inside_repeat, scope=sub):
            return True
        leading = leading or sub.leading_open
        trailing = trailing or sub.pending_required is not None
    if leading:
        if scope.pending_required is not None:
            return True
        if not scope.consumed:
            scope.leading_open = True
    if all(_regex_min_width(branch) >= 1 for branch in branches):
        scope.mandatory()
    if trailing:
        scope.pending_required = ""
    return False


def _regex_tree_has_unsafe_backtracking(
    tokens: Any,
    *,
    inside_repeat: bool = False,
    repeated_token: bool = False,
    scope: _RepeatScope | None = None,
) -> bool:
    """Return true when parsed regex tokens can cause exponential backtracking.

    The journal search surface only needs small grep-style patterns. Nested
    repeats, quantified alternations, backreferences, and assertions can all
    make Python's ``re`` engine spend unbounded CPU on a single haystack, so
    reject them instead of trying to sandbox individual ``search()`` calls.

    A single repeat is fine, and so are repeats separated by a mandatory
    token (``\\d+\\.\\d+``, ``https?://\\S+``). Two shapes in one sibling
    sequence are rejected even though no repeat is nested inside another:

    * two or more *optional* (``min == 0``) repeats -- e.g. ``a?a?...aaa`` or
      its grouped twin ``(a?)(a?)...`` -- which let the engine explore every
      "skip vs. match" subset;
    * *required* variable-width repeats (``min >= 1``, ``min != max``) that
      follow one another with no mandatory token between them -- e.g.
      ``a+a+a+!``, ``a{1,2}a{1,2}...`` or ``(a+)(a+)`` -- which let the engine
      try every way of splitting the input between them. Adjacent repeats
      over provably disjoint categories (``\\w+\\s+\\w+``) have a single split
      and stay allowed.

    ``scope`` carries that bookkeeping; ``SUBPATTERN`` reuses the parent's
    scope (transparent groups stay in the same sequence) while repeat children
    start a fresh one and each alternative of a branch is checked in its own
    scope and then folded back into the parent's.
    """
    if scope is None:
        scope = _RepeatScope()
    for op, arg in tokens:
        op_name = str(op)
        if op_name in {"GROUPREF", "GROUPREF_EXISTS", "ASSERT", "ASSERT_NOT"}:
            return True
        if op_name in _REGEX_FIXED_WIDTH_OPS:
            scope.mandatory()
        elif op_name == "BRANCH":
            _none, branches = arg
            if repeated_token:
                return True
            if _regex_branch_is_unsafe(branches, scope, inside_repeat=inside_repeat):
                return True
        elif op_name in _REGEX_REPEAT_OPS:
            min_repeat, max_repeat, child = arg
            if inside_repeat or _regex_repeat_is_unsafe(min_repeat, max_repeat, child, scope):
                return True
            if _regex_tree_has_unsafe_backtracking(child, inside_repeat=True, repeated_token=True):
                return True
        elif op_name == "SUBPATTERN" and _regex_tree_has_unsafe_backtracking(
            arg[-1],
            inside_repeat=inside_repeat,
            repeated_token=repeated_token,
            scope=scope,
        ):
            return True
    return False


def _compile_search_regex(query: str) -> re.Pattern[str]:
    """Compile a bounded, grep-style regex for journal search."""
    try:
        parsed = re_parser.parse(query, 0)
        if _regex_tree_has_unsafe_backtracking(parsed.data):
            raise ValueError(_UNSAFE_REGEX_MESSAGE)
        return re.compile(query, re.IGNORECASE)
    except ValueError:
        raise
    except re.error as exc:
        raise ValueError("invalid regex") from exc


def _record_searchable_text(record: dict[str, Any]) -> str:
    """Build the haystack a full-text query is matched against.

    Combines the serialized ``data`` payload, the error type/message/notes,
    and the indexed ``name``/``turn_id`` so a query like ``timeout`` or a
    phone number embedded in a tool argument is found regardless of where it
    lives in the record.
    """
    parts: list[str] = []
    name = record.get("name")
    if name:
        parts.append(str(name))
    turn_id = record.get("turn_id")
    if turn_id:
        parts.append(str(turn_id))
    data = record.get("data")
    if data is not None:
        try:
            parts.append(json.dumps(data, default=str))
        except (TypeError, ValueError):
            parts.append(str(data))
    error = record.get("error")
    if isinstance(error, dict):
        for key in ("type", "message", "traceback", "notes"):
            value = error.get(key)
            if value:
                parts.append(str(value))
    return "\n".join(parts)


def _record_match_fields(record: dict[str, Any], needle: Any, *, is_regex: bool) -> list[str]:
    """Return the named fields of *record* whose text matches *needle*.

    *needle* is a lowercased substring (when ``is_regex`` is false) or a
    compiled :class:`re.Pattern` (when true).  Used to render match badges in
    the SPA and to scope redaction in the CLI grep output.
    """

    def _hit(text: str) -> bool:
        if not text:
            return False
        if is_regex:
            return needle.search(text) is not None
        return needle in text.lower()

    fields: list[str] = []
    if _hit(str(record.get("name") or "")):
        fields.append("name")
    if _hit(str(record.get("turn_id") or "")):
        fields.append("turn_id")
    data = record.get("data")
    if data is not None:
        try:
            data_text = json.dumps(data, default=str)
        except (TypeError, ValueError):
            data_text = str(data)
        if _hit(data_text):
            fields.append("data")
    error = record.get("error")
    if isinstance(error, dict) and any(
        _hit(str(error.get(key) or "")) for key in ("type", "message", "traceback", "notes")
    ):
        fields.append("error")
    return fields


def _search_records(
    records: list[dict[str, Any]],
    *,
    query: str,
    use_regex: bool = False,
    errors_only: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    """Full-text filter *records* against *query*, returning ``(matches, truncated)``.

    The haystack per record is :func:`_record_searchable_text` (serialized
    ``data`` + error fields + ``name``/``turn_id``).  Matching is a
    case-insensitive substring by default; when *use_regex* is true *query* is
    compiled with :data:`re.IGNORECASE` and a bad pattern raises
    :class:`ValueError` (mapped to a 400 / CLI error by callers).

    Matched records are returned as **shallow copies** carrying a
    ``_match_fields`` list — the cached ``source.records()`` dicts are never
    mutated.  The scan stops after :data:`_SEARCH_SCAN_LIMIT` records and the
    second tuple element reports whether that cap was hit.
    """
    if len(query) > _SEARCH_MAX_QUERY_LEN:
        raise ValueError("search query too long")
    needle: Any
    if use_regex:
        needle = _compile_search_regex(query)
    else:
        needle = query.lower()
        if not needle:
            # An empty query matches nothing rather than everything — an empty
            # search box should not silently return the entire journal.
            return [], False

    matches: list[dict[str, Any]] = []
    truncated = False
    for index, record in enumerate(records):
        if index >= _SEARCH_SCAN_LIMIT:
            truncated = True
            break
        if errors_only and not record.get("error"):
            continue
        haystack = _record_searchable_text(record)
        if use_regex:
            if needle.search(haystack) is None:
                continue
        elif needle not in haystack.lower():
            continue
        fields = _record_match_fields(record, needle, is_regex=use_regex)
        # Copy before annotating so the cached source records stay pristine.
        copied = dict(record)
        copied["_match_fields"] = fields
        matches.append(copied)
    return matches, truncated


def _build_transcript(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pull user transcripts and agent responses out of the journal.

    The UI renders this alongside the waterfall so a developer can read
    the conversation without opening every record.  The pure projection
    lives in :func:`easycat.debug._turn_timeline.extract_turn_transcripts`
    so the two-source ``easycat diff`` shares one implementation; this thin
    wrapper keeps the historical name the SPA routes call.
    """
    return _extract_turn_transcripts(records)
