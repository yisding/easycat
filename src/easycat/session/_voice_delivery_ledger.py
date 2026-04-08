"""VoiceDeliveryLedger — tracks what text has been delivered to the user."""

from __future__ import annotations


class VoiceDeliveryLedger:
    """Tracks what text has been delivered to the user.

    In voice mode, "delivered" means the text was synthesised and the
    corresponding audio was sent to the transport.  In text mode,
    every agent output is immediately considered delivered.
    """

    def __init__(self, *, text_mode: bool = False) -> None:
        self._text_mode = text_mode
        self._delivered_text = ""
        self._raw_agent_text = ""
        self._spoken_text = ""

    def record_agent_text(self, text: str) -> None:
        """Record raw text produced by the agent."""
        self._raw_agent_text += text
        if self._text_mode:
            self._delivered_text += text

    def record_spoken_text(self, text: str) -> None:
        """Record text that was spoken (TTS synthesised)."""
        self._spoken_text += text

    def mark_delivered(self, text: str) -> None:
        """Explicitly mark *text* as delivered."""
        self._delivered_text = text

    @property
    def delivered_text(self) -> str:
        """Return text considered delivered to the user."""
        return self._delivered_text

    def reset(self) -> None:
        """Reset all tracked text for a new turn."""
        self._delivered_text = ""
        self._raw_agent_text = ""
        self._spoken_text = ""
