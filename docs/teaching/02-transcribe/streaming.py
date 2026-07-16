"""Chapter 2 — streaming transcription.

Open a mic transport, stream audio into an STT provider, and print
partial + final transcripts with timestamps as they arrive. Writes a
debug bundle to ``runs/``.

Dependencies:
    uv sync --extra quickstart --group dev
    uv sync --extra quickstart --extra deepgram --group dev  # for --provider deepgram
    export OPENAI_API_KEY=...
    export DEEPGRAM_API_KEY=...  # for --provider deepgram
    uv run easycat doctor
    uv run easycat doctor --env-file .env         # if keys live in .env
    uv run easycat doctor --env-file .env --json  # for parseable checks
    Add `--env-file .env` after `uv run` on script commands if keys live in `.env`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
import types
from pathlib import Path

from easycat import LocalTransportConfig
from easycat.audio_format import PCM16_MONO_24K
from easycat.debug.export import export_debug_bundle
from easycat.events import EventBus, STTEventType
from easycat.runtime import InMemoryRingBuffer, JournalRecordKind
from easycat.runtime.capabilities import close_if_supported
from easycat.stt.factory import STTProviderConfig, create_stt_provider
from easycat.transports.local import LocalTransport

DURATION_S = 5
RUNS_DIR = Path(__file__).parent / "runs"
PROVIDERS = ("openai", "deepgram")
PROVIDER_ENV_VARS = {
    "openai": "OPENAI_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
}
PROVIDER_TIMING = {
    "openai": "after_stream_end",
    "deepgram": "during_audio",
}


def _display_path(path: Path) -> Path:
    try:
        return path.relative_to(Path.cwd())
    except ValueError:
        return path


def build_stt_config(provider: str) -> STTProviderConfig:
    """Resolve one documented provider without leaking its credential."""
    if provider not in PROVIDER_ENV_VARS:
        choices = ", ".join(PROVIDERS)
        raise ValueError(f"Unknown STT provider {provider!r}; choose one of: {choices}")

    env_var = PROVIDER_ENV_VARS[provider]
    api_key = os.getenv(env_var)
    if not api_key:
        raise SystemExit(f"Set {env_var} in your environment first.")

    params = None
    if provider == "deepgram":
        # This is Deepgram's wire target, not a restriction on upstream PCM.
        # Matching the transport avoids a resample in this comparison; the
        # provider also accepts other PCM rates and resamples them internally.
        params = {
            "sample_rate": PCM16_MONO_24K.sample_rate,
            "event_bus": EventBus(),
        }

    return STTProviderConfig(provider=provider, api_key=api_key, params=params)


async def shutdown(stt, transport, *, needs_stream_end: bool) -> None:
    """End an active stream once, then close its provider and transport."""
    try:
        if needs_stream_end:
            await stt.end_stream()
    finally:
        try:
            await close_if_supported(stt)
        finally:
            await transport.disconnect()


async def main(provider: str = "openai") -> None:
    config = build_stt_config(provider)
    session_id = f"ch02-streaming-{provider}-{int(time.time())}"

    journal = InMemoryRingBuffer(capacity=10_000)
    # The same STT factory from batch.py — the CLI changes only its config.
    # The start/send/events consumer below is provider-independent.
    stt = create_stt_provider(config)

    # LocalTransport's 24 kHz pipeline rate matches chapters 3+.
    transport = LocalTransport(LocalTransportConfig(audio_format=PCM16_MONO_24K))

    journal.append(
        kind=JournalRecordKind.EVENT,
        name="stt.provider.selected",
        session_id=session_id,
        data={
            "provider": provider,
            "credential_env": PROVIDER_ENV_VARS[provider],
            "event_timing": PROVIDER_TIMING[provider],
            "input_sample_rate_hz": PCM16_MONO_24K.sample_rate,
            "provider_target_sample_rate_hz": (
                PCM16_MONO_24K.sample_rate if provider == "deepgram" else None
            ),
        },
    )

    await transport.connect()
    await stt.start_stream()
    stream_end_started = False
    start = time.monotonic()
    print(f"Speak for {DURATION_S} seconds...")

    async def feed_audio() -> None:
        """Push mic chunks into STT until DURATION_S seconds elapse."""
        nonlocal stream_end_started
        async for chunk in transport.receive_audio():
            await stt.send_audio(chunk)
            if time.monotonic() - start >= DURATION_S:
                break
        # Closing the STT stream is what triggers the upload (for
        # OpenAI's batch provider) or the final commit (for Deepgram).
        # For OpenAI this call blocks for the full round-trip: the
        # partials you see start arriving *after* we get here.
        stream_end_started = True
        await stt.end_stream()

    async def consume_events() -> None:
        """Print every partial / final as soon as it arrives."""
        async for event in stt.events():
            offset_ms = (time.monotonic() - start) * 1000
            kind = "FINAL" if event.type == STTEventType.FINAL else "part "
            print(f"  t+{offset_ms:6.0f}ms  [{kind}] {event.text}")
            journal.append(
                kind=JournalRecordKind.EVENT,
                name=f"stt.{event.type.value}",
                session_id=session_id,
                data={
                    "stage": "stt",
                    "event_type": event.type.value,
                    "text": event.text,
                    "offset_ms": offset_ms,
                    # t_ms mirrors the later chapters' field so downstream
                    # scripts (ch 12's evals.py, etc.) can read this bundle
                    # without a translator.
                    "t_ms": time.monotonic() * 1000,
                },
            )

    try:
        await asyncio.gather(feed_audio(), consume_events())
    finally:
        await shutdown(stt, transport, needs_stream_end=not stream_end_started)

    RUNS_DIR.mkdir(exist_ok=True)
    bundle_path = RUNS_DIR / f"{session_id}.bundle"
    session_stub = types.SimpleNamespace(journal=journal)
    export_debug_bundle(session_stub, bundle_path, overwrite=True)
    print(f"\nWrote bundle → {_display_path(bundle_path)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        choices=PROVIDERS,
        default="openai",
        help="STT provider: OpenAI batches locally; Deepgram emits during speech.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args().provider))
