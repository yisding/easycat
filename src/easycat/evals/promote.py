"""Promote a recorded turn into a committed regression test (hardened).

Milestone 11 (Workstream C) implements the hardened, security-sensitive
promotion path. Unlike the legacy ``journal promote`` command
(``cli/debug/bundles.py``) — which copies the full raw NDJSON, every audio
blob, and the verbatim agent reply into a committed file with ZERO redaction —
``promote_turn_to_test`` is **redact-by-default**:

1. **Redact by default.** Every promoted journal record is routed through
   :func:`easycat.validation.redaction.redact_value` before serialization.
2. **No audio by default.** Artifact blobs are excluded unless
   ``include_audio=True`` (mirrors ``bundles export`` ``include_audio=False``).
3. **Tripwire gated by ``allow_pii``.** Before writing the committed slice the
   serialized NDJSON is scanned with
   :func:`easycat.validation.redaction.contains_unredacted_sensitive_text`
   (mirrors ``_assert_context_pack_redacted``) and a :class:`RuntimeError` is
   raised unless ``allow_pii=True``.
4. **Hash-by-default assertion.** Because redaction is field-name + secret-regex
   only (no NER), a transcript that is itself the assertion target cannot be
   both redacted and useful. The default ``assert_on="hash"`` asserts a stable
   hash of the (redacted) reply; ``"regex"`` is the redaction-safe alternative;
   ``"exact"`` embeds the verbatim reply and is opt-in (it warns).

The generated ``.py`` imports its helpers from :mod:`easycat.evals` (TEST-2):
every symbol it references is re-exported there, so a promoted test imports and
runs under ``pytest`` against the committed redacted slice.
"""

from __future__ import annotations

import hashlib
import json
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from easycat.debug.bundle import ArtifactEntry, RunBundle
from easycat.debug.export import slice_bundle_by_turn
from easycat.debug.testing import _assistant_text
from easycat.validation.redaction import (
    REDACTED_PROVIDER_TEXT,
    REDACTED_TRANSCRIPT,
    contains_unredacted_sensitive_text,
    redact_value,
)

__all__ = ["PromotionError", "promote_turn_to_test"]

_ASSERT_MODES = ("hash", "regex", "exact")
_PROMOTE_MODES = ("record-assertion", "artifact-replay")

# ``redact_value`` is field-name + secret-regex only and does NOT redact the
# generic ``data.text`` field, but that is exactly where the journal sink writes
# the verbatim transcript / agent reply. Promotion redacts those reply-bearing
# ``text`` fields by event name so the committed slice never embeds the raw
# conversation, even when the text carries no secret-shaped token.
_REPLY_TEXT_EVENTS: Mapping[str, str] = {
    "agent_final": REDACTED_PROVIDER_TEXT,
    "stage.tts.execute": REDACTED_PROVIDER_TEXT,
    "stt_final": REDACTED_TRANSCRIPT,
    "stt.final": REDACTED_TRANSCRIPT,
}


class PromotionError(RuntimeError):
    """Raised when a turn cannot be safely promoted into a regression test."""


def reply_hash(text: str) -> str:
    """Return the stable promotion hash of a (redacted) reply string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_source_bundle(bundle_path: Path) -> RunBundle:
    """Load a ZIP bundle or SQLite journal into a :class:`RunBundle`."""
    if bundle_path.suffix == ".sqlite":
        from easycat.cli.debug.bundles import _crash_dump_artifact_root

        return RunBundle.from_partial_journal(
            bundle_path,
            artifact_root=_crash_dump_artifact_root(bundle_path),
        )
    return RunBundle.load(bundle_path)


def _redact_reply_text(original: Mapping[str, Any], redacted: dict[str, Any]) -> None:
    """Redact the reply-bearing ``data.text`` / top-level ``text`` field.

    ``redact_value`` leaves the generic ``text`` key untouched, but for
    reply/transcript events that field holds the verbatim conversation, so we
    replace it with the appropriate placeholder by event name.
    """
    placeholder = _REPLY_TEXT_EVENTS.get(str(original.get("name")))
    if placeholder is None:
        return
    data = redacted.get("data")
    if isinstance(data, dict) and "text" in data and data.get("text"):
        data["text"] = placeholder
    if redacted.get("text"):
        redacted["text"] = placeholder


def _redact_slice(sliced: RunBundle, *, include_audio: bool) -> RunBundle:
    """Return a redacted copy of *sliced* with audio optionally dropped.

    Every journal record is routed through :func:`redact_value` so transcripts,
    tool arguments, provider output, and phone numbers never reach the
    committed slice verbatim. Audio blobs are dropped (and their ``input_ref`` /
    ``output_ref`` pointers cleared) unless ``include_audio`` is set.
    """
    redacted_records: list[dict[str, Any]] = []
    for record in sliced.records():
        redacted = redact_value(record)
        if not isinstance(redacted, dict):
            continue
        _redact_reply_text(record, redacted)
        if not include_audio:
            redacted.pop("input_ref", None)
            redacted.pop("output_ref", None)
        redacted_records.append(redacted)

    journal_ndjson = "\n".join(
        json.dumps(record, default=str, sort_keys=True) for record in redacted_records
    ).encode("utf-8")

    if include_audio:
        artifact_blobs = dict(sliced.artifact_blobs)
        artifact_index = dict(sliced.artifact_index)
    else:
        artifact_blobs = {}
        artifact_index = {}

    manifest_provider = redact_value(sliced.manifest.provider_versions)
    manifest_config = redact_value(sliced.manifest.config_snapshot)
    manifest_env = redact_value(sliced.manifest.env_metadata)
    from easycat.debug.bundle import Manifest

    redacted_manifest = Manifest(
        format_version=sliced.manifest.format_version,
        provider_versions=manifest_provider if isinstance(manifest_provider, dict) else {},
        config_snapshot=manifest_config if isinstance(manifest_config, dict) else {},
        env_metadata=manifest_env if isinstance(manifest_env, dict) else {},
        sharing_banner=sliced.manifest.sharing_banner,
    )

    return RunBundle(
        format_version=sliced.format_version,
        manifest=redacted_manifest,
        journal_ndjson=journal_ndjson,
        artifact_index={
            ref: ArtifactEntry(ref=ref, size_bytes=len(blob))
            for ref, blob in artifact_blobs.items()
        }
        if include_audio
        else artifact_index,
        artifact_blobs=artifact_blobs,
        replay_entry_points=list(sliced.replay_entry_points),
        sharing_banner=sliced.sharing_banner,
    )


def _redacted_reply(redacted: RunBundle, turn_id: str) -> str | None:
    """Return the (already redacted) ``agent_final`` reply text for *turn_id*."""
    for record in redacted.filter_by_turn(turn_id):
        if record.get("name") != "agent_final":
            continue
        text = _assistant_text(record)
        if text:
            return text
    return None


def _hardened_stub(
    *,
    bundle_name: str,
    turn_id: str,
    assert_on: str,
    reply: str | None,
    name: str | None,
) -> str:
    """Render a self-contained pytest regression stub for a promoted turn.

    The stub imports every helper from :mod:`easycat.evals` (TEST-2) and loads
    the committed *redacted* slice via the ``easycat_bundle`` fixture.
    """
    safe_id = turn_id.replace("-", "_")
    test_name = name or f"test_{safe_id}_regression"

    imports = ["    assert_no_error,", "    assert_turn_completed,"]
    if assert_on == "hash":
        imports.append("    assert_reply_hash,")
    elif assert_on == "regex":
        imports.append("    assert_regex,")
    else:
        imports.append("    assert_exact_match,")
    imports.sort()

    if assert_on == "hash":
        digest = reply_hash(reply or "")
        match_line = f"    assert_reply_hash(bundle, expected_hash={digest!r})"
    elif assert_on == "regex":
        if reply:
            # The reply is already redacted; a literal-escaped regex over it is
            # the redaction-safe assertion target.
            import re

            pattern = re.escape(reply)
            match_line = f"    assert_regex(bundle, pattern={pattern!r})"
        else:
            match_line = (
                "    # TODO: fill in a regex over the expected (redacted) reply.\n"
                '    # assert_regex(bundle, pattern="...")'
            )
    else:  # exact
        if reply is not None:
            match_line = f"    assert_exact_match(bundle, expected={reply!r})"
        else:
            match_line = (
                "    # TODO: fill in the expected agent reply for this turn.\n"
                '    # assert_exact_match(bundle, expected="...")'
            )

    return "\n".join(
        [
            "from easycat.evals import (",
            *imports,
            ")",
            "",
            "",
            f"def {test_name}(easycat_bundle):",
            f"    bundle = easycat_bundle({bundle_name!r})",
            "    assert_no_error(bundle)",
            f"    assert_turn_completed(bundle, {turn_id!r})",
            match_line,
            "",
        ]
    )


def promote_turn_to_test(
    bundle_path: str | Path,
    turn_id: str,
    *,
    out: str | Path,
    name: str | None = None,
    include_audio: bool = False,
    allow_pii: bool = False,
    mode: str = "record-assertion",
    assert_on: str = "hash",
) -> Path:
    """Generate a hardened pytest regression test from a recorded turn.

    Writes two committed artifacts next to *out*: the redacted single-turn
    slice (``<out_stem>.bundle``) and the ``.py`` regression test that loads it.
    Returns the path to the written ``.py`` test file.

    Raises :class:`PromotionError` when the turn is missing, the slice does not
    replay cleanly/deterministically, or the serialized slice still contains
    unredacted sensitive text and ``allow_pii`` is not set.
    """
    bundle_path = Path(bundle_path)
    out = Path(out)

    if assert_on not in _ASSERT_MODES:
        raise ValueError(f"assert_on must be one of {_ASSERT_MODES}; got {assert_on!r}")
    if mode not in _PROMOTE_MODES:
        raise ValueError(f"mode must be one of {_PROMOTE_MODES}; got {mode!r}")
    if assert_on == "exact":
        warnings.warn(
            "assert_on='exact' embeds the verbatim agent reply into the committed "
            "test; prefer the redaction-safe 'hash' (default) or 'regex' modes.",
            stacklevel=2,
        )

    bundle = _load_source_bundle(bundle_path)
    if not bundle.filter_by_turn(turn_id):
        raise PromotionError(f"No journal records found for turn {turn_id!r}")

    try:
        sliced = slice_bundle_by_turn(bundle, turn_id)
    except ValueError as exc:
        raise PromotionError(str(exc)) from None

    redacted = _redact_slice(sliced, include_audio=include_audio)

    # Tripwire: refuse to write a slice that still carries sensitive text unless
    # the caller explicitly opts in via allow_pii.
    serialized = redacted.journal_ndjson.decode("utf-8", errors="replace")
    if not allow_pii and contains_unredacted_sensitive_text(serialized):
        raise PromotionError(
            "Promoted slice still contains unredacted sensitive text; refusing to "
            "write it. Pass allow_pii=True (CLI: --allow-pii) only if you have "
            "reviewed the committed content."
        )

    # Validate-before-write: the slice must replay cleanly and deterministically
    # at ARTIFACT fidelity with tools denied (artifact-replay safety).
    import tempfile

    out.parent.mkdir(parents=True, exist_ok=True)
    bundle_out = out.with_suffix(".bundle")
    with tempfile.NamedTemporaryFile(dir=out.parent, suffix=".bundle", delete=False) as handle:
        tmp_path = Path(handle.name)

    from easycat.cli.debug.bundles import _validate_promoted_slice

    _, error_message = _validate_promoted_slice(redacted, tmp_path, turn_id=turn_id)
    if error_message is not None:
        raise PromotionError(error_message)
    tmp_path.replace(bundle_out)

    reply = _redacted_reply(redacted, turn_id)
    stub = _hardened_stub(
        bundle_name=bundle_out.name,
        turn_id=turn_id,
        assert_on=assert_on,
        reply=reply,
        name=name,
    )
    out.write_text(stub, encoding="utf-8")
    return out
