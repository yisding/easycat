"""Tests for the ``cp_<sequence>`` checkpoint vocabulary."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from easycat.debug.bundle import (
    FORMAT_VERSION,
    CommittableCheckpoint,
    RunBundle,
    checkpoint_id,
    parse_checkpoint_id,
)


def test_checkpoint_id_roundtrip() -> None:
    assert checkpoint_id(87) == "cp_87"
    assert parse_checkpoint_id("cp_87") == 87


def test_checkpoint_id_rejects_negative() -> None:
    with pytest.raises(ValueError):
        checkpoint_id(-1)


@pytest.mark.parametrize("bad", ["", "87", "cp-87", "cp_", "cp_abc", "CP_87", "cp_-5"])
def test_parse_checkpoint_id_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_checkpoint_id(bad)


def test_committable_checkpoint_exposes_id() -> None:
    cp = CommittableCheckpoint(sequence=3, stage="stt", unit_id="u-1")
    assert cp.checkpoint_id == "cp_3"


def _make_bundle(path: Path, records: list[dict]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps({"format_version": FORMAT_VERSION}))
        zf.writestr("journal.ndjson", "\n".join(json.dumps(r) for r in records))


def test_lookup_by_checkpoint_id(tmp_path: Path) -> None:
    """``RunBundle.lookup_by_checkpoint_id`` is the ``cp_N`` analogue of
    ``lookup_by_sequence`` — user-facing code should never need to strip
    the ``cp_`` prefix by hand."""
    bundle_path = tmp_path / "demo.zip"
    _make_bundle(
        bundle_path,
        [
            {"sequence": 12, "name": "TurnStarted"},
            {"sequence": 13, "name": "STTFinal", "data": {"text": "hi"}},
        ],
    )
    bundle = RunBundle.load(bundle_path)

    hit = bundle.lookup_by_checkpoint_id("cp_13")
    assert hit is not None
    assert hit["name"] == "STTFinal"

    miss = bundle.lookup_by_checkpoint_id("cp_99")
    assert miss is None

    with pytest.raises(ValueError):
        bundle.lookup_by_checkpoint_id("nope")
