"""Unit tests for :mod:`easycat.runtime.replay`.

These cover the pure helpers and the ``ReplayRunner`` walk behaviour —
provider-version match (AC4.21), ``mask_nondeterministic``,
committable-boundary enforcement (T4.8), tool-policy enforcement
(AC4.24), and the three fidelity downgrade paths.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from easycat.debug.bundle import (
    FORMAT_VERSION,
    BundleValidationError,
    CommittableCheckpoint,
    RunBundle,
)
from easycat.runtime.replay import (
    REPLAY_IGNORE_FIELDS,
    ProviderVersionMismatchError,
    ReplayCassette,
    ReplayDivergenceError,
    ReplayError,
    ReplayFidelity,
    ReplayResult,
    ReplayRunner,
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


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _spec(**overrides) -> ReplaySpec:
    """ReplaySpec factory with a sensible fidelity default for tests."""
    overrides.setdefault("fidelity", ReplayFidelity.ARTIFACT)
    return ReplaySpec(**overrides)


@pytest.mark.parametrize("sample_rate", [[16000], float("inf")])
def test_audio_replay_coerces_malformed_pcm_metadata(tmp_path, sample_rate):
    """Optional audio metadata cannot make either replay path crash."""
    audio = b"audio"
    audio_ref = _sha256(audio)
    for name, stage, ref_key, method in (
        ("stage_start", "stt", "input_ref", "replay_stt_audio"),
        ("tts_frame", "tts", "output_ref", "replay_audio"),
    ):
        bundle = RunBundle.load(
            _write_bundle(
                tmp_path,
                records=[
                    {
                        "sequence": 1,
                        "name": name,
                        "data": {"stage": stage, "sample_rate": sample_rate},
                        ref_key: audio_ref,
                    }
                ],
                artifacts={audio_ref: audio},
            )
        )

        chunks = getattr(bundle, method)()

        assert len(chunks) == 1
        assert chunks[0].sample_rate == 0


def test_tts_audio_replay_coerces_nonfinite_duration(tmp_path):
    """Duration metadata remains safe for callers that schedule replay chunks."""
    audio = b"audio"
    audio_ref = _sha256(audio)
    bundle = RunBundle.load(
        _write_bundle(
            tmp_path,
            records=[
                {
                    "sequence": 1,
                    "name": "tts_frame",
                    "data": {"stage": "tts", "duration_ms": float("inf")},
                    "output_ref": audio_ref,
                }
            ],
            artifacts={audio_ref: audio},
        )
    )

    chunks = bundle.replay_audio()

    assert chunks[0].duration_ms == 0.0


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

    def test_empty_installed_version_matches_empty_bundle_version(self, tmp_path):
        path = _write_bundle(
            tmp_path,
            records=[],
            provider_versions={"custom.provider": ""},
        )
        bundle = RunBundle.load(path)
        assert check_provider_versions(bundle, {"custom.provider": ""}) == []

    def test_none_installed_version_is_unknown(self, tmp_path):
        path = _write_bundle(
            tmp_path,
            records=[],
            provider_versions={"custom.provider": ""},
        )
        bundle = RunBundle.load(path)
        mismatches = check_provider_versions(bundle, {"custom.provider": None})
        assert len(mismatches) == 1
        assert mismatches[0].installed_version == "unknown"
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
        input_blob = b"audio-in"
        output_blob = b"audio-out"
        input_ref = _sha256(input_blob)
        output_ref = _sha256(output_blob)
        records = [
            {"sequence": 1, "kind": "event", "name": "turn_started", "turn_id": "t1"},
            {
                "sequence": 2,
                "kind": "event",
                "name": "stage_start",
                "turn_id": "t1",
                "data": {"stage": "stt"},
                "input_ref": input_ref,
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
                "output_ref": output_ref,
            },
            {"sequence": 4, "kind": "event", "name": "turn_ended", "turn_id": "t1"},
        ]
        path = _write_bundle(
            tmp_path,
            records=records,
            artifacts={input_ref: input_blob, output_ref: output_blob},
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
        assert len(result.stage_replays) == 1
        replayed = result.stage_replays[0]
        assert replayed.stage == "stt"
        assert replayed.turn_id == "t1"
        assert replayed.output == "hello world"
        assert replayed.matches_recording is True

    def test_custom_stage_replayer_is_compared_with_recording(self, tmp_path):
        bundle = self._basic_bundle(tmp_path)
        calls = []

        def replay_stt(spec, cassette):
            calls.append((spec.fidelity, cassette.stage_name))
            return "hello world"

        result = bundle.replay(_spec(), stage_replayers={"stt": replay_stt})

        assert calls == [(ReplayFidelity.ARTIFACT, "stt")]
        assert result.stage_replays[0].matches_recording is True

    def test_live_builtin_exposes_input_without_comparing_it_to_output(self, tmp_path):
        bundle = self._basic_bundle(tmp_path)

        result = bundle.replay(_spec(fidelity=ReplayFidelity.LIVE))

        replayed = result.stage_replays[0]
        assert replayed.output == b"audio-in"
        assert replayed.matches_recording is None

    def test_live_custom_replayer_is_compared_with_recorded_artifact(self, tmp_path):
        bundle = self._basic_bundle(tmp_path)

        result = bundle.replay(
            _spec(fidelity=ReplayFidelity.LIVE),
            stage_replayers={"stt": lambda _spec, _cassette: "hello world"},
        )

        assert result.stage_replays[0].matches_recording is True

    def test_stage_replay_divergence_raises_e403(self, tmp_path):
        bundle = self._basic_bundle(tmp_path)

        with pytest.raises(ReplayDivergenceError) as exc_info:
            bundle.replay(
                _spec(),
                stage_replayers={"stt": lambda _spec, _cassette: "changed"},
            )

        error = exc_info.value
        assert error.code == "EASYCAT_E403"
        assert error.stage == "stt"
        assert error.turn_id == "t1"
        assert error.expected_digest != error.actual_digest

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

    def test_timing_wall_replays_recorded_delays(self, tmp_path):
        records = [
            {
                "sequence": 1,
                "kind": "event",
                "name": "first",
                "timing": {"mono_ns": 1_000_000_000},
            },
            {
                "sequence": 2,
                "kind": "event",
                "name": "second",
                "timing": {"mono_ns": 1_125_000_000},
            },
            {
                "sequence": 3,
                "kind": "event",
                "name": "third",
                "timing": {"mono_ns": 1_175_000_000},
            },
        ]
        bundle = RunBundle.load(_write_bundle(tmp_path, records=records))
        delays = []

        result = ReplayRunner(bundle, _spec(timing="wall"), sleep=delays.append).run()

        assert len(result.frames) == 3
        assert delays == [0.125, 0.05]

    def test_timing_wall_skips_delay_across_clock_source_switch(self, tmp_path):
        """A mono_ns→wall_ns (or back) transition is not a real recorded gap."""
        records = [
            {"sequence": 1, "kind": "event", "name": "a", "timing": {"mono_ns": 1_000_000_000}},
            {"sequence": 2, "kind": "event", "name": "b", "timing": {"mono_ns": 1_125_000_000}},
            # Crash-dump projection: only a wall clock, epoch-scale and far
            # ahead of the monotonic values above.
            {"sequence": 3, "kind": "event", "name": "c", "wall_ns": 1_700_000_000_000_000_000},
            {"sequence": 4, "kind": "event", "name": "d", "wall_ns": 1_700_000_000_050_000_000},
            {"sequence": 5, "kind": "event", "name": "e", "timing": {"mono_ns": 9_000_000_000}},
            {"sequence": 6, "kind": "event", "name": "f", "timing": {"mono_ns": 9_075_000_000}},
        ]
        bundle = RunBundle.load(_write_bundle(tmp_path, records=records))
        delays = []

        result = ReplayRunner(bundle, _spec(timing="wall"), sleep=delays.append).run()

        assert len(result.frames) == 6
        # Same-source neighbours keep their bounded delay; the two source
        # switches (2→3 and 4→5) contribute no sleep at all.
        assert delays == [0.125, 0.05, 0.075]

    def test_timing_wall_caps_untrusted_recorded_gap(self, tmp_path):
        records = [
            {
                "sequence": 1,
                "kind": "event",
                "name": "first",
                "timing": {"mono_ns": 1},
            },
            {
                "sequence": 2,
                "kind": "event",
                "name": "second",
                "timing": {"mono_ns": 10**100},
            },
        ]
        bundle = RunBundle.load(_write_bundle(tmp_path, records=records))
        delays = []

        ReplayRunner(bundle, _spec(timing="wall"), sleep=delays.append).run()

        assert delays == [30.0]

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

    def test_rejects_records_with_malformed_sequences_before_replay(self, tmp_path):
        records = [
            {"sequence": "2", "kind": "event", "name": "bad-string"},
            {"sequence": True, "kind": "event", "name": "bad-bool"},
            {"sequence": 3, "kind": "event", "name": "ok"},
        ]

        with pytest.raises(BundleValidationError) as exc_info:
            RunBundle.load(_write_bundle(tmp_path, records=records))

        assert exc_info.value.reason_code == "INVALID_JOURNAL"

    def test_normalizes_unhashable_turn_id_before_stage_grouping(self, tmp_path):
        records = [
            {
                "sequence": 1,
                "kind": "event",
                "name": "stage_complete",
                "turn_id": ["untrusted"],
                "data": {"stage": "stt", "transcript": "hello"},
            }
        ]
        bundle = RunBundle.load(_write_bundle(tmp_path, records=records))

        result = bundle.replay(_spec())

        assert result.frames[0].turn_id is None
        assert result.stage_replays[0].turn_id is None
        assert result.stage_replays[0].output == "hello"


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

    def test_malformed_tool_record_is_rejected_before_policy_evaluation(self, tmp_path):
        records = [
            {
                "sequence": "2",
                "kind": "framework_transition",
                "name": "tool_call",
                "data": {
                    "phase": "start",
                    "tool_name": "get_weather",
                    "tool_call_id": "c1",
                },
            },
        ]
        with pytest.raises(BundleValidationError) as exc_info:
            RunBundle.load(_write_bundle(tmp_path, records=records))

        assert exc_info.value.reason_code == "INVALID_JOURNAL"

    def test_stub_records_substitution(self, tmp_path):
        bundle = self._bundle_with_tool(tmp_path)
        result = bundle.replay(_spec(tool_policy=ToolReplayPolicy.STUB))
        assert result.side_effecting is False
        # Both tool-phase records are classified as stubbed.
        assert len(result.stubbed_tool_calls) == 2
        assert "get_weather" in result.stubbed_tool_calls[0]

    def test_allow_without_executor_is_not_reported_as_side_effecting(self, tmp_path, caplog):
        bundle = self._bundle_with_tool(tmp_path)
        with caplog.at_level("WARNING", logger="easycat.runtime.replay"):
            result = bundle.replay(_spec(tool_policy=ToolReplayPolicy.ALLOW))
        assert result.side_effecting is False
        assert len(result.allowed_tool_calls) == 2
        assert result.executed_tool_calls == []
        tool_frames = [f for f in result.frames if f.name == "tool_call"]
        assert not any(f.side_effecting for f in tool_frames)
        assert any("no tool executor" in rec.message for rec in caplog.records)

    def test_allow_executes_start_phase_once_when_executor_is_supplied(self, tmp_path, caplog):
        bundle = self._bundle_with_tool(tmp_path)
        executed_records = []

        with caplog.at_level("WARNING", logger="easycat.runtime.replay"):
            result = bundle.replay(
                _spec(tool_policy=ToolReplayPolicy.ALLOW),
                tool_executor=executed_records.append,
            )

        assert [record["data"]["phase"] for record in executed_records] == ["start"]
        assert result.side_effecting is True
        assert result.executed_tool_calls == ["get_weather(c1)"]
        tool_frames = [f for f in result.frames if f.name == "tool_call"]
        assert [f.side_effecting for f in tool_frames] == [True, False]
        assert any("result is side-effecting" in rec.message for rec in caplog.records)

    @pytest.mark.parametrize("sequence", [None, "2", True])
    def test_allow_rejects_tool_with_malformed_sequence_before_execution(
        self,
        tmp_path,
        sequence,
    ):
        records = [
            {
                "sequence": sequence,
                "kind": "framework_transition",
                "name": "tool_call",
                "data": {
                    "phase": "start",
                    "tool_name": "get_weather",
                    "tool_call_id": "c1",
                },
            }
        ]

        with pytest.raises(BundleValidationError) as exc_info:
            RunBundle.load(_write_bundle(tmp_path, records=records))

        assert exc_info.value.reason_code == "INVALID_JOURNAL"


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

        input_blob = b"audio"
        output_blob = b"xyz"
        input_ref = _sha256(input_blob)
        output_ref = _sha256(output_blob)
        records = [
            {
                "sequence": 1,
                "name": "stage_start",
                "data": {"stage": "stt"},
                "input_ref": input_ref,
            },
            {
                "sequence": 2,
                "name": "stage_complete",
                "data": {"stage": "stt", "transcript": "from cassette"},
                "output_ref": output_ref,
            },
        ]
        bundle = RunBundle.load(
            _write_bundle(
                tmp_path,
                records=records,
                artifacts={input_ref: input_blob, output_ref: output_blob},
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

        audio = b"replay-audio"
        audio_ref = _sha256(audio)
        records = [
            {
                "sequence": 1,
                "name": "stage_complete",
                "data": {"stage": "tts"},
                "output_ref": audio_ref,
            },
        ]
        bundle = RunBundle.load(
            _write_bundle(tmp_path, records=records, artifacts={audio_ref: audio})
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

        audio = b"input-audio"
        audio_ref = _sha256(audio)
        records = [
            {
                "sequence": 1,
                "name": "stage_start",
                "data": {"stage": "stt"},
                "input_ref": audio_ref,
            },
        ]
        bundle = RunBundle.load(
            _write_bundle(tmp_path, records=records, artifacts={audio_ref: audio})
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
        ref_a = _sha256(blob_a)
        ref_b = _sha256(blob_b)
        bundle = RunBundle.load(
            _write_bundle(
                tmp_path,
                records=[],
                artifacts={ref_a: blob_a, ref_b: blob_b},
            )
        )
        assert bundle.artifact_blobs[ref_a] == blob_a
        assert bundle.artifact_blobs[ref_b] == blob_b
        # artifact_index is still populated with size info.
        assert bundle.artifact_index[ref_a].size_bytes == len(blob_a)

    def test_cassette_resolver_reads_from_blobs(self, tmp_path):
        blob = b"abc"
        ref = _sha256(blob)
        bundle = RunBundle.load(
            _write_bundle(
                tmp_path,
                records=[
                    {
                        "sequence": 1,
                        "name": "stage_complete",
                        "data": {"stage": "stt"},
                        "output_ref": ref,
                    },
                ],
                artifacts={ref: blob},
            )
        )
        cassette = bundle.cassette_for_stage("stt")
        assert cassette.blob(ref) == blob


# ── ReplaySpec re-export: package-level only, not stages.base ─────


class TestReplaySpecForward:
    def test_stages_base_no_longer_forwards_replayspec(self):
        import easycat.stages.base as stages_base

        with pytest.raises(AttributeError):
            stages_base.ReplaySpec

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
