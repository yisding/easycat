"""Distinguish a real absence from a broken journal query.

Run with::

    uv run python docs/teaching/11-journal/query_coverage_probe.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from easycat.debug.testing import load_bundle


def load_investigator():
    path = Path(__file__).with_name("investigate.py")
    spec = importlib.util.spec_from_file_location("teaching_ch11_query_probe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    investigator = load_investigator()
    path = Path(__file__).parent / "bundles" / "bug_03_ghost_interruption.bundle"
    bundle = load_bundle(path)
    all_records = list(bundle.records())

    valid = investigator.query_records(
        bundle,
        turn="ch11-bug03-turn-2",
        stage="stt",
        name="stt.final",
    )
    typo = investigator.query_diagnostics(all_records, turn="typo")
    impossible = investigator.query_diagnostics(all_records, stage="agent", sequence=9)

    payload = {
        "impossible_intersection": {
            "combined_matches": len(investigator.query_records(bundle, stage="agent", sequence=9)),
            "marginal_matches": impossible["marginal_matches"],
        },
        "total_records": len(all_records),
        "turn_typo": {
            "known_turns": typo["known_turns"],
            "marginal_matches": typo["marginal_matches"],
        },
        "valid_combined_query": [record["sequence"] for record in valid],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
