#!/usr/bin/env python3
"""Demo: run one no-key scripted turn and dump journal records.

No API keys required. The scripted providers drive one local turn with
silent PCM audio so you can inspect the journal without live services.

Setup:
    uv sync --group dev

Run:
    uv run python examples/journal_demo.py
"""

from __future__ import annotations

import asyncio
from collections import Counter

from easycat import EasyConfig, TurnManagerConfig, create_session
from easycat.stubs import scripted_turn_providers


async def main() -> None:
    providers = scripted_turn_providers(
        transcript="Hello, how are you?",
        reply=lambda text: f"I'm doing great! You said: {text}",
    )
    config = EasyConfig.mic(
        transport=providers.transport,
        vad=providers.vad,
        stt=providers.stt,
        agent=providers.agent,
        tts=providers.tts,
        turn_taking=TurnManagerConfig(end_of_turn_silence_ms=1),
        debug="light",
    )
    async with create_session(config) as session:
        await asyncio.sleep(0.5)

    assert session.journal is not None
    records = session.journal.read()

    print(f"{'seq':>4}  {'kind':<24} {'name':<28} data")
    print("-" * 90)
    for r in records:
        data_summary = str(r.data)[:40] if r.data else ""
        print(f"{r.sequence:>4}  {r.kind.value:<24} {r.name:<28} {data_summary}")

    print("\n--- Summary ---")
    by_kind = Counter(r.kind.value for r in records)
    for kind, count in sorted(by_kind.items()):
        print(f"  {kind}: {count} records")
    print(f"  total: {len(records)} records")


if __name__ == "__main__":
    asyncio.run(main())
