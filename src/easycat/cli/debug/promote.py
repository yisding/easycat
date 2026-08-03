"""``easycat journal promote`` — promote one turn into a regression bundle."""

from __future__ import annotations

import keyword
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import typer

from easycat.cli._errors import cli_command
from easycat.cli._output import (
    emit_command_error,
    emit_json,
    json_envelope,
    stdout_console,
    success,
)
from easycat.cli.debug._common import _load_bundle_or_journal
from easycat.debug._turn_timeline import safe_turn_id
from easycat.debug.bundle import BundleError, BundleValidationError, RunBundle
from easycat.debug.export import slice_bundle_by_turn
from easycat.runtime.replay import (
    ReplayError,
    ReplayFidelity,
    ReplaySideEffectBlocked,
    ReplaySpec,
    ToolReplayPolicy,
)
from easycat.validation.redaction import contains_unredacted_sensitive_text, redact_text


def _replay_signature(bundle: RunBundle) -> tuple[tuple[int, str, str | None], ...]:
    """Replay *bundle* at ARTIFACT/DENY/fast and return a determinism key.

    The key is the ``(sequence, name, output_ref)`` tuple of every frame —
    stable across runs of a deterministic turn, so two equal keys prove the
    slice replays the same way twice.  Tool side effects are denied and
    timing is masked (``fast``) so a barge-in or a non-committable tool call
    surfaces as a raised exception rather than a silent non-match.
    """
    result = bundle.replay(
        ReplaySpec(
            fidelity=ReplayFidelity.ARTIFACT,
            tool_policy=ToolReplayPolicy.DENY,
            timing="fast",
        )
    )
    return tuple((f.sequence, f.name, f.output_ref) for f in result.frames)


def _promote_stub_test_name(turn_id: str) -> str:
    """Return a Python identifier-safe pytest name suffix for a turn id."""
    suffix = re.sub(r"[^0-9A-Za-z_]+", "_", turn_id).strip("_")
    if not suffix:
        suffix = "turn"
    if suffix[0].isdigit() or keyword.iskeyword(suffix):
        suffix = f"turn_{suffix}"
    return suffix


def _promote_test_stub(*, bundle_name: str, turn_id: str, expected: str | None) -> str:
    """Render a copy-pasteable pytest regression stub for a promoted turn.

    Uses the ``easycat_bundle`` fixture plus ``assert_no_error`` /
    ``assert_turn_completed`` / ``assert_exact_match`` from
    :mod:`easycat.debug.testing`.  When the turn's ``agent_final`` text was
    captured we assert it exactly; otherwise we emit a ``TODO`` so the
    author fills in the expected reply.
    """
    safe_id = _promote_stub_test_name(turn_id)
    if expected is not None:
        match_line = f"    assert_exact_match(bundle, expected={expected!r})"
    else:
        match_line = (
            "    # TODO: fill in the expected agent reply for this turn.\n"
            '    # assert_exact_match(bundle, expected="...")'
        )
    return "\n".join(
        [
            "from easycat.debug.testing import (",
            "    assert_exact_match,",
            "    assert_no_error,",
            "    assert_turn_completed,",
            ")",
            "",
            "",
            f"def test_{safe_id}(easycat_bundle):",
            f"    bundle = easycat_bundle({bundle_name!r})",
            "    assert_no_error(bundle)",
            f"    assert_turn_completed(bundle, {turn_id!r})",
            match_line,
            "",
        ]
    )


def _promoted_agent_text(records: list[dict[str, Any]]) -> str | None:
    """Return a safe ``agent_final`` expected value, or ``None`` if sensitive.

    ``journal promote`` prints a copy-pasteable pytest stub.  Journals can
    contain transcripts, tool payloads, and provider text, so never echo an
    ``agent_final`` value when the shared redaction policy would modify it or
    still considers it sensitive after redaction.  Returning ``None`` keeps the
    promoted bundle usable while making the stub ask the author to fill in the
    exact expectation locally.
    """
    for record in records:
        if record.get("name") != "agent_final":
            continue
        data = record.get("data")
        expected: str | None = None
        if isinstance(data, Mapping) and isinstance(data.get("text"), str):
            expected = data["text"]
        else:
            text = record.get("text")
            if isinstance(text, str):
                expected = text
        if expected is None:
            return None
        redacted = redact_text(expected)
        if redacted != expected or contains_unredacted_sensitive_text(redacted):
            return None
        return expected
    return None


def _validate_promoted_slice(
    sliced: RunBundle, tmp_path: Path, *, turn_id: str
) -> tuple[int | None, str | None]:
    """Write *sliced* to *tmp_path*, reload it, and replay twice.

    Returns ``(frame_count, None)`` when the slice replays cleanly and
    deterministically; ``(None, message)`` otherwise.  On any failure the
    temp file is removed so no half-written bundle lingers next to ``--out``.
    """
    try:
        sliced.save(tmp_path)
        reloaded = RunBundle.load(tmp_path)
        first = _replay_signature(reloaded)
        second = _replay_signature(reloaded)
    except (BundleError, BundleValidationError, ReplayError, ReplaySideEffectBlocked) as exc:
        tmp_path.unlink(missing_ok=True)
        return None, f"Promoted turn does not replay cleanly: {exc}"
    except Exception as exc:  # noqa: BLE001 - never leave a temp behind
        tmp_path.unlink(missing_ok=True)
        return None, f"Promoted turn failed validation replay: {exc}"

    if first != second:
        tmp_path.unlink(missing_ok=True)
        return None, (
            f"Turn {turn_id!r} replays non-deterministically; "
            "refusing to promote a flaky regression."
        )
    return len(first), None


def _publish_promoted_slice(tmp_path: Path, out: Path, *, force: bool) -> bool:
    """Publish a validated temp bundle, preserving a raced no-force output.

    Returns ``False`` only when a competing writer created *out* after the
    caller's initial destination check. Other I/O failures propagate after the
    temporary file is removed.
    """
    if force:
        try:
            tmp_path.replace(out)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        return True

    try:
        # Link gives the sibling temp's validated bytes a new name only when
        # the destination does not already exist. Unlike rename/replace, it
        # never turns a late competing output into an overwrite.
        os.link(tmp_path, out)
    except FileExistsError:
        tmp_path.unlink(missing_ok=True)
        return False
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    tmp_path.unlink()
    return True


@cli_command
def promote_turn(
    bundle_path: Path = typer.Argument(
        ...,
        help=(
            "Path to a ZIP bundle archive (``.zip``, ``.bundle``, or "
            "``.easycat-bundle``) or a ``.sqlite`` journal."
        ),
    ),
    turn_id: str = typer.Argument(
        ...,
        help="Turn id to promote into a self-contained, replayable regression bundle.",
    ),
    out: Path = typer.Option(
        ...,
        "--out",
        "-o",
        help="Destination ``.zip`` for the single-turn regression bundle.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite the destination if it already exists.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Promote one turn into a replayable, self-contained regression bundle.

    Slices the turn's journal records and the artifact blobs they reference
    into a new bundle, then validates it before writing: the slice is
    replayed twice at ARTIFACT fidelity (tools denied, fast timing) and must
    both succeed and produce an identical frame signature.  A turn that
    replays non-deterministically (a live tool call, a barge-in) is rejected
    so a flaky bundle never lands as a regression test.  On success the
    bundle is written atomically and a copy-pasteable pytest stub using the
    ``easycat_bundle`` fixture is printed.
    """
    safe_id = safe_turn_id(turn_id)
    if safe_id is None:
        emit_command_error(
            "journal_promote",
            f"Invalid turn id: {turn_id!r}.",
            json_output=json_output,
            exit_code=2,
            path=str(bundle_path),
        )
        raise typer.Exit(2)

    if out.exists() and not force:
        emit_command_error(
            "journal_promote",
            f"Output already exists: {out}. Use --force to overwrite.",
            json_output=json_output,
            exit_code=101,
            path=str(bundle_path),
            out=str(out),
        )
        raise typer.Exit(101)

    if out.exists() and out.is_dir() and not out.is_symlink():
        # --force overwrites a destination *file*; refuse to recursively delete
        # a directory (e.g. the regressions dir passed in place of a .zip name).
        emit_command_error(
            "journal_promote",
            f"Output path is a directory: {out}. Pass a .zip file path, not a directory.",
            json_output=json_output,
            exit_code=2,
            path=str(bundle_path),
            out=str(out),
        )
        raise typer.Exit(2)

    bundle = _load_bundle_or_journal(
        bundle_path, command="journal_promote", json_output=json_output
    )
    turn_records = bundle.filter_by_turn(safe_id)
    if not turn_records:
        emit_command_error(
            "journal_promote",
            f"No journal records found for turn {safe_id!r}.",
            json_output=json_output,
            exit_code=5,
            path=str(bundle_path),
        )
        raise typer.Exit(5)

    try:
        sliced = slice_bundle_by_turn(bundle, safe_id)
    except ValueError:
        emit_command_error(
            "journal_promote",
            f"No journal records found for turn {safe_id!r}.",
            json_output=json_output,
            exit_code=5,
            path=str(bundle_path),
        )
        raise typer.Exit(5) from None

    # Validate-before-write: serialise the slice to a temp .zip, reload it,
    # and replay twice. Only an identical, successful double replay earns a
    # write to --out; anything else deletes the temp and exits 6.
    import tempfile

    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=out.parent, suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    frame_count, error_message = _validate_promoted_slice(sliced, tmp_path, turn_id=safe_id)
    if error_message is not None:
        emit_command_error(
            "journal_promote",
            error_message,
            json_output=json_output,
            exit_code=6,
            path=str(bundle_path),
        )
        raise typer.Exit(6)

    if not _publish_promoted_slice(tmp_path, out, force=force):
        emit_command_error(
            "journal_promote",
            f"Output already exists: {out}. Use --force to overwrite.",
            json_output=json_output,
            exit_code=101,
            path=str(bundle_path),
            out=str(out),
        )
        raise typer.Exit(101)

    expected = _promoted_agent_text(turn_records)
    stub = _promote_test_stub(bundle_name=out.name, turn_id=safe_id, expected=expected)

    if json_output:
        emit_json(
            json_envelope(
                "journal_promote",
                path=str(bundle_path),
                turn_id=safe_id,
                out=str(out),
                records=len(turn_records),
                artifact_count=len(sliced.artifact_blobs),
                frames=frame_count,
                stub=stub,
            )
        )
        raise typer.Exit(0)

    success(f"Promoted turn {safe_id} to {out}")
    stdout_console.print(stub)
