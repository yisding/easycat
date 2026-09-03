"""``easycat journal follow`` / ``easycat tail`` — live-tail a SQLite journal."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import typer
from rich.markup import escape

from easycat.cli._errors import cli_command
from easycat.cli._output import emit_command_error, stderr_console, stdout_console
from easycat.cli.debug._common import _record_detail, _record_stage
from easycat.debug._turn_timeline import safe_turn_id
from easycat.validation.redaction import REDACTED_TRANSCRIPT, redact_value

# Record names whose ``data['audio_bytes']`` we render as a compact audio
# bar so a tail watcher can eyeball codec/frame throughput without opening
# the debugger UI.
_FOLLOW_AUDIO_NAMES = frozenset(("tts_frame", "stt_audio_in"))
# The TTS first-byte names mirror ``debug/_turn_timeline._TTS_FIRST`` — the
# first such record per turn closes the critical-path milestone, so the
# follow line flags it as a per-turn milestone landmark.
_FOLLOW_TTS_FIRST = frozenset(("tts_frame", "tts_audio"))


def _follow_audio_bar(record: Mapping[str, Any]) -> str:
    """Render a tiny throughput bar for an audio record, or ``""``.

    Reads ``data['audio_bytes']`` (the per-frame byte count the audio
    stages journal) and maps it onto a short block-glyph bar so a long
    tail stays scannable.  Never raises on malformed data.
    """
    data = record.get("data")
    if not isinstance(data, Mapping):
        return ""
    audio_bytes = data.get("audio_bytes")
    if not isinstance(audio_bytes, int) or audio_bytes <= 0:
        return ""
    # ~1 block per kilobyte, capped so a large frame can't blow out the line.
    blocks = min(20, max(1, audio_bytes // 1024))
    return f"audio={audio_bytes}B {'▮' * blocks}"


def _format_follow_line(record: Mapping[str, Any]) -> str:
    """Render one live-tail line for *record* — pure and table-testable.

    Shape: ``[seq] turn=.. name=.. stage=.. detail``.  Two special cases:

    - A synthetic :class:`BufferOverflow` gap notice
      (``data['dropped_from'] == 'follow_gap'``) renders as a one-line
      ``-- gap: N records dropped --`` marker so a non-contiguous sequence
      stream is obvious in the tail.
    - The first TTS byte of a turn and audio frames append a milestone or
      throughput annotation; both reuse ``_record_stage`` / ``_record_detail``
      so the CLI and the bundle timeline agree on field projection.
    """
    data = record.get("data")
    if isinstance(data, Mapping) and data.get("dropped_from") == "follow_gap":
        gap = data.get("gap")
        count = gap if isinstance(gap, int) and gap > 0 else "?"
        return f"-- gap: {count} records dropped --"

    seq = record.get("sequence")
    seq_text = str(seq) if isinstance(seq, int) else "-"
    turn_id = safe_turn_id(record.get("turn_id")) or "-"
    name = str(record.get("name") or "-")
    stage = _record_stage(record) or "-"

    parts = [f"[{seq_text}]", f"turn={turn_id}", f"name={name}", f"stage={stage}"]
    detail = _record_detail(record)
    if detail:
        parts.append(detail)
    # The first TTS byte of a turn closes the critical-path milestone; callers
    # that have already flagged it for a turn pass ``_no_milestone`` to drop
    # the landmark on later frames of the same turn.
    if name in _FOLLOW_TTS_FIRST and not record.get("_no_milestone"):
        parts.append("milestone=tts_first_byte")
    audio_bar = _follow_audio_bar(record)
    if audio_bar:
        parts.append(audio_bar)
    return " ".join(parts)


async def _stream_follow(
    view: Any,
    *,
    from_sequence: int | None,
    errors_only: bool,
    turn_id: str | None,
    json_output: bool,
    cursor: list[int | None] | None = None,
) -> None:
    """Drive a :meth:`JournalView.follow` loop, printing one line per record.

    Persistent SQLite journals are written by a separate live session, so
    transient ``FileNotFoundError`` / ``sqlite3.OperationalError`` (a half-open
    file, a table not yet created) are swallowed and retried on the next poll
    rather than aborting the tail.  Per-turn milestone names ride the formatted
    line so a tail watcher sees the critical-path landmarks inline.
    """
    seen_tts_first: set[str] = set()
    async for record in view.follow(from_sequence=from_sequence, poll_interval=0.25):
        record_dict = _record_to_follow_dict(record)
        # Advance the resume cursor for EVERY yielded record, before any
        # ``errors_only``/``turn_id`` filtering below drops it: a post-outage
        # retry must resume past filtered-out records too, or they are re-read.
        seq = record_dict.get("sequence")
        if cursor is not None and isinstance(seq, int):
            cursor[0] = seq if cursor[0] is None else max(cursor[0], seq)
        # ``errors_only`` filters to records that carry an error, but always
        # let the synthetic gap notice through so a dropped-record warning is
        # never hidden by the filter.
        is_gap = (
            isinstance(record_dict.get("data"), Mapping)
            and record_dict["data"].get("dropped_from") == "follow_gap"
        )
        if errors_only and not record_dict.get("error") and not is_gap:
            continue
        rec_turn = safe_turn_id(record_dict.get("turn_id"))
        if turn_id is not None and not is_gap and rec_turn != turn_id:
            continue

        if json_output:
            # Newline-delimited JSON, one record per line (NOT a single
            # envelope) so a consumer can ``read`` the stream incrementally.
            # Write straight to the file handle, bypassing Rich: Rich soft-wraps
            # at terminal width, which would split long records across lines and
            # mangle the NDJSON when consumers pipe it into ``jq`` or ``read``.
            line = json.dumps(_redact_follow_record(record_dict), sort_keys=False)
            stdout_console.file.write(line + "\n")
            stdout_console.file.flush()
            continue

        # Only the FIRST TTS byte of a turn is the milestone landmark; later
        # frames of the same turn keep the throughput bar but drop the tag.
        name = str(record_dict.get("name") or "")
        if name in _FOLLOW_TTS_FIRST and rec_turn is not None:
            if rec_turn in seen_tts_first:
                record_dict = {**record_dict, "_no_milestone": True}
            else:
                seen_tts_first.add(rec_turn)
        stdout_console.print(escape(_format_follow_line(record_dict)))


async def _follow_with_retry(
    view: Any,
    *,
    from_sequence: int | None,
    errors_only: bool,
    turn_id: str | None,
    json_output: bool,
) -> None:
    """Drive :func:`_stream_follow`, resuming past records already streamed.

    Persistent SQLite journals are written by a separate live session, so a
    mid-stream ``FileNotFoundError`` / ``sqlite3.OperationalError`` (the writer
    has not created the table yet, or the file is mid-rotation) is retried after
    a short back-off.  The retry MUST resume from ``last_yielded + 1`` rather
    than the original ``from_sequence``: keeping the original argument would
    re-emit every already-printed record (``--from-sequence 0``) or recompute
    ``latest_sequence + 1`` at retry time and silently skip records written
    during the outage.  A shared ``cursor`` holder carries the highest yielded
    sequence back out even when the generator unwinds via a propagating
    exception rather than a normal return.
    """
    cursor: list[int | None] = [None]
    resume = from_sequence
    # Capture initial cursor so a retry that fails before first yield still resumes correctly (gh 1045).  # noqa: E501
    if resume is None:
        try:
            cursor[0] = view._journal.latest_sequence  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001, S110
            pass
    while True:
        try:
            await _stream_follow(
                view,
                from_sequence=resume,
                errors_only=errors_only,
                turn_id=turn_id,
                json_output=json_output,
                cursor=cursor,
            )
            return
        except (FileNotFoundError, sqlite3.OperationalError):
            if cursor[0] is not None:
                resume = cursor[0] + 1
            elif resume is None:
                # First attempt never reached view.follow's internal cursor computation; re-capture
                try:
                    # Use initially captured value if available, else recompute (gh 1045).  # noqa: E501
                    if cursor[0] is None:
                        cursor[0] = view._journal.latest_sequence  # type: ignore[attr-defined]
                        resume = (cursor[0] or 0) + 1  # type: ignore[operator]
                except Exception:  # noqa: BLE001, S110
                    pass
            await asyncio.sleep(0.25)
        except sqlite3.DatabaseError as exc:
            from easycat.errors import EasyCatError

            raise EasyCatError("EASYCAT_E104", "Not an easycat journal", details=str(exc)) from exc


def _record_to_follow_dict(record: Any) -> dict[str, Any]:
    """Project a ``JournalRecord`` (or dict) into the follow-line dict shape."""
    if isinstance(record, dict):
        return record
    out: dict[str, Any] = {}
    for attr in ("sequence", "session_id", "name", "turn_id", "data", "input_ref", "output_ref"):
        out[attr] = getattr(record, attr, None)
    kind = getattr(record, "kind", None)
    out["kind"] = getattr(kind, "value", kind)
    error = getattr(record, "error", None)
    if error is not None:
        out["error"] = {
            "type": getattr(error, "type", None),
            "message": getattr(error, "message", None),
        }
    timing = getattr(record, "timing", None)
    if timing is not None:
        out["timing"] = {k: getattr(timing, k, None) for k in ("wall_ns", "mono_ns", "cpu_ns")}
    return out


# Free-form STT/agent text that ``SessionJournalSink`` stores under generic
# ``data`` keys: final/partial transcript and model output land under
# ``data.text`` and streamed tokens under ``data.delta``.  Neither key is in
# ``redact_value``'s field-name allowlist, so they would only get pattern-based
# redaction and otherwise stream verbatim utterances (e.g. medical or account
# details).  Replace them wholesale with the shared transcript placeholder.
_FOLLOW_FREE_TEXT_KEYS = ("text", "delta")


def _redact_follow_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return a redacted copy of a follow record before JSON streaming.

    Human follow output already renders a narrow, redacted summary.  JSON mode
    intentionally preserves the same record shape for incremental consumers,
    but must pass every projected field through the shared redaction policy
    before writing newline-delimited records to stdout.  Free-form transcript
    and model text under ``data.text`` / ``data.delta`` is stripped explicitly
    because those generic keys fall outside the shared field-name allowlist.
    """
    redacted = cast(dict[str, Any], redact_value(dict(record)))
    data = redacted.get("data")
    if isinstance(data, dict):
        for key in _FOLLOW_FREE_TEXT_KEYS:
            if key in data:
                data[key] = REDACTED_TRANSCRIPT
    return redacted


@cli_command
def follow_journal(
    bundle_path: Path = typer.Argument(
        ...,
        help="Path to a live or crash-dump ``.sqlite`` journal to tail.",
    ),
    from_sequence: int | None = typer.Option(
        None,
        "--from-sequence",
        help="Start the tail at this sequence (default: only future records; 0 replays history).",
    ),
    errors_only: bool = typer.Option(
        False,
        "--errors",
        help="Only print records that carry an error.",
    ),
    turn: str | None = typer.Option(
        None,
        "--turn",
        help="Restrict the tail to a single turn id.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Stream newline-delimited JSON, one record per line (not a single envelope).",
    ),
) -> None:
    """Live-tail a SQLite journal as it grows, redacting every printed line.

    Wraps a :class:`ReadonlySqliteJournal` in a :class:`JournalView` and
    drives :meth:`JournalView.follow`, so a tail keeps up with a session
    writing the same ``.sqlite`` file.  Exported ZIP bundles are immutable
    and cannot grow, so they exit with guidance to use ``bundles show``.
    """
    from easycat.runtime import JournalView
    from easycat.runtime.journal_views import ReadonlySqliteJournal

    if bundle_path.suffix != ".sqlite":
        emit_command_error(
            "journal_follow",
            "Live tail only works on a .sqlite journal; ZIP bundles are immutable. "
            "Use 'easycat bundles show <path>' or 'easycat journal grep <path>' instead.",
            json_output=json_output,
            exit_code=2,
            path=str(bundle_path),
        )
        raise typer.Exit(2)
    if not bundle_path.exists():
        emit_command_error(
            "journal_follow",
            f"Journal not found: {bundle_path}",
            json_output=json_output,
            exit_code=5,
            path=str(bundle_path),
        )
        raise typer.Exit(5)

    view = JournalView(ReadonlySqliteJournal(bundle_path))
    if not json_output:
        stderr_console.print(
            f"[bold]Tailing[/] [cyan]{escape(str(bundle_path))}[/] — Ctrl-C to stop."
        )

    async def _runner() -> None:
        await _follow_with_retry(
            view,
            from_sequence=from_sequence,
            errors_only=errors_only,
            turn_id=turn,
            json_output=json_output,
        )

    # A bare Ctrl-C propagates out of ``asyncio.run`` as ``KeyboardInterrupt``;
    # the top-level ``main()`` handler maps it to a clean exit code 130.
    asyncio.run(_runner())
