"""Guard the ladder's sample-rate and duplex fundamentals against overclaims."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEACHING = ROOT / "docs" / "teaching"


def test_chapter_00_separates_nyquist_boundary_from_quality() -> None:
    readme = (TEACHING / "00-hello-audio" / "README.md").read_text(encoding="utf-8")
    exercises = (TEACHING / "00-hello-audio" / "EXERCISES.md").read_text(encoding="utf-8")
    lesson = " ".join(f"{readme}\n{exercises}".split())

    assert "ideally band-limited signal" in lesson
    assert "theoretical upper boundary" in lesson
    assert "sample rate alone is not a quality guarantee" in lesson
    assert "300–3400 Hz" in lesson
    assert "ITU-T P.342's G.711 profile" in lesson
    assert "ITU-T G.722" in lesson
    assert "reconstruct speech perfectly" not in lesson
    assert "Human speech energy stops around 8 kHz" not in lesson
    assert "pure bandwidth cost for no intelligibility gain" not in lesson


def test_chapter_10_treats_duplex_as_terminal_behavior() -> None:
    readme = (TEACHING / "10-cleaning-signal" / "README.md").read_text(encoding="utf-8")
    normalized = " ".join(readme.split())

    assert "full-, partial-, or no-duplex capability" in normalized
    assert "ITU-T P.340" in readme
    assert "AEC is a key enabler" in readme
    assert "does not change the network into a half-duplex transport" in normalized
    assert "Speakerphone hardware is not inherently half-duplex" in normalized
    assert "A regular telephone speakerphone is half-duplex by hardware" not in normalized


def test_ladder_index_names_duplex_behavior_not_half_duplex_requirement() -> None:
    index = (TEACHING / "README.md").read_text(encoding="utf-8")

    assert "Noise reduction, AEC, duplex behavior." in index
    assert "Noise reduction, AEC, half-duplex." not in index
