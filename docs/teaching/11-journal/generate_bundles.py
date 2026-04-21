"""Build the three planted-bug bundles for chapter 11.

Run this once; the resulting ``.bundle`` files are checked into
``bundles/``. You should not need to rerun it unless you want to
regenerate the fixtures.

    uv run python docs/teaching/11-journal/generate_bundles.py
"""

from __future__ import annotations

import types
from pathlib import Path

from easycat.debug.export import export_debug_bundle
from easycat.runtime import InMemoryRingBuffer, JournalRecordKind

BUNDLES = Path(__file__).parent / "bundles"


def _emit(journal, name, session_id, data):
    journal.append(
        kind=JournalRecordKind.EVENT,
        name=name,
        session_id=session_id,
        data=data,
    )


def _write(journal, session_id: str, filename: str) -> None:
    BUNDLES.mkdir(exist_ok=True)
    path = BUNDLES / filename
    shim = types.SimpleNamespace(journal=journal)
    export_debug_bundle(shim, path, overwrite=True)
    print(f"  wrote {path.relative_to(Path.cwd())}")


def build_bug_01_empty_final() -> None:
    """Turn entered PROCESSING but STT committed an empty final.

    Root cause (in solutions.md): pre-roll off-by-one; the very first
    speech frame was dropped, STT thought the user trailed off into
    silence, and emitted `text=""`. The agent stage never fires
    because ``run_turn`` short-circuits on empty input.
    """
    j = InMemoryRingBuffer(capacity=1_000)
    sid = "ch11-bug01"
    t = 1_000_000.0
    _emit(j, "turn.started", sid, {"stage": "turn", "t_ms": t})
    _emit(j, "stt.partial", sid, {"stage": "stt", "text": ""})
    _emit(j, "stt.final", sid, {"stage": "stt", "text": "", "t_ms": t + 1100})
    _emit(j, "turn.state_changed", sid, {"stage": "turn", "from": "SPEAKING", "to": "PROCESSING"})
    # Conspicuously absent: stage.agent.execute, stage.tts.execute.
    _emit(j, "turn.state_changed", sid, {"stage": "turn", "from": "PROCESSING", "to": "IDLE"})
    _write(j, sid, "bug_01_empty_final.bundle")


def build_bug_02_tts_stutter() -> None:
    """TTS output stutters. Sentence N plays; N+1 delays; N+2 fine.

    Root cause: intermittent WebSocket reconnects in the TTS provider
    — some ``stage.tts.execute`` spans balloon to 3-5× normal because
    the synth stream reconnected under the hood.
    """
    j = InMemoryRingBuffer(capacity=1_000)
    sid = "ch11-bug02"
    _emit(j, "turn.started", sid, {"stage": "turn", "t_ms": 2_000_000.0})
    _emit(
        j,
        "stt.final",
        sid,
        {"stage": "stt", "text": "Tell me a bit about Rome.", "t_ms": 2_001_000.0},
    )
    _emit(j, "agent.first_token", sid, {"stage": "agent", "t_ms": 2_001_400.0})
    # Three sentences; one of them spent ~1.8 s in WS reconnect.
    _emit(
        j,
        "stage.tts.execute",
        sid,
        {"stage": "tts", "text": "Rome was founded in 753 BC.", "elapsed_ms": 420.0},
    )
    _emit(
        j,
        "ws.reconnect.attempt",
        sid,
        {"stage": "tts", "provider": "openai_tts", "attempt": 1},
    )
    _emit(
        j,
        "ws.reconnect.failure",
        sid,
        {"stage": "tts", "provider": "openai_tts", "attempt": 1, "error": "conn reset"},
    )
    _emit(
        j,
        "ws.reconnect.attempt",
        sid,
        {"stage": "tts", "provider": "openai_tts", "attempt": 2},
    )
    _emit(
        j,
        "ws.reconnect.success",
        sid,
        {"stage": "tts", "provider": "openai_tts", "attempt": 2, "elapsed_ms": 1400.0},
    )
    _emit(
        j,
        "stage.tts.execute",
        sid,
        {
            "stage": "tts",
            "text": "It grew from a small city-state into an empire.",
            "elapsed_ms": 2100.0,  # blown span
        },
    )
    _emit(
        j,
        "stage.tts.execute",
        sid,
        {
            "stage": "tts",
            "text": "At its peak it ruled three continents.",
            "elapsed_ms": 390.0,
        },
    )
    _emit(
        j,
        "turn.gap",
        sid,
        {"stage": "turn", "total_gap_ms": 3500.0, "text": "..."},
    )
    _write(j, sid, "bug_02_tts_stutter.bundle")


def build_bug_03_ghost_interruption() -> None:
    """Bot cancels itself mid-sentence. User never spoke.

    Root cause: speakerphone self-trigger. The bot's own TTS bleeds
    back through the mic; VAD fires VADStartSpeaking; the coordinator
    interprets that as barge-in. No AEC was enabled (note
    ``audio.config``), so the bot keeps 'interrupting' itself on
    every reply.
    """
    j = InMemoryRingBuffer(capacity=1_000)
    sid = "ch11-bug03"
    _emit(j, "audio.config", sid, {"stage": "audio", "nr": "rnnoise", "aec": "off"})
    _emit(j, "turn.started", sid, {"stage": "turn", "t_ms": 3_000_000.0})
    _emit(
        j,
        "stt.final",
        sid,
        {"stage": "stt", "text": "What time is it?", "t_ms": 3_001_000.0},
    )
    _emit(j, "agent.first_token", sid, {"stage": "agent", "t_ms": 3_001_200.0})
    _emit(
        j,
        "stage.tts.execute",
        sid,
        {"stage": "tts", "text": "It's about three in the afternoon.", "elapsed_ms": 300.0},
    )
    # Barge-in fires mid-reply.
    _emit(j, "interruption.start", sid, {"stage": "vad", "t_ms": 3_001_550.0})
    # But — no corresponding STT final or partial from the "user" ever
    # appears in the journal. The next record is just the turn ending.
    _emit(
        j,
        "turn.state_changed",
        sid,
        {"stage": "turn", "from": "BOT_SPEAKING", "to": "IDLE"},
    )
    # Second turn: same story.
    _emit(j, "turn.started", sid, {"stage": "turn", "t_ms": 3_005_000.0})
    _emit(
        j,
        "stt.final",
        sid,
        {"stage": "stt", "text": "Try again please.", "t_ms": 3_006_100.0},
    )
    _emit(j, "agent.first_token", sid, {"stage": "agent", "t_ms": 3_006_350.0})
    _emit(
        j,
        "stage.tts.execute",
        sid,
        {"stage": "tts", "text": "Sorry, about three pm.", "elapsed_ms": 250.0},
    )
    _emit(j, "interruption.start", sid, {"stage": "vad", "t_ms": 3_006_700.0})
    _emit(
        j,
        "turn.state_changed",
        sid,
        {"stage": "turn", "from": "BOT_SPEAKING", "to": "IDLE"},
    )
    _write(j, sid, "bug_03_ghost_interruption.bundle")


def main() -> None:
    print("Building planted-bug bundles...")
    build_bug_01_empty_final()
    build_bug_02_tts_stutter()
    build_bug_03_ghost_interruption()


if __name__ == "__main__":
    main()
