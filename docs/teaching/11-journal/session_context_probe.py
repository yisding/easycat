"""Show when a turn query needs same-session unscoped context.

Run with::

    uv run python docs/teaching/11-journal/session_context_probe.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from easycat.debug.testing import load_bundle


def load_investigator():
    path = Path(__file__).with_name("investigate.py")
    spec = importlib.util.spec_from_file_location("teaching_ch11_context_probe", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def probe() -> dict[str, object]:
    investigator = load_investigator()
    path = Path(__file__).parent / "bundles" / "bug_03_ghost_interruption.bundle"
    bundle = load_bundle(path)
    turn = "ch11-bug03-turn-2"
    strict = investigator.query_records(bundle, turn=turn)
    with_context = investigator.query_records(bundle, turn=turn, include_session_context=True)
    audio_context = investigator.query_records(
        bundle,
        turn=turn,
        stage="audio",
        include_session_context=True,
    )
    return {
        "audio_context": [
            {
                "aec": record["data"]["aec"],
                "name": record["name"],
                "sequence": record["sequence"],
                "session_id": record["session_id"],
                "turn_id": record["turn_id"],
            }
            for record in audio_context
        ],
        "strict_turn_sequences": [record["sequence"] for record in strict],
        "with_context_sequences": [record["sequence"] for record in with_context],
    }


if __name__ == "__main__":
    print(json.dumps(probe(), indent=2, sort_keys=True))
