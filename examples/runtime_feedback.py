"""Console feedback helpers for EasyCat examples."""

from __future__ import annotations

from easycat.events import AgentFinal, BotStoppedSpeaking, Interruption, STTFinal, TurnStarted
from easycat.session import Session


def attach_runtime_feedback(session: Session) -> None:
    """Print useful status updates and transcripts while examples are running."""

    session.subscribe_event(TurnStarted, lambda _e: print("🎤 Listening…"))
    session.subscribe_event(STTFinal, lambda e: print(f"📝 You: {e.text}"))
    session.subscribe_event(AgentFinal, lambda e: print(f"🤖 Assistant: {e.text}"))
    session.subscribe_event(
        BotStoppedSpeaking, lambda _e: print("✅ Your turn — you can speak now.")
    )
    session.subscribe_event(Interruption, lambda _e: print("⚡ Interruption detected."))
