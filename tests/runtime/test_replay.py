"""Unit tests for :mod:`easycat.runtime.replay`.

These cover the pure helpers and the ``ReplayRunner`` walk behaviour —
provider-version match (AC4.21), ``mask_nondeterministic``,
committable-boundary enforcement (T4.8), tool-policy enforcement
(AC4.24), and the three fidelity downgrade paths.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from easycat.debug.bundle import (
    FORMAT_VERSION,
    CommittableCheckpoint,
    RunBundle,
)
from easycat.runtime.replay import (
    REPLAY_IGNORE_FIELDS,
    ProviderVersionMismatchError,
    ReplayCassette,
    ReplayError,
    ReplayFidelity,
    ReplayResult,
    ReplaySideEffectBlocked,
    ReplaySpec,
    ToolReplayPolicy,
    VersionMismatch,
    check_provider_versions,
    find_nearest_committable,
    mask_nondeterministic,
)

# ── Helpers ──────────────────────────────────────────────────────


def _write_bundle(
    tmp_path: Path,
    records: list[dict],
    *,
    provider_versions: dict | None = None,
    replay_entry_points: list[dict] | None = None,
    artifacts: dict[str, bytes] | None = None,
) -> Path:
    """Write a minimal bundle on disk for round-trip tests."""
    path = tmp_path / "b.zip"
    manifest = {
        "format_version": FORMAT_VERSION,
        "provider_versions": provider_versions or {},
        "replay_entry_points": replay_entry_points or [],
    }
    journal_ndjson = "\n".join(json.dumps(r) for r in records)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("journal.ndjson", journal_ndjson)
        for ref, data in (artifacts or {}).items():
            zf.writestr(f"artifacts/{ref}.bin", data)
    return path


_SHA_A = "a" * 64
_SHA_B = "b" * 64


def _spec(**overrides) -> ReplaySpec:
    """ReplaySpec factory with a sensible fidelity default for tests."""
    overrides.setdefault("fidelity", ReplayFidelity.ARTIFACT)
    return ReplaySpec(**overrides)


# ── mask_nondeterministic ────────────────────────────────────────


class TestMaskNondeterministic:
    def test_plain_keys_are_stripped(self):
        data = {
            "recorded_at_utc": "2026-04-13T00:00:00",
            "payload": "keep me",
        }
        out = mask_nondeterministic(data)
        assert "recorded_at_utc" not in out
        assert out["payload"] == "keep me"

    def test_dotted_paths_strip_nested_keys(self):
        data = {
            "timing": {"wall_ns": 123, "cpu_ns": 456, "stage_ms": 789},
            "value": 1,
        }
        out = mask_nondeterministic(data)
        assert "wall_ns" not in out["timing"]
        assert "cpu_ns" not in out["timing"]
        # Fields not in REPLAY_IGNORE_FIELDS survive.
        assert out["timing"]["stage_ms"] == 789
        assert out["value"] == 1

    def test_dotted_path_only_matches_from_root(self):
        # "cursor.entered_at" is a root path; a nested cursor should NOT
        # be masked unless reached via the root.
        data = {"nested": {"cursor": {"entered_at": "keep"}}}
        out = mask_nondeterministic(data)
        assert out["nested"]["cursor"]["entered_at"] == "keep"

    def test_deep_copy_leaves_original_untouched(self):
        data = {"recorded_at_utc": "x", "deep": {"recorded_at_utc": "y"}}
        out = mask_nondeterministic(data)
        # Plain keys are stripped anywhere, including nested.
        assert "recorded_at_utc" not in out["deep"]
        # Original is unmodified.
        assert data == {"recorded_at_utc": "x", "deep": {"recorded_at_utc": "y"}}

    def test_lists_and_tuples_walk_through(self):
        data = {
            "items": [
                {"recorded_at_utc": "x", "value": 1},
                {"recorded_at_utc": "y", "value": 2},
            ],
            "tpl": ({"recorded_at_utc": "z"},),
        }
        out = mask_nondeterministic(data)
        assert [item.get("recorded_at_utc") for item in out["items"]] == [None, None]
        assert [item["value"] for item in out["items"]] == [1, 2]
        assert "recorded_at_utc" not in out["tpl"][0]

    def test_custom_field_set(self):
        data = {"x": 1, "y": 2, "z": 3}
        out = mask_nondeterministic(data, fields={"x", "z"})
        assert out == {"y": 2}

    def test_scalar_passes_through(self):
        assert mask_nondeterministic("hello") == "hello"
        assert mask_nondeterministic(42) == 42

    def test_ignore_fields_snapshot(self):
        # REPLAY_IGNORE_FIELDS should cover at minimum the base set.
        assert "timing.wall_ns" in REPLAY_IGNORE_FIELDS
        assert "recorded_at_utc" in REPLAY_IGNORE_FIELDS
        assert "artifact_written_at" in REPLAY_IGNORE_FIELDS


# ── find_nearest_committable ─────────────────────────────────────


class TestFindNearestCommittable:
    def test_before_and_after_present(self):
        cps = [
            CommittableCheckpoint(sequence=10, stage="stt"),
            CommittableCheckpoint(sequence=30, stage="agent"),
            CommittableCheckpoint(sequence=50, stage="tts"),
        ]
        before, after = find_nearest_committable(cps, 25)
        assert before == 10
        assert after == 30

    def test_exact_match_returns_self_as_before(self):
        cps = [
            CommittableCheckpoint(sequence=10, stage="stt"),
            CommittableCheckpoint(sequence=30, stage="agent"),
        ]
        before, after = find_nearest_committable(cps, 10)
        assert before == 10
        assert after == 30

    def test_no_before(self):
        cps = [CommittableCheckpoint(sequence=30, stage="agent")]
        before, after = find_nearest_committable(cps, 10)
        assert before is None
        assert after == 30

    def test_no_after(self):
        cps = [CommittableCheckpoint(sequence=10, stage="stt")]
        before, after = find_nearest_committable(cps, 50)
        assert before == 10
        assert after is None

    def test_empty(self):
        assert find_nearest_committable([], 5) == (None, None)


# ── check_provider_versions ──────────────────────────────────────


class TestCheckProviderVersions:
    def test_match_empty_list(self, tmp_path):
        path = _write_bundle(
            tmp_path,
            records=[],
            provider_versions={"stt": "openai-1.0"},
        )
        bundle = RunBundle.load(path)
        assert check_provider_versions(bundle, {"stt": "openai-1.0"}) == []

    def test_mismatch(self, tmp_path):
        path = _write_bundle(
            tmp_path,
            records=[],
            provider_versions={"stt": "openai-1.0"},
        )
        bundle = RunBundle.load(path)
        mismatches = check_provider_versions(bundle, {"stt": "openai-2.0"})
        assert len(mismatches) == 1
        assert mismatches[0].provider == "stt"
        assert mismatches[0].bundle_version == "openai-1.0"
        assert mismatches[0].installed_version == "openai-2.0"
        assert mismatches[0].code == "MISMATCH"

    def test_unknown_on_installed_side(self, tmp_path):
        path = _write_bundle(
            tmp_path,
            records=[],
            provider_versions={"stt": "openai-1.0"},
        )
        bundle = RunBundle.load(path)
        mismatches = check_provider_versions(bundle, {"stt": "unknown"})
        assert len(mismatches) == 1
        assert mismatches[0].code == "UNKNOWN"

    def test_unknown_on_bundle_side(self, tmp_path):
        path = _write_bundle(
            tmp_path,
            records=[],
            provider_versions={"stt": "unknown"},
        )
        bundle = RunBundle.load(path)
        mismatches = check_provider_versions(bundle, {"stt": "openai-1.0"})
        assert len(mismatches) == 1
        assert mismatches[0].code == "UNKNOWN"

    def test_provider_not_in_bundle_reported_missing(self, tmp_path):
        path = _write_bundle(
            tmp_path,
            records=[],
            provider_versions={"stt": "openai-1.0"},
        )
        bundle = RunBundle.load(path)
        # Installed has tts but bundle didn't capture it — determinism
        # can't be guaranteed, so it surfaces as a MISSING mismatch.
        mismatches = check_provider_versions(bundle, {"tts": "eleven-v5"})
        assert len(mismatches) == 1
        assert mismatches[0].provider == "tts"
        assert mismatches[0].installed_version == "eleven-v5"
        assert mismatches[0].code == "MISSING"

    def test_dict_version_stringify_is_key_order_stable(self):
        """``version_info()`` may return a dict; the helper stringifies
        via sorted-keys repr so two equivalent dicts compare equal
        regardless of insertion order."""
        from easycat.runtime.replay import _stringify_version

        v1 = {"sdk_version": "1.2", "model": "nova-2"}
        v2 = {"model": "nova-2", "sdk_version": "1.2"}  # reordered
        assert _stringify_version(v1) == _stringify_version(v2)


# ── ReplayRunner integration ─────────────────────────────────────


class TestReplayRunner:
    def _basic_bundle(self, tmp_path: Path) -> RunBundle:
        records = [
            {"sequence": 1, "kind": "event", "name": "turn_started", "turn_id": "t1"},
            {
                "sequence": 2,
                "kind": "event",
                "name": "stage_start",
                "turn_id": "t1",
                "data": {"stage": "stt"},
                "input_ref": _SHA_A,
            },
            {
                "sequence": 3,
                "kind": "event",
                "name": "stage_complete",
                "turn_id": "t1",
                "data": {
                    "stage": "stt",
                    "transcript": "hello world",
                    "timing": {"wall_ns": 123, "stage_ms": 42},
                },
                "output_ref": _SHA_B,
            },
            {"sequence": 4, "kind": "event", "name": "turn_ended", "turn_id": "t1"},
        ]
        path = _write_bundle(
            tmp_path,
            records=records,
            artifacts={_SHA_A: b"audio-in", _SHA_B: b"audio-out"},
        )
        return RunBundle.load(path)

    def test_walks_records_and_attaches_blobs(self, tmp_path):
        bundle = self._basic_bundle(tmp_path)
        result = bundle.replay(_spec())
        assert isinstance(result, ReplayResult)
        assert len(result.frames) == 4
        # Blobs are resolved via artifact_blobs.
        stt_complete = next(f for f in result.frames if f.sequence == 3)
        assert stt_complete.output_blob == b"audio-out"
        stt_start = next(f for f in result.frames if f.sequence == 2)
        assert stt_start.input_blob == b"audio-in"

    def test_timing_fast_masks_nondeterministic(self, tmp_path):
        bundle = self._basic_bundle(tmp_path)
        result = bundle.replay(_spec(timing="fast"))
        stt_complete = next(f for f in result.frames if f.sequence == 3)
        # timing.wall_ns is in REPLAY_IGNORE_FIELDS; stage_ms is not.
        assert "wall_ns" not in stt_complete.data["timing"]
        assert stt_complete.data["timing"]["stage_ms"] == 42

    def test_timing_wall_preserves_nondeterministic(self, tmp_path):
        bundle = self._basic_bundle(tmp_path)
        result = bundle.replay(_spec(timing="wall"))
        stt_complete = next(f for f in result.frames if f.sequence == 3)
        # wall-timing replay keeps every field for interruption debugging.
        assert stt_complete.data["timing"]["wall_ns"] == 123
        assert stt_complete.data["timing"]["stage_ms"] == 42

    def test_stage_filter(self, tmp_path):
        records = [
            {
                "sequence": 1,
                "kind": "event",
                "name": "stage_complete",
                "data": {"stage": "stt"},
            },
            {
                "sequence": 2,
                "kind": "event",
                "name": "stage_complete",
                "data": {"stage": "tts"},
            },
        ]
        bundle = RunBundle.load(_write_bundle(tmp_path, records=records))
        result = bundle.replay(_spec(stage_filter=["tts"]))
        assert [f.sequence for f in result.frames] == [2]

    def test_from_to_sequence_bounds(self, tmp_path):
        records = [{"sequence": i, "kind": "event", "name": "evt"} for i in range(1, 11)]
        bundle = RunBundle.load(_write_bundle(tmp_path, records=records))
        result = bundle.replay(_spec(from_sequence=3, to_sequence=5))
        # NOTE: from_sequence=3 with no replay_entry_points doesn't hit
        # committable validation, so the walk simply slices [3, 5].
        assert [f.sequence for f in result.frames] == [3, 4, 5]


# ── Version-match policy on ReplayRunner ─────────────────────────


class TestVersionMatchPolicy:
    def _bundle(self, tmp_path, versions):
        path = _write_bundle(tmp_path, records=[], provider_versions=versions)
        return RunBundle.load(path)

    def test_match_proceeds(self, tmp_path):
        bundle = self._bundle(tmp_path, {"stt": "v1"})
        result = bundle.replay(
            _spec(fidelity=ReplayFidelity.ARTIFACT), installed_versions={"stt": "v1"}
        )
        assert result.fidelity_label is ReplayFidelity.ARTIFACT

    def test_mismatch_artifact_no_force_raises(self, tmp_path):
        bundle = self._bundle(tmp_path, {"stt": "v1"})
        with pytest.raises(ProviderVersionMismatchError) as exc_info:
            bundle.replay(
                ReplaySpec(fidelity=ReplayFidelity.ARTIFACT),
                installed_versions={"stt": "v2"},
            )
        assert exc_info.value.error_code == "PROVIDER_VERSION_MISMATCH"
        assert len(exc_info.value.mismatches) == 1
        assert exc_info.value.mismatches[0].provider == "stt"

    def test_mismatch_artifact_with_force_downgrades_to_live(self, tmp_path):
        bundle = self._bundle(tmp_path, {"stt": "v1"})
        result = bundle.replay(
            ReplaySpec(fidelity=ReplayFidelity.ARTIFACT, force=True),
            installed_versions={"stt": "v2"},
        )
        assert result.fidelity_label is ReplayFidelity.LIVE

    def test_unknown_version_raises_with_specific_code(self, tmp_path):
        bundle = self._bundle(tmp_path, {"stt": "unknown"})
        with pytest.raises(ProviderVersionMismatchError) as exc_info:
            bundle.replay(
                ReplaySpec(fidelity=ReplayFidelity.ARTIFACT),
                installed_versions={"stt": "v1"},
            )
        assert exc_info.value.error_code == "PROVIDER_VERSION_UNKNOWN"

    def test_mismatch_live_warns_only(self, tmp_path, caplog):
        bundle = self._bundle(tmp_path, {"stt": "v1"})
        with caplog.at_level("WARNING", logger="easycat.runtime.replay"):
            result = bundle.replay(
                ReplaySpec(fidelity=ReplayFidelity.LIVE),
                installed_versions={"stt": "v2"},
            )
        assert result.fidelity_label is ReplayFidelity.LIVE
        assert any("version mismatch" in rec.message for rec in caplog.records)

    def test_missing_provider_artifact_no_force_raises_unknown(self, tmp_path):
        # Bundle never captured the installed provider's version, so
        # ARTIFACT replay must surface it like the UNKNOWN sentinel
        # rather than silently treating it as a match.
        bundle = self._bundle(tmp_path, {"stt": "v1"})
        with pytest.raises(ProviderVersionMismatchError) as exc_info:
            bundle.replay(
                ReplaySpec(fidelity=ReplayFidelity.ARTIFACT),
                installed_versions={"tts": "eleven-v5"},
            )
        assert exc_info.value.error_code == "PROVIDER_VERSION_UNKNOWN"
        assert exc_info.value.mismatches[0].code == "MISSING"

    def test_missing_provider_artifact_with_force_downgrades_to_live(self, tmp_path):
        bundle = self._bundle(tmp_path, {"stt": "v1"})
        result = bundle.replay(
            ReplaySpec(fidelity=ReplayFidelity.ARTIFACT, force=True),
            installed_versions={"tts": "eleven-v5"},
        )
        assert result.fidelity_label is ReplayFidelity.LIVE


# ── Committable-boundary enforcement ─────────────────────────────


class TestCommittableEntryPoint:
    def _bundle_with_checkpoints(self, tmp_path, cps):
        return RunBundle.load(
            _write_bundle(
                tmp_path,
                records=[],
                replay_entry_points=cps,
            )
        )

    def test_entry_on_checkpoint_is_allowed(self, tmp_path):
        bundle = self._bundle_with_checkpoints(
            tmp_path, [{"sequence": 10, "stage": "agent", "unit_id": "u1"}]
        )
        bundle.replay(_spec(from_sequence=10))  # no raise

    def test_entry_off_checkpoint_raises_replay_error(self, tmp_path):
        bundle = self._bundle_with_checkpoints(
            tmp_path,
            [
                {"sequence": 10, "stage": "agent", "unit_id": "u1"},
                {"sequence": 30, "stage": "tts", "unit_id": "u2"},
            ],
        )
        with pytest.raises(ReplayError) as exc_info:
            bundle.replay(_spec(from_sequence=22))
        err = exc_info.value
        assert err.requested_sequence == 22
        assert err.nearest_committable_before == 10
        assert err.nearest_committable_after == 30

    def test_bundle_without_checkpoints_does_not_validate(self, tmp_path):
        bundle = self._bundle_with_checkpoints(tmp_path, [])
        # No checkpoints declared — can't enforce; replay proceeds.
        bundle.replay(_spec(from_sequence=5))


# ── Tool policy enforcement ──────────────────────────────────────


class TestToolPolicyEnforcement:
    def _bundle_with_tool(self, tmp_path):
        records = [
            {"sequence": 1, "kind": "event", "name": "turn_started"},
            {
                "sequence": 2,
                "kind": "framework_transition",
                "name": "tool_call",
                "data": {
                    "phase": "start",
                    "tool_name": "get_weather",
                    "tool_call_id": "c1",
                },
            },
            {
                "sequence": 3,
                "kind": "framework_transition",
                "name": "tool_call",
                "data": {
                    "phase": "result",
                    "tool_name": "get_weather",
                    "tool_call_id": "c1",
                },
            },
        ]
        return RunBundle.load(_write_bundle(tmp_path, records=records))

    def test_deny_blocks_with_descriptor_in_message(self, tmp_path):
        bundle = self._bundle_with_tool(tmp_path)
        with pytest.raises(ReplaySideEffectBlocked) as exc_info:
            bundle.replay(_spec(tool_policy=ToolReplayPolicy.DENY))
        assert "get_weather" in str(exc_info.value)
        assert "c1" in str(exc_info.value)

    def test_stub_records_substitution(self, tmp_path):
        bundle = self._bundle_with_tool(tmp_path)
        result = bundle.replay(_spec(tool_policy=ToolReplayPolicy.STUB))
        assert result.side_effecting is False
        # Both tool-phase records are classified as stubbed.
        assert len(result.stubbed_tool_calls) == 2
        assert "get_weather" in result.stubbed_tool_calls[0]

    def test_allow_marks_result_side_effecting(self, tmp_path, caplog):
        bundle = self._bundle_with_tool(tmp_path)
        with caplog.at_level("WARNING", logger="easycat.runtime.replay"):
            result = bundle.replay(_spec(tool_policy=ToolReplayPolicy.ALLOW))
        assert result.side_effecting is True
        assert len(result.allowed_tool_calls) == 2
        # Per-frame flag is set for allowed tool phases too.
        tool_frames = [f for f in result.frames if f.name == "tool_call"]
        assert all(f.side_effecting for f in tool_frames)
        # ALLOW logs a prominent warning.
        assert any("ALLOW" in rec.message for rec in caplog.records)


# ── ReplaySpec data-class behaviour ──────────────────────────────


class TestReplaySpecBehaviour:
    def test_fidelity_required(self):
        with pytest.raises(TypeError):
            ReplaySpec()  # type: ignore[call-arg]

    def test_tool_policy_default_is_deny(self):
        spec = ReplaySpec(fidelity=ReplayFidelity.ARTIFACT)
        assert spec.tool_policy is ToolReplayPolicy.DENY

    def test_frozen(self):
        spec = ReplaySpec(fidelity=ReplayFidelity.ARTIFACT)
        with pytest.raises(Exception):
            spec.fidelity = ReplayFidelity.LIVE  # type: ignore[misc]


# ── Cassette behaviour (stand-alone, not via runner) ─────────────


class TestReplayCassette:
    def _sample_records(self):
        return (
            {
                "sequence": 2,
                "name": "stage_start",
                "data": {"stage": "stt"},
                "input_ref": _SHA_A,
            },
            {
                "sequence": 3,
                "name": "stage_complete",
                "data": {"stage": "stt", "transcript": "hi"},
                "output_ref": _SHA_B,
            },
        )

    def test_last_record_filters_by_name(self):
        records = self._sample_records()
        cassette = ReplayCassette(stage_name="stt", records=records)
        assert cassette.last_record("stage_start")["sequence"] == 2
        assert cassette.last_record("stage_complete")["sequence"] == 3

    def test_records_named(self):
        records = self._sample_records()
        cassette = ReplayCassette(stage_name="stt", records=records)
        starts = cassette.records_named("stage_start")
        assert len(starts) == 1

    def test_blob_resolver_returns_none_for_missing_ref(self):
        cassette = ReplayCassette(
            stage_name="stt",
            records=(),
            _resolver=lambda ref: None,
        )
        assert cassette.blob(None) is None
        assert cassette.blob("missing") is None


# ── Stage.replay() via cassette ──────────────────────────────────


class TestStageReplayViaCassette:
    def test_stt_artifact_reads_transcript_from_cassette(self, tmp_path):
        from easycat.stages.stt import STTStage

        class _Stub:
            async def send_audio(self, chunk):
                pass

        records = [
            {
                "sequence": 1,
                "name": "stage_start",
                "data": {"stage": "stt"},
                "input_ref": _SHA_A,
            },
            {
                "sequence": 2,
                "name": "stage_complete",
                "data": {"stage": "stt", "transcript": "from cassette"},
                "output_ref": _SHA_B,
            },
        ]
        bundle = RunBundle.load(
            _write_bundle(
                tmp_path,
                records=records,
                artifacts={_SHA_A: b"audio", _SHA_B: b"xyz"},
            )
        )
        stage = STTStage(_Stub())
        cassette = bundle.cassette_for_stage("stt")
        result = stage.replay(_spec(), cassette=cassette)
        assert result == "from cassette"

    def test_tts_artifact_reads_audio_blob(self, tmp_path):
        from easycat.stages.tts import TTSStage

        class _Stub:
            def synthesize(self, text):
                return b"live"

        records = [
            {
                "sequence": 1,
                "name": "stage_complete",
                "data": {"stage": "tts"},
                "output_ref": _SHA_B,
            },
        ]
        bundle = RunBundle.load(
            _write_bundle(tmp_path, records=records, artifacts={_SHA_B: b"replay-audio"})
        )
        stage = TTSStage(_Stub())
        cassette = bundle.cassette_for_stage("tts")
        result = stage.replay(_spec(), cassette=cassette)
        assert result == b"replay-audio"

    def test_stt_live_returns_captured_input_blob(self, tmp_path):
        from easycat.stages.stt import STTStage

        class _Stub:
            async def send_audio(self, chunk):
                pass

        records = [
            {
                "sequence": 1,
                "name": "stage_start",
                "data": {"stage": "stt"},
                "input_ref": _SHA_A,
            },
        ]
        bundle = RunBundle.load(
            _write_bundle(tmp_path, records=records, artifacts={_SHA_A: b"input-audio"})
        )
        stage = STTStage(_Stub())
        cassette = bundle.cassette_for_stage("stt")
        result = stage.replay(_spec(fidelity=ReplayFidelity.LIVE), cassette=cassette)
        assert result == b"input-audio"

    def test_override_wins_over_cassette(self, tmp_path):
        from easycat.stages.stt import STTStage

        class _Stub:
            async def send_audio(self, chunk):
                pass

        records = [
            {
                "sequence": 2,
                "name": "stage_complete",
                "data": {"stage": "stt", "transcript": "cassette"},
            },
        ]
        bundle = RunBundle.load(_write_bundle(tmp_path, records=records))
        stage = STTStage(_Stub())
        cassette = bundle.cassette_for_stage("stt")
        # Explicit override takes precedence.
        result = stage.replay(
            ReplaySpec(
                fidelity=ReplayFidelity.ARTIFACT,
                overrides={"transcript": "override"},
            ),
            cassette=cassette,
        )
        assert result == "override"


# ── Bundle.artifact_blobs round-trip ─────────────────────────────


class TestArtifactBlobsRoundTrip:
    def test_load_populates_artifact_blobs(self, tmp_path):
        blob_a = b"content-A"
        blob_b = b"content-B"
        bundle = RunBundle.load(
            _write_bundle(
                tmp_path,
                records=[],
                artifacts={_SHA_A: blob_a, _SHA_B: blob_b},
            )
        )
        assert bundle.artifact_blobs[_SHA_A] == blob_a
        assert bundle.artifact_blobs[_SHA_B] == blob_b
        # artifact_index is still populated with size info.
        assert bundle.artifact_index[_SHA_A].size_bytes == len(blob_a)

    def test_cassette_resolver_reads_from_blobs(self, tmp_path):
        blob = b"abc"
        bundle = RunBundle.load(
            _write_bundle(
                tmp_path,
                records=[
                    {
                        "sequence": 1,
                        "name": "stage_complete",
                        "data": {"stage": "stt"},
                        "output_ref": _SHA_A,
                    },
                ],
                artifacts={_SHA_A: blob},
            )
        )
        cassette = bundle.cassette_for_stage("stt")
        assert cassette.blob(_SHA_A) == blob


# ── Back-compat: stages.base.ReplaySpec forwards ─────────────────


class TestReplaySpecForward:
    def test_stages_base_forwards(self):
        from easycat.stages.base import ReplaySpec as StageSpec

        assert StageSpec is ReplaySpec

    def test_stages_package_forwards(self):
        from easycat.stages import ReplaySpec as PkgSpec

        assert PkgSpec is ReplaySpec


# ── VersionMismatch equality ─────────────────────────────────────


def test_version_mismatch_is_frozen_dataclass():
    m = VersionMismatch(
        provider="stt", bundle_version="v1", installed_version="v2", code="MISMATCH"
    )
    with pytest.raises(Exception):
        m.provider = "tts"  # type: ignore[misc]
