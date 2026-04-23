"""Telephony helpers used standalone (no live Twilio required).

``twilio_app.py`` wires several telephony helpers together inside a live
Media Streams session.  This example exercises the same helpers against a
plain ``EventBus`` with synthetic events so you can understand them in
isolation — useful when you are writing tests, debugging, or deciding
which helper you need.

Covered here:

  * ``DTMFAggregator`` — accumulates individual DTMF digits and emits a
    single ``DTMFAggregated`` event on terminator, max length, or idle
    timeout.
  * ``VoicemailDetector`` — heuristic detector that flags a call as
    voicemail when VAD reports one continuous monologue longer than a
    threshold.
  * ``IVRNavigator.classify_ivr_prompt`` / ``detect_human_after_ivr`` —
    pure text classifiers used by the navigator to decide whether an
    incoming STT transcript is an IVR prompt or a human greeting.

Setup:
  uv sync --extra quickstart
  uv run python examples/telephony_helpers.py

This example does NOT need any API keys or network access.
"""

from __future__ import annotations

import asyncio

from easycat.events import (
    DTMF,
    DTMFAggregated,
    EventBus,
    VADStartSpeaking,
    VADStopSpeaking,
    VoicemailDetected,
)
from easycat.telephony.dtmf import DTMFAggregator, DTMFAggregatorConfig
from easycat.telephony.ivr import classify_ivr_prompt, detect_human_after_ivr
from easycat.telephony.voicemail import VoicemailDetector, VoicemailDetectorConfig


async def demo_dtmf() -> None:
    print("── DTMFAggregator ──")
    bus = EventBus()

    emitted: list[str] = []

    async def on_aggregated(event: DTMFAggregated) -> None:
        emitted.append(event.sequence)
        print(f"  aggregated: {event.sequence!r}")

    bus.subscribe(DTMFAggregated, on_aggregated)

    aggregator = DTMFAggregator(bus, DTMFAggregatorConfig(timeout_ms=150))
    aggregator.start()

    # Caller types "1234#" — the terminator forces immediate emission.
    for digit in "1234#":
        await bus.emit(DTMF(digit=digit))

    # Caller later types "42" and pauses — idle timeout emits after 150 ms.
    for digit in "42":
        await bus.emit(DTMF(digit=digit))
    await asyncio.sleep(0.25)

    aggregator.stop()
    assert emitted == ["1234#", "42"], emitted
    print(f"  sequences: {emitted}\n")


async def demo_voicemail() -> None:
    print("── VoicemailDetector ──")
    bus = EventBus()

    flagged: list[VoicemailDetected] = []

    async def on_detected(event: VoicemailDetected) -> None:
        flagged.append(event)
        print(f"  detected: result={event.result!r} source={event.source!r}")

    bus.subscribe(VoicemailDetected, on_detected)

    detector = VoicemailDetector(bus, VoicemailDetectorConfig(monologue_threshold_s=2.0))
    detector.start()

    # Simulate a 3-second monologue (longer than the 2 s threshold).
    await bus.emit(VADStartSpeaking(timestamp=0.0))
    await bus.emit(VADStopSpeaking(timestamp=3.0))

    detector.stop()
    assert flagged and flagged[0].result == "machine", flagged
    print()


def demo_ivr_text_classifiers() -> None:
    print("── IVR text classifiers ──")

    samples = [
        "Press 1 for sales or press 2 for support.",
        "Hello, this is Alex from accounting. How can I help you?",
        "Please hold while we connect you to an agent.",
    ]
    for text in samples:
        is_ivr = classify_ivr_prompt(text)
        is_human = detect_human_after_ivr(text)
        print(f"  {text!r}\n    is_ivr_prompt={is_ivr}  human_after_ivr={is_human}")
    print()


async def main() -> None:
    await demo_dtmf()
    await demo_voicemail()
    demo_ivr_text_classifiers()


if __name__ == "__main__":
    asyncio.run(main())
