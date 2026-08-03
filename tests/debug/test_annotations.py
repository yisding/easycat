"""Tests for the read-only bundle annotation sidecar."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

import easycat.debug.annotations as annotations_mod
from easycat.debug.annotations import (
    FAILURE_TYPES,
    MAX_ANNOTATIONS,
    MAX_SIDECAR_BYTES,
    SCHEMA_VERSION,
    Annotation,
    AnnotationError,
    load_annotations,
    save_annotation,
    sidecar_path,
)


def test_failure_types_are_the_documented_six() -> None:
    assert FAILURE_TYPES == (
        "asr_error",
        "barge_in_miss",
        "tts_cutoff",
        "wrong_tool",
        "hallucination",
        "self_echo",
    )


def test_sidecar_path_appends_annotations_suffix(tmp_path: Path) -> None:
    bundle = tmp_path / "call.zip"
    assert sidecar_path(bundle) == tmp_path / "call.zip.annotations.json"
    # String input is accepted and normalised to the same target.
    assert sidecar_path(str(bundle)).name == "call.zip.annotations.json"


def test_save_and_load_round_trips(tmp_path: Path) -> None:
    bundle = tmp_path / "call.zip"
    record = save_annotation(
        bundle,
        Annotation(
            turn_id="t1",
            passed=False,
            failure_type="tts_cutoff",
            score=2,
            notes="bot got cut off",
        ),
    )
    assert record["passed"] is False
    assert record["failure_type"] == "tts_cutoff"
    assert record["score"] == 2
    assert record["notes"] == "bot got cut off"
    assert "updated_at" in record

    loaded = load_annotations(bundle)
    assert set(loaded) == {"t1"}
    assert loaded["t1"]["failure_type"] == "tts_cutoff"
    assert loaded["t1"]["score"] == 2

    # Sidecar lands at <bundle>.annotations.json with the schema envelope.
    sidecar = sidecar_path(bundle)
    assert sidecar.exists()
    import json

    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["schema_version"] == SCHEMA_VERSION
    assert "t1" in payload["annotations"]


def test_save_is_read_modify_write_per_turn(tmp_path: Path) -> None:
    bundle = tmp_path / "call.zip"
    save_annotation(bundle, Annotation(turn_id="t1", passed=True, score=5))
    save_annotation(bundle, Annotation(turn_id="t2", passed=False, failure_type="asr_error"))
    # Re-annotating t1 must upsert, not wipe t2.
    save_annotation(bundle, Annotation(turn_id="t1", passed=False, notes="changed my mind"))

    loaded = load_annotations(bundle)
    assert set(loaded) == {"t1", "t2"}
    assert loaded["t1"]["passed"] is False
    assert loaded["t1"]["notes"] == "changed my mind"
    assert loaded["t2"]["failure_type"] == "asr_error"


@pytest.mark.parametrize("failure_type", FAILURE_TYPES)
def test_all_failure_types_accepted(failure_type: str) -> None:
    ann = Annotation(turn_id="t1", failure_type=failure_type)
    assert ann.failure_type == failure_type


def test_bad_failure_type_rejected() -> None:
    with pytest.raises(AnnotationError):
        Annotation(turn_id="t1", failure_type="not_a_real_type")


@pytest.mark.parametrize("score", [1, 2, 3, 4, 5])
def test_score_in_band_accepted(score: int) -> None:
    assert Annotation(turn_id="t1", score=score).score == score


@pytest.mark.parametrize("score", [0, 6, -1, 100])
def test_score_out_of_band_rejected(score: int) -> None:
    with pytest.raises(AnnotationError):
        Annotation(turn_id="t1", score=score)


def test_score_bool_rejected() -> None:
    # ``bool`` is an ``int`` subclass; a stray True must not pass as 1.
    with pytest.raises(AnnotationError):
        Annotation(turn_id="t1", score=True)  # type: ignore[arg-type]


def test_passed_must_be_bool_or_none() -> None:
    assert Annotation(turn_id="t1", passed=None).passed is None
    with pytest.raises(AnnotationError):
        Annotation(turn_id="t1", passed="yes")  # type: ignore[arg-type]


def test_empty_turn_id_rejected() -> None:
    with pytest.raises(AnnotationError):
        Annotation(turn_id="")


def test_notes_length_capped() -> None:
    Annotation(turn_id="t1", notes="x" * 4000)
    with pytest.raises(AnnotationError):
        Annotation(turn_id="t1", notes="x" * 4001)


def test_missing_sidecar_loads_empty(tmp_path: Path) -> None:
    assert load_annotations(tmp_path / "never-written.zip") == {}


def test_corrupt_sidecar_loads_empty(tmp_path: Path) -> None:
    bundle = tmp_path / "call.zip"
    sidecar_path(bundle).write_text("{not valid json", encoding="utf-8")
    assert load_annotations(bundle) == {}


def test_non_object_sidecar_loads_empty(tmp_path: Path) -> None:
    bundle = tmp_path / "call.zip"
    sidecar_path(bundle).write_text("[1, 2, 3]", encoding="utf-8")
    assert load_annotations(bundle) == {}


def test_sidecar_with_bad_annotations_block_loads_empty(tmp_path: Path) -> None:
    bundle = tmp_path / "call.zip"
    sidecar_path(bundle).write_text('{"annotations": "nope"}', encoding="utf-8")
    assert load_annotations(bundle) == {}


def test_save_does_not_touch_the_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / "call.zip"
    bundle.write_bytes(b"ORIGINAL BUNDLE BYTES")
    before = bundle.read_bytes()
    save_annotation(bundle, Annotation(turn_id="t1", passed=True))
    assert bundle.read_bytes() == before


def test_load_annotations_rejects_oversized_sidecar_before_reading(tmp_path: Path) -> None:
    bundle = tmp_path / "call.zip"
    sidecar_path(bundle).write_bytes(b" " * (MAX_SIDECAR_BYTES + 1))
    assert load_annotations(bundle) == {}


def test_load_annotations_rejects_over_count_cap_sidecar(tmp_path: Path) -> None:
    # A compact sidecar can stay under the byte cap while holding far more
    # records than the count cap; load must bound it like a corrupt file so
    # downstream consumers stay bounded and the write path is not locked out.
    import json

    bundle = tmp_path / "call.zip"
    annotations = {f"t{i}": {"passed": True} for i in range(MAX_ANNOTATIONS + 1)}
    sidecar_path(bundle).write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "annotations": annotations}),
        encoding="utf-8",
    )
    assert load_annotations(bundle) == {}
    # The count-cap lockout is gone: a fresh annotation can still be saved,
    # rewriting the oversized sidecar down to a bounded map.
    save_annotation(bundle, Annotation(turn_id="fresh", passed=True))
    assert set(load_annotations(bundle)) == {"fresh"}


def test_load_annotations_tolerates_deep_json_recursion(tmp_path: Path) -> None:
    bundle = tmp_path / "call.zip"
    sidecar_path(bundle).write_text(
        '{"annotations":' + ("[" * 2000) + ("]" * 2000) + "}", encoding="utf-8"
    )
    assert load_annotations(bundle) == {}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are unavailable on this platform")
@pytest.mark.parametrize("via_symlink", [False, True])
def test_load_annotations_rejects_fifo_sidecar_before_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    via_symlink: bool,
) -> None:
    bundle = tmp_path / "call.zip"
    sidecar = sidecar_path(bundle)
    fifo = tmp_path / "annotations.fifo"
    os.mkfifo(fifo)
    if via_symlink:
        sidecar.symlink_to(fifo)
    else:
        os.mkfifo(sidecar)

    def _read_text_must_not_run(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("a non-regular sidecar must not be opened for reading")

    monkeypatch.setattr(Path, "read_text", _read_text_must_not_run)

    assert load_annotations(bundle) == {}


def test_save_annotation_serializes_concurrent_upserts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "call.zip"
    barrier = threading.Barrier(2)
    real_load = annotations_mod.load_annotations
    errors: list[BaseException] = []

    def _synchronized_load(bundle_path: str | Path) -> dict[str, dict[str, object]]:
        try:
            barrier.wait(timeout=0.2)
        except threading.BrokenBarrierError:
            pass
        return real_load(bundle_path)

    monkeypatch.setattr(annotations_mod, "load_annotations", _synchronized_load)

    def _save(turn_id: str) -> None:
        try:
            save_annotation(bundle, Annotation(turn_id=turn_id, passed=True))
        except BaseException as exc:  # noqa: BLE001  # pragma: no cover - asserted below
            errors.append(exc)

    first = threading.Thread(target=_save, args=("t1",))
    second = threading.Thread(target=_save, args=("t2",))
    first.start()
    second.start()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert set(real_load(bundle)) == {"t1", "t2"}


def test_serve_run_bundle_propagates_annotation_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from easycat.debug.bundle import RunBundle
    from easycat.debugger import server

    captured = []
    monkeypatch.setattr(server, "_serve", lambda source, **_kwargs: captured.append(source))

    bundle_path = tmp_path / "call.zip"
    server.serve_run_bundle(
        RunBundle(),
        label="call.zip",
        annotate_path=bundle_path,
        open_browser=False,
    )

    assert captured[0]._annotate_path == bundle_path


def test_save_annotation_rejects_unknown_allowed_turn(tmp_path: Path) -> None:
    bundle = tmp_path / "call.zip"
    with pytest.raises(AnnotationError, match="does not exist"):
        save_annotation(bundle, Annotation(turn_id="missing"), allowed_turn_ids={"known"})


def test_save_annotation_caps_record_count(tmp_path: Path) -> None:
    bundle = tmp_path / "call.zip"
    annotations = {f"t{i}": {"passed": True} for i in range(MAX_ANNOTATIONS)}
    sidecar_path(bundle).write_text(
        __import__("json").dumps({"schema_version": SCHEMA_VERSION, "annotations": annotations}),
        encoding="utf-8",
    )
    with pytest.raises(AnnotationError, match="limited"):
        save_annotation(bundle, Annotation(turn_id="too-many"))
    # Existing turns can still be updated at the cap.
    save_annotation(bundle, Annotation(turn_id="t0", notes="updated"))
    assert load_annotations(bundle)["t0"]["notes"] == "updated"


def test_save_annotation_caps_serialized_sidecar_size(tmp_path: Path, monkeypatch) -> None:
    import easycat.debug.annotations as annotations_mod

    bundle = tmp_path / "call.zip"
    monkeypatch.setattr(annotations_mod, "MAX_SIDECAR_BYTES", 200)
    with pytest.raises(AnnotationError, match="bytes"):
        save_annotation(bundle, Annotation(turn_id="t1", notes="x" * 180))
