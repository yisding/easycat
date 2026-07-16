"""Build Chapter 13's provider × transport configuration matrix without providers.

Run with::

    uv run python docs/teaching/13-swap-providers-and-transports/matrix_probe.py
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import main as chapter

PROVIDER_MIXES = ("openai", "deepgram-eleven")
TRANSPORTS = ("local", "webrtc", "twilio")


def probe() -> dict[str, object]:
    with patch.dict(
        os.environ,
        {"DEEPGRAM_API_KEY": "probe", "ELEVENLABS_API_KEY": "probe"},
    ):
        mixes = {name: chapter.provider_mix(name) for name in PROVIDER_MIXES}
    transports = {name: type(chapter.transport_config(name)).__name__ for name in TRANSPORTS}
    cells = [
        {
            "tag": f"{provider}-{transport}",
            "stt": mixes[provider]["stt"],
            "tts": mixes[provider]["tts"],
            "transport_config": transports[transport],
        }
        for provider in PROVIDER_MIXES
        for transport in TRANSPORTS
    ]
    return {
        "provider_mixes": mixes,
        "transports": transports,
        "cells": cells,
        "cell_count": len(cells),
        "provider_axis_reused_across_transports": all(
            cell["stt"] == mixes[provider]["stt"] and cell["tts"] == mixes[provider]["tts"]
            for provider in PROVIDER_MIXES
            for cell in cells
            if cell["tag"].startswith(f"{provider}-")
        ),
        "transport_axis_reused_across_provider_mixes": all(
            cell["transport_config"] == transports[transport]
            for transport in TRANSPORTS
            for cell in cells
            if cell["tag"].endswith(f"-{transport}")
        ),
    }


if __name__ == "__main__":
    print(json.dumps(probe(), indent=2, sort_keys=True))
