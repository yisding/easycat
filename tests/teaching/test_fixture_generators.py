from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

from easycat.debug.bundle import FORMAT_VERSION
from easycat.debug.testing import load_bundle
from tests.teaching import _script_runner as script_runner

ROOT = Path(__file__).resolve().parents[2]
TEACHING = ROOT / "docs" / "teaching"


def _tracked_bundle_bytes(chapter: Path) -> dict[Path, bytes]:
    return {path.relative_to(chapter): path.read_bytes() for path in chapter.rglob("*.bundle")}


def _run_generator(chapter: Path, output_root: Path) -> str:
    result = script_runner.run(
        [
            sys.executable,
            str(chapter / "generate_bundles.py"),
            "--output-root",
            str(output_root),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def test_chapter_11_generator_can_write_an_isolated_chapter_root(tmp_path: Path) -> None:
    chapter = TEACHING / "11-journal"
    before = _tracked_bundle_bytes(chapter)
    output_root = tmp_path / "11-journal"

    stdout = _run_generator(chapter, output_root)

    generated = sorted((output_root / "bundles").glob("*.bundle"))
    assert len(generated) == 3
    assert all(list(load_bundle(path).records()) for path in generated)
    assert "Building planted-bug bundles" in stdout
    assert _tracked_bundle_bytes(chapter) == before


def test_chapter_12_generator_can_write_an_isolated_chapter_root(tmp_path: Path) -> None:
    chapter = TEACHING / "12-evals-and-latency"
    before = _tracked_bundle_bytes(chapter)
    output_root = tmp_path / "12-evals-and-latency"

    stdout = _run_generator(chapter, output_root)

    generated = sorted((output_root / "bundles").glob("*.bundle"))
    golden = sorted((output_root / "bundles" / "golden").glob("*.bundle"))
    assert len(generated) == 6
    assert len(golden) == 3
    assert (output_root / "ground_truth.csv").is_file()
    assert (output_root / "bundles" / "golden" / "ground_truth.csv").is_file()
    assert all(list(load_bundle(path).records()) for path in [*generated, *golden])
    assert "Expected WERs: 5.0%, 10.0%, 25.0%, aggregate 10.5%." in stdout
    assert _tracked_bundle_bytes(chapter) == before


def test_fixture_generator_docs_use_ignored_output_roots() -> None:
    for slug in ("11-journal", "12-evals-and-latency"):
        chapter = TEACHING / slug
        readme = (chapter / "README.md").read_text(encoding="utf-8")
        script = (chapter / "generate_bundles.py").read_text(encoding="utf-8")
        normalized_readme = " ".join(readme.split())
        output_root = f"--output-root .easycat/teaching/{slug}"

        assert output_root in readme
        assert output_root in script
        assert "omit `--output-root`" in normalized_readme


def test_checked_in_teaching_bundles_use_the_current_export_shape() -> None:
    bundles = sorted((TEACHING / "11-journal" / "bundles").glob("*.bundle"))
    bundles.extend(sorted((TEACHING / "12-evals-and-latency" / "bundles").rglob("*.bundle")))

    assert len(bundles) == 12
    for path in bundles:
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            records = [json.loads(line) for line in archive.read("journal.ndjson").splitlines()]

        assert manifest["format_version"] == FORMAT_VERSION, path
        assert records, path
        for record in records:
            location = f"{path}: sequence {record.get('sequence')}"
            assert record["kind"] == "event", location
            assert "op_id" not in record, location
            assert "queue_ns" not in record["timing"], location
