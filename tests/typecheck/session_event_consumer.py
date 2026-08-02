"""Static downstream contract for typed Session event subscriptions."""

from easycat import Session, STTFinal
from easycat.events import EventSubscription


def register_transcript_handlers(session: Session) -> EventSubscription:
    transcripts: list[str] = []

    def on_final(event: STTFinal) -> None:
        transcripts.append(event.text)

    async def on_final_async(event: STTFinal) -> None:
        transcripts.append(event.text)

    subscription = session.subscribe_event(STTFinal, on_final)
    async_subscription = session.subscribe_event(STTFinal, on_final_async)
    inferred_subscription = session.subscribe_event(
        STTFinal,
        lambda event: transcripts.append(event.text),
    )

    session.unsubscribe_event(STTFinal, on_final_async)
    async_subscription.unsubscribe()
    inferred_subscription.unsubscribe()
    return subscription
