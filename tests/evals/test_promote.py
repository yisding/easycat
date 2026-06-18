"""Milestone 11 — hardened, redact-by-default turn promotion.

These tests cover ``easycat.evals.promote_turn_to_test`` (the FORKED, hardened
replacement for ``journal promote``):

* records route through ``redact_value`` before serialization (redact-by-default);
* audio is excluded by default (``--no-audio``);
* the ``contains_unredacted_sensitive_text`` tripwire RAISES without ``allow_pii``;
* the default ``--assert-on hash`` mode embeds a hash, not the verbatim reply;
* the generated ``.py`` imports from ``easycat.evals`` and PASSES under pytest in
  ``tmp_path`` against the committed redacted slice (TEST-2).
"""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from easycat.debug.bundle import FORMAT_VERSION, RunBundle
from easycat.evals import PromotionError, promote_turn_to_test
from easycat.evals.promote import reply_hash

_TURN_ID = "turn-abc"
# A 64-hex artifact ref, the only shape RunBundle.load accepts for blobs.
_BLOB_REF = "a" * 64


def _records(reply: str = "Your refund for order 12345 is on its way.") -> list[dict]:
    return [
        {
            "sequence": 1,
            "kind": "event",
            "name": "turn_started",
            "turn_id": _TURN_ID,
            "session_id": "sess-1",
        },
        {
            "sequence": 2,
            "kind": "event",
            "name": "stt_final",
            "turn_id": _TURN_ID,
            "session_id": "sess-1",
            "data": {"text": "I need a refund", "transcript": "I need a refund"},
        },
        {
            "sequence": 3,
            "kind": "event",
            "name": "agent_final",
            "turn_id": _TURN_ID,
            "session_id": "sess-1",
            "data": {"text": reply, "generated_text": reply},
        },
        {
            "sequence": 4,
            "kind": "event",
            "name": "turn_ended",
            "turn_id": _TURN_ID,
            "session_id": "sess-1",
        },
    ]


def _make_bundle(path: Path, records: list[dict]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "manifest.json",
            json.dumps({"format_version": FORMAT_VERSION, "provider_versions": {}}),
        )
        zf.writestr("journal.ndjson", "\n".join(json.dumps(r) for r in records))


def test_promote_writes_test_and_redacted_slice(tmp_path: Path) -> None:
    bundle = tmp_path / "session.zip"
    _make_bundle(bundle, _records())
    out = tmp_path / "test_regressions.py"

    written = promote_turn_to_test(bundle, _TURN_ID, out=out)

    assert written == out
    assert written.exists()
    slice_path = out.with_suffix(".bundle")
    assert slice_path.exists()

    # The generated test imports from easycat.evals (TEST-2), not debug.testing.
    source = written.read_text(encoding="utf-8")
    assert "from easycat.evals import" in source
    assert "assert_reply_hash" in source


def test_promote_redacts_transcript_by_default(tmp_path: Path) -> None:
    reply = "Your refund for order 12345 is on its way."
    bundle = tmp_path / "session.zip"
    _make_bundle(bundle, _records(reply=reply))
    out = tmp_path / "test_regressions.py"

    promote_turn_to_test(bundle, _TURN_ID, out=out)

    # The committed slice must not carry the verbatim transcript/reply text.
    slice_bundle = RunBundle.load(out.with_suffix(".bundle"))
    journal = slice_bundle.journal_ndjson.decode("utf-8")
    assert reply not in journal
    assert "I need a refund" not in journal
    assert "[REDACTED" in journal

    # Default mode hashes the (redacted) reply rather than embedding it verbatim.
    source = out.read_text(encoding="utf-8")
    assert reply not in source
    assert reply_hash("[REDACTED_PROVIDER_TEXT]") in source


def test_promote_excludes_audio_by_default(tmp_path: Path) -> None:
    records = _records()
    records[2]["output_ref"] = _BLOB_REF
    bundle = tmp_path / "session.zip"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "manifest.json",
            json.dumps({"format_version": FORMAT_VERSION, "provider_versions": {}}),
        )
        zf.writestr("journal.ndjson", "\n".join(json.dumps(r) for r in records))
        zf.writestr(f"artifacts/{_BLOB_REF}.bin", b"RAWAUDIOBYTES")

    out = tmp_path / "test_regressions.py"
    promote_turn_to_test(bundle, _TURN_ID, out=out)

    slice_bundle = RunBundle.load(out.with_suffix(".bundle"))
    assert not slice_bundle.artifact_blobs
    journal = slice_bundle.journal_ndjson.decode("utf-8")
    assert _BLOB_REF not in journal  # the output_ref pointer is dropped too


def test_tripwire_raises_without_allow_pii(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Defense in depth: even if redaction were bypassed (here forced to identity),
    # the tripwire must catch a residual secret and RAISE before writing.
    import easycat.evals.promote as promote_mod

    monkeypatch.setattr(promote_mod, "redact_value", lambda value, key=None: value)

    records = _records()
    records[1]["data"] = {"note": "secret sk-ABCDEF1234567890ABCDEF here"}
    bundle = tmp_path / "session.zip"
    _make_bundle(bundle, records)
    out = tmp_path / "test_regressions.py"

    with pytest.raises(PromotionError, match="unredacted sensitive text"):
        promote_turn_to_test(bundle, _TURN_ID, out=out)
    assert not out.exists()


def test_allow_pii_disables_tripwire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import easycat.evals.promote as promote_mod

    monkeypatch.setattr(promote_mod, "redact_value", lambda value, key=None: value)

    records = _records()
    records[1]["data"] = {"note": "secret sk-ABCDEF1234567890ABCDEF here"}
    bundle = tmp_path / "session.zip"
    _make_bundle(bundle, records)
    out = tmp_path / "test_regressions.py"

    written = promote_turn_to_test(bundle, _TURN_ID, out=out, allow_pii=True)
    assert written.exists()


def test_missing_turn_raises(tmp_path: Path) -> None:
    bundle = tmp_path / "session.zip"
    _make_bundle(bundle, _records())
    out = tmp_path / "test_regressions.py"

    with pytest.raises(PromotionError, match="No journal records"):
        promote_turn_to_test(bundle, "missing-turn", out=out)


def test_exact_mode_warns(tmp_path: Path) -> None:
    bundle = tmp_path / "session.zip"
    _make_bundle(bundle, _records())
    out = tmp_path / "test_regressions.py"

    with pytest.warns(UserWarning, match="verbatim"):
        promote_turn_to_test(bundle, _TURN_ID, out=out, assert_on="exact")


def test_generated_test_imports_and_passes(tmp_path: Path) -> None:
    """The generated .py must import and PASS under pytest in tmp_path (TEST-2)."""
    bundle = tmp_path / "session.zip"
    _make_bundle(bundle, _records())
    out = tmp_path / "test_promoted_regression.py"

    promote_turn_to_test(bundle, _TURN_ID, out=out)

    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(out), "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_generated_regex_mode_passes(tmp_path: Path) -> None:
    bundle = tmp_path / "session.zip"
    _make_bundle(bundle, _records())
    out = tmp_path / "test_promoted_regex.py"

    promote_turn_to_test(bundle, _TURN_ID, out=out, assert_on="regex")
    assert "assert_regex" in out.read_text(encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(out), "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
