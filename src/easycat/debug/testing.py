"""Bundle-driven pytest helpers for voice-agent regression tests.

These helpers live in the library (LiveKit 1.0 pattern — ship the
testing surface in core, not a sidecar package) so authors can promote
a production failure into a regression test in the same PR that fixes
it.  The API is intentionally small: load a bundle, assert something
about its records.  Per the plan in
``plan/peripheral-eval-and-debugger-ui.md``, deeper helpers
(``assert_llm_judge``, Simulator + Judge personas, per-stage latency
budgets) follow once the `LatencyBudget` + LLM-judge primitives land.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from easycat.debug.bundle import RunBundle

__all__ = [
    "load_bundle",
    "iter_records",
    "turn_records",
    "find_record",
    "assert_exact_match",
    "assert_regex",
    "assert_turn_completed",
    "assert_no_error",
    "assert_tool_called",
]


# ── Loading ──────────────────────────────────────────────────────


def load_bundle(path: str | Path) -> RunBundle:
    """Load a :class:`RunBundle` from a ``.zip`` path.

    Works equally well with bundles captured via
    ``session.export_debug_bundle(...)`` and with the fixture bundles
    checked into ``tests/fixtures/``.
    """
    return RunBundle.load(Path(path))


# ── Iteration helpers ────────────────────────────────────────────


def iter_records(bundle: RunBundle, *, name: str | None = None) -> Iterable[dict[str, Any]]:
    """Iterate journal records, optionally filtering by event name.

    Names match the :data:`JournalRecord.name` field emitted at record
    creation.  The session's journal sink writes snake_case names
    (``"turn_started"``, ``"stt_final"``, ``"agent_final"``,
    ``"tool_call_started"``) — pass those here.
    """
    for record in bundle.records():
        if name is None or record.get("name") == name:
            yield record


def turn_records(bundle: RunBundle, turn_id: str) -> list[dict[str, Any]]:
    """Return every record that carries the given ``turn_id``."""
    return [r for r in bundle.records() if r.get("turn_id") == turn_id]


def find_record(bundle: RunBundle, *, name: str) -> dict[str, Any] | None:
    """Return the first record whose ``name`` matches, or ``None``."""
    for record in iter_records(bundle, name=name):
        return record
    return None


# ── Assertion helpers ────────────────────────────────────────────
#
# Each helper raises ``AssertionError`` with a pytest-friendly message
# so failing tests surface the offending record payload, not just a
# boolean.  They stay deliberately independent of pytest so callers
# outside a test context (e.g. a CLI `replay --fail-on-regression`
# integration) can use them too.


def _assistant_text(record: dict[str, Any]) -> str:
    """Best-effort extraction of the assistant-reply text from a record.

    Prefers ``data.text`` (what the session journal sink writes for
    ``agent_final`` / ``stt_final``) and falls back to a top-level
    ``text`` key for bundle variants that flatten it.
    """
    data = record.get("data") or {}
    if isinstance(data, dict) and "text" in data:
        return str(data["text"])
    if "text" in record:
        return str(record["text"])
    return ""


def assert_exact_match(
    bundle: RunBundle,
    *,
    expected: str,
    event: str = "agent_final",
) -> None:
    """Assert an event's text field equals ``expected`` exactly.

    Matches Vapi Evals' "exact match" method: deterministic content
    checks are the baseline for non-semantic regressions.  Defaults to
    ``agent_final`` — the name the session journal sink emits for
    :class:`~easycat.events.AgentFinal`.
    """
    record = find_record(bundle, name=event)
    if record is None:
        raise AssertionError(f"no {event!r} record in bundle")
    actual = _assistant_text(record)
    if actual != expected:
        raise AssertionError(
            f"{event} text mismatch\n  expected: {expected!r}\n  actual:   {actual!r}"
        )


def assert_regex(
    bundle: RunBundle,
    *,
    pattern: str,
    event: str = "agent_final",
    flags: int = 0,
) -> None:
    """Assert an event's text field matches a regex pattern.

    Complement to :func:`assert_exact_match` for flexible checks like
    "mentions the user's name" or "ends with a question mark".
    """
    compiled = re.compile(pattern, flags)
    record = find_record(bundle, name=event)
    if record is None:
        raise AssertionError(f"no {event!r} record in bundle")
    actual = _assistant_text(record)
    if not compiled.search(actual):
        raise AssertionError(f"{event} text did not match /{pattern}/\n  actual: {actual!r}")


def assert_turn_completed(bundle: RunBundle, turn_id: str) -> None:
    """Assert the given turn emitted both ``turn_started`` and ``turn_ended``.

    Catches pipeline hangs where a turn starts but never resolves —
    the single most common "sessions feel broken" symptom.
    """
    records = turn_records(bundle, turn_id)
    names = {r.get("name") for r in records}
    if "turn_started" not in names:
        raise AssertionError(f"turn {turn_id!r} has no turn_started record")
    if "turn_ended" not in names:
        raise AssertionError(f"turn {turn_id!r} never completed (no turn_ended record)")


def assert_no_error(bundle: RunBundle, *, turn_id: str | None = None) -> None:
    """Assert no journal record carries an ``error`` payload.

    Scopes to a single turn when ``turn_id`` is provided so fixture
    bundles that deliberately include a neighbouring failed turn still
    exercise the happy-path assertion.
    """
    iterator = turn_records(bundle, turn_id) if turn_id else bundle.records()
    for record in iterator:
        err = record.get("error")
        if err:
            scope = f"turn {turn_id!r}" if turn_id else "bundle"
            raise AssertionError(
                f"{scope} contains an error record: "
                f"{err.get('type', '?')}: {err.get('message', '')} "
                f"(record={record.get('name')!r} seq={record.get('sequence')})"
            )


def assert_tool_called(
    bundle: RunBundle,
    *,
    tool_name: str,
    event: str = "tool_call_started",
) -> None:
    """Assert the agent invoked a specific tool at least once.

    Reads the ``tool_name`` field that the session journal sink writes
    from :class:`~easycat.events.ToolCallStarted` (see
    ``_JOURNAL_ATTRS`` in ``session/_session.py``).
    """
    for record in iter_records(bundle, name=event):
        data = record.get("data") or {}
        if isinstance(data, dict) and data.get("tool_name") == tool_name:
            return
    raise AssertionError(
        f"tool {tool_name!r} was never invoked (no {event} record with tool_name={tool_name!r})"
    )
