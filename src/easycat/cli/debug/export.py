"""``easycat bundles export`` — write a redacted context pack for a coding agent."""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape

from easycat.cli._errors import cli_command
from easycat.cli._output import (
    emit_command_error,
    emit_json,
    json_envelope,
    stdout_console,
    success,
    warn,
)
from easycat.cli.debug._common import (
    _load_bundle_or_journal,
    _record_detail,
    _record_stage,
    _summarise_bundle,
)
from easycat.debug._turn_timeline import record_wall_ns
from easycat.debug.bundle import RunBundle
from easycat.validation.redaction import (
    REDACTION_VERSION,
    contains_unredacted_sensitive_text,
    redact_text,
    redact_value,
)

_EXPORT_TARGETS = frozenset(("claude-code", "cursor", "codex"))
_REDACTION_POLICIES = frozenset(("development", "production", "regulated"))
_CONTEXT_DATA_KEYS = frozenset(
    {
        "boundary_reason",
        "bridge_latency_ms",
        "call_id",
        "cancellation_mode",
        "cause",
        "caused_by_signal_id",
        "checkpoint_id",
        "committable",
        "direction",
        "display_name",
        "duration_ms",
        "exit_reason",
        "fidelity",
        "format",
        "framework",
        "from_unit",
        "handoff_reason",
        "latency_ms",
        "model",
        "mutation_kind",
        "observed_stage",
        "parent_unit_id",
        "phase",
        "provider",
        "provider_name",
        "sample_rate",
        "signal_id",
        "signal_kind",
        "stage",
        "to_unit",
        "tool_call_id",
        "tool_name",
        "transition_kind",
        "unit_id",
        "unit_kind",
    }
)
_CONTEXT_TOP_LEVEL_KEYS = _CONTEXT_DATA_KEYS | frozenset(
    {
        "framework",
        "direction",
        "bridge_latency_ms",
    }
)
_CONTEXT_ERROR_KEYS = frozenset(("type", "code", "status"))


def _default_export_path(bundle_path: Path) -> Path:
    return bundle_path.with_name(f"{bundle_path.stem}-pack")


def _context_error(error: Any) -> dict[str, Any] | None:
    if not isinstance(error, Mapping):
        return None

    context = {
        str(key): redact_value(error[key], str(key))
        for key in sorted(error, key=str)
        if str(key) in _CONTEXT_ERROR_KEYS and error[key] not in (None, "", [], {})
    }
    omitted = sum(
        1
        for key, value in error.items()
        if str(key) not in _CONTEXT_ERROR_KEYS and value not in (None, "", [], {})
    )
    if omitted > 0:
        context["omitted_error_fields"] = omitted
    return context or None


def _context_record(record: Mapping[str, Any]) -> dict[str, Any]:
    context: dict[str, Any] = {}
    for key in ("sequence", "kind", "name", "session_id", "turn_id"):
        value = record.get(key)
        if value not in (None, ""):
            context[key] = redact_value(value, key)

    wall_ns = record_wall_ns(record)
    if wall_ns is not None:
        context["wall_ns"] = wall_ns

    data = record.get("data")
    if isinstance(data, Mapping):
        safe_data = {
            str(key): redact_value(data[key], str(key))
            for key in sorted(data, key=str)
            if str(key) in _CONTEXT_DATA_KEYS
        }
        if safe_data:
            context["data"] = safe_data
        omitted = len(data) - len(safe_data)
        if omitted > 0:
            context["omitted_data_fields"] = omitted
    elif data not in (None, "", {}, []):
        context["omitted_data_fields"] = 1

    for key in sorted(_CONTEXT_TOP_LEVEL_KEYS):
        if key in record and key not in context:
            context[key] = redact_value(record[key], key)

    refs: dict[str, Any] = {}
    for key in ("input_ref", "output_ref"):
        value = record.get(key)
        if value:
            refs[key] = redact_value(value, key)
    if refs:
        context["refs"] = refs

    error = _context_error(record.get("error"))
    if error:
        context["error"] = error

    tags = record.get("tags")
    if isinstance(tags, list | tuple | set | frozenset) and tags:
        context["tags"] = redact_value(sorted(tags, key=str), "tags")

    return context


def _context_records(bundle: RunBundle) -> list[dict[str, Any]]:
    return [_context_record(record) for record in bundle.records()]


def _markdown_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return redact_text(text).replace("\n", " ").replace("|", r"\|")


def _timeline_markdown(records: list[dict[str, Any]]) -> str:
    lines = [
        "# Timeline",
        "",
        "| seq | kind | name | turn | stage | detail |",
        "|---:|---|---|---|---|---|",
    ]
    for record in records:
        lines.append(
            "| "
            + " | ".join(
                (
                    _markdown_cell(record.get("sequence")),
                    _markdown_cell(record.get("kind")),
                    _markdown_cell(record.get("name")),
                    _markdown_cell(record.get("turn_id")),
                    _markdown_cell(_record_stage(record)),
                    _markdown_cell(_record_detail(record)),
                )
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def _readme_text(
    *,
    target: str,
    source_path: str,
    summary: Mapping[str, Any],
    redaction_requested: str,
    redaction_applied: str,
) -> str:
    session_id = summary.get("session_id") or "(unknown)"
    return "\n".join(
        [
            "# EasyCat Debug Context Pack",
            "",
            f"Target: {target}",
            f"Source: {source_path}",
            f"Session: {session_id}",
            f"Records: {summary.get('records', 0)}",
            f"Errors: {summary.get('errors', 0)}",
            "",
            "## Files",
            "",
            "- summary.json: bundle metadata, provider versions, and artifact index.",
            "- timeline.md: compact redacted event timeline for quick reading.",
            "- timeline.jsonl: one redacted event per line for scripts and coding agents.",
            "",
            "## Safety Boundary",
            "",
            "This pack intentionally omits raw journal payload fields such as transcripts, "
            "prompts, generated text, tool arguments, tool results, and provider responses.",
            "Treat the original bundle or SQLite journal as sensitive.",
            f"Requested redaction: {redaction_requested}; applied redaction: {redaction_applied}.",
            "",
            "## Useful Commands",
            "",
            "```bash",
            "easycat bundles show <bundle> --json",
            "easycat replay <bundle> --json",
            "```",
            "",
        ]
    )


def _artifact_manifest(
    bundle: RunBundle,
    *,
    output_path: Path,
    include_audio: bool,
) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    artifacts_dir = output_path / "artifacts"
    if include_audio and bundle.artifact_blobs:
        artifacts_dir.mkdir()

    for ref, entry in sorted(bundle.artifact_index.items()):
        artifact: dict[str, Any] = {
            "ref": redact_value(ref, "artifact_ref"),
            "size_bytes": entry.size_bytes,
            "included": False,
        }
        data = bundle.artifact_blobs.get(ref)
        if include_audio and data is not None:
            filename = f"{ref}.bin"
            (artifacts_dir / filename).write_bytes(data)
            artifact["included"] = True
            artifact["path"] = f"artifacts/{filename}"
        artifacts.append(artifact)
    return artifacts


def _prepare_output_dir(
    output_path: Path,
    *,
    force: bool,
    command: str,
    json_output: bool,
) -> None:
    resolved = output_path.resolve(strict=False)
    cwd = Path.cwd().resolve()
    home = Path.home().resolve()
    if (
        resolved == Path(resolved.anchor)
        or resolved == cwd
        or resolved in cwd.parents
        or resolved == home
        or resolved in home.parents
    ):
        emit_command_error(
            command,
            f"Refusing to export into {output_path}.",
            json_output=json_output,
            exit_code=1,
            output_path=str(output_path),
        )
        raise typer.Exit(1)

    if output_path.exists():
        if not force:
            emit_command_error(
                command,
                f"Output path already exists: {output_path}. Use --force to replace it.",
                json_output=json_output,
                exit_code=101,
                output_path=str(output_path),
            )
            raise typer.Exit(101)
        if output_path.is_symlink() or not output_path.is_dir():
            output_path.unlink()
        else:
            shutil.rmtree(output_path)
    output_path.mkdir(parents=True)


def _assert_context_pack_redacted(output_path: Path) -> None:
    for path in (
        output_path / "README.md",
        output_path / "summary.json",
        output_path / "timeline.md",
    ):
        if contains_unredacted_sensitive_text(path.read_text(encoding="utf-8")):
            raise RuntimeError(f"{path.name} contains unredacted sensitive text")
    timeline = output_path / "timeline.jsonl"
    if contains_unredacted_sensitive_text(timeline.read_text(encoding="utf-8")):
        raise RuntimeError("timeline.jsonl contains unredacted sensitive text")


def _write_context_pack(
    bundle: RunBundle,
    *,
    bundle_path: Path,
    output_path: Path,
    target: str,
    redaction_requested: str,
    include_audio: bool,
    force: bool,
    command: str,
    json_output: bool,
) -> dict[str, Any]:
    _prepare_output_dir(output_path, force=force, command=command, json_output=json_output)

    summary = redact_value(_summarise_bundle(bundle))
    if not isinstance(summary, dict):
        summary = {}
    records = _context_records(bundle)
    source_path = redact_text(str(bundle_path))
    # Context-pack export always applies the conservative production redaction
    # boundary regardless of the requested policy; durable journals are on by
    # default, so exports must stay safe to share without an explicit opt-in.
    redaction_applied = "production"
    files = ["README.md", "summary.json", "timeline.md", "timeline.jsonl"]

    artifacts = _artifact_manifest(bundle, output_path=output_path, include_audio=include_audio)
    if include_audio and artifacts:
        files.append("artifacts/")

    manifest = {
        "provider_versions": redact_value(bundle.manifest.provider_versions),
        "config_snapshot": redact_value(bundle.manifest.config_snapshot),
        "env_metadata": redact_value(bundle.manifest.env_metadata),
        "sharing_banner": redact_text(bundle.manifest.sharing_banner or bundle.sharing_banner),
    }
    payload = {
        "schema_version": 1,
        "target": target,
        "source_path": source_path,
        "format_version": bundle.format_version,
        "redaction": {
            "version": REDACTION_VERSION,
            "requested": redaction_requested,
            "applied": redaction_applied,
            "boundary": "context-pack-minimal",
        },
        "summary": summary,
        "manifest": manifest,
        "artifacts": artifacts,
        "files": files,
    }

    (output_path / "README.md").write_text(
        _readme_text(
            target=target,
            source_path=source_path,
            summary=summary,
            redaction_requested=redaction_requested,
            redaction_applied=redaction_applied,
        ),
        encoding="utf-8",
    )
    (output_path / "summary.json").write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    (output_path / "timeline.md").write_text(_timeline_markdown(records), encoding="utf-8")
    (output_path / "timeline.jsonl").write_text(
        "".join(json.dumps(record, sort_keys=False) + "\n" for record in records),
        encoding="utf-8",
    )

    _assert_context_pack_redacted(output_path)
    return {
        "target": target,
        "output_path": str(output_path),
        "source_path": source_path,
        "format_version": bundle.format_version,
        "redaction": payload["redaction"],
        "summary": summary,
        "files": files,
        "records": len(records),
        "artifacts": artifacts,
    }


@cli_command
def export_bundle(
    bundle_path: Path = typer.Argument(
        ...,
        help=(
            "Path to a ZIP bundle archive (``.zip``, ``.bundle``, or "
            "``.easycat-bundle``) or a ``.sqlite`` journal."
        ),
    ),
    export_for: str = typer.Option(
        "claude-code",
        "--for",
        help="Consumer format: claude-code, cursor, or codex.",
    ),
    output_path: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Directory to write (default: ``./<bundle>-pack/`` next to the bundle).",
    ),
    redaction: str = typer.Option(
        "production",
        "--redaction",
        help="Requested policy: development, production, or regulated.",
    ),
    include_audio: bool = typer.Option(
        False,
        "--include-audio/--no-include-audio",
        help="Copy artifact blobs into the pack. Off by default.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Replace the output directory if it already exists.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Write a redacted context pack for a coding agent."""
    target = export_for.strip().lower()
    if target not in _EXPORT_TARGETS:
        emit_command_error(
            "bundles_export",
            "target must be one of: claude-code, cursor, codex",
            json_output=json_output,
            exit_code=2,
        )
        raise typer.Exit(2)

    redaction_requested = redaction.strip().lower()
    if redaction_requested not in _REDACTION_POLICIES:
        emit_command_error(
            "bundles_export",
            "redaction must be one of: development, production, regulated",
            json_output=json_output,
            exit_code=2,
        )
        raise typer.Exit(2)

    bundle = _load_bundle_or_journal(
        bundle_path,
        command="bundles_export",
        json_output=json_output,
    )
    destination = output_path or _default_export_path(bundle_path)

    if redaction_requested != "production" and not json_output:
        warn("Context-pack export currently applies the conservative production boundary.")
    if include_audio and not json_output:
        warn(
            "Artifact blobs may contain sensitive audio or payload data; "
            "treat the pack as sensitive."
        )

    try:
        payload = _write_context_pack(
            bundle,
            bundle_path=bundle_path,
            output_path=destination,
            target=target,
            redaction_requested=redaction_requested,
            include_audio=include_audio,
            force=force,
            command="bundles_export",
            json_output=json_output,
        )
    except typer.Exit:
        raise
    except RuntimeError as exc:
        emit_command_error(
            "bundles_export",
            f"Export failed: {exc}",
            json_output=json_output,
            exit_code=1,
            path=str(bundle_path),
            output_path=str(destination),
        )
        raise typer.Exit(1) from None

    if json_output:
        emit_json(json_envelope("bundles_export", **payload))
        raise typer.Exit(0)

    success(f"Wrote context pack to {destination}")
    stdout_console.print(escape(str(destination)), soft_wrap=True)
