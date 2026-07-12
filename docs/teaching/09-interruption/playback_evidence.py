"""Show which playback-progress evidence two teaching transports expose."""

from __future__ import annotations

import json
from typing import Any

from easycat.runtime.capabilities import playback_acknowledgements
from easycat.transports.local import LocalTransport
from easycat.transports.twilio_media import TwilioTransport


def describe(transport: Any) -> dict[str, object]:
    delivery_callbacks = bool(getattr(transport, "reports_audio_delivery", False))
    playback_marks = playback_acknowledgements(transport) is not None
    evidence: list[str] = []
    if delivery_callbacks:
        evidence.append("TransportAudioDelivered")
    if playback_marks:
        evidence.append("PlaybackMarkAck")
    return {
        "transport_class": type(transport).__name__,
        "reports_audio_delivery": delivery_callbacks,
        "supports_playback_marks": playback_marks,
        "progress_evidence": evidence,
    }


def probe() -> dict[str, object]:
    return {
        "local": describe(LocalTransport()),
        "twilio": describe(TwilioTransport()),
        "human_ear_ground_truth": False,
    }


if __name__ == "__main__":
    print(json.dumps(probe(), indent=2, sort_keys=True))
