"""Voicemail / answering machine detection for EasyCat telephony.

Task 6.5: Twilio AMD result consumer (HTTP callback handler).
Task 6.6: Heuristic voicemail detection (long monologues + beep detection).
Task 6.7: Voicemail policy handler (hang up, leave message, transfer).
"""

from __future__ import annotations

import enum
import logging
import math
import struct
from dataclasses import dataclass, field
from typing import Any

from easycat.events import (
    EventBus,
    VADStartSpeaking,
    VADStopSpeaking,
    VoicemailDetected,
)

logger = logging.getLogger(__name__)


# ── Task 6.5: Twilio AMD result consumer ─────────────────────────

# Twilio AnsweredBy values -> EasyCat result mapping
_TWILIO_AMD_MAP: dict[str, str] = {
    "human": "human",
    "machine_start": "machine",
    "machine_end_beep": "machine",
    "machine_end_silence": "machine",
    "machine_end_other": "machine",
    "fax": "machine",
    "unknown": "unknown",
}


def parse_twilio_amd_webhook(params: dict[str, Any]) -> VoicemailDetected | None:
    """Parse a Twilio AMD status callback into a ``VoicemailDetected`` event.

    Twilio Answering Machine Detection results are delivered via HTTP status
    callbacks (POST form data), **not** the Media Streams WebSocket. The
    relevant field is ``AnsweredBy``.

    Args:
        params: POST form parameters from Twilio's AMD callback.

    Returns:
        A ``VoicemailDetected`` event, or ``None`` if the payload lacks AMD data.
    """
    answered_by = params.get("AnsweredBy", "")
    if not isinstance(answered_by, str) or not answered_by:
        return None

    result = _TWILIO_AMD_MAP.get(answered_by.lower(), "unknown")
    return VoicemailDetected(result=result)


async def emit_twilio_amd(
    params: dict[str, Any],
    event_bus: EventBus,
) -> VoicemailDetected | None:
    """Parse a Twilio AMD callback and emit a ``VoicemailDetected`` event.

    Returns:
        The emitted event, or ``None`` if the payload was not an AMD callback.
    """
    event = parse_twilio_amd_webhook(params)
    if event is not None:
        await event_bus.emit(event)
    return event


# ── Task 6.6: Heuristic voicemail detection ──────────────────────


@dataclass
class BeepDetectorConfig:
    """Configuration for beep (tone) detection."""

    min_frequency_hz: float = 800.0
    """Lower bound of expected beep frequency."""

    max_frequency_hz: float = 1200.0
    """Upper bound of expected beep frequency."""

    min_duration_ms: int = 300
    """Minimum duration of a sustained tone to qualify as a beep."""

    energy_threshold: float = 0.02
    """Minimum RMS energy to consider a chunk as containing a tone."""

    sample_rate: int = 16000
    """Expected sample rate of input audio."""


@dataclass
class VoicemailDetectorConfig:
    """Configuration for heuristic voicemail detection."""

    monologue_threshold_s: float = 8.0
    """Continuous speech duration (seconds) that suggests a recorded greeting."""

    beep: BeepDetectorConfig = field(default_factory=BeepDetectorConfig)
    """Beep detection configuration."""


class VoicemailDetector:
    """Heuristic voicemail detector using VAD events and audio analysis.

    Subscribes to ``VADStartSpeaking`` / ``VADStopSpeaking`` events to track
    speech duration. If continuous speech exceeds the monologue threshold, it
    emits ``VoicemailDetected(result="machine")``.

    Also provides :meth:`process_audio` for beep detection on raw audio chunks.
    """

    def __init__(
        self,
        event_bus: EventBus,
        config: VoicemailDetectorConfig | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._config = config or VoicemailDetectorConfig()
        self._speech_start: float | None = None
        self._has_emitted = False
        self._started = False

        # Beep detection state — tracks cumulative tone duration in seconds
        self._tone_duration_s: float = 0.0
        self._beep_detected = False

    @property
    def has_emitted(self) -> bool:
        """Whether a voicemail detection event has been emitted."""
        return self._has_emitted

    def start(self) -> None:
        """Subscribe to VAD events for monologue detection."""
        if not self._started:
            self._event_bus.subscribe(VADStartSpeaking, self._on_speech_start)
            self._event_bus.subscribe(VADStopSpeaking, self._on_speech_stop)
            self._started = True

    def stop(self) -> None:
        """Unsubscribe and reset state."""
        if self._started:
            self._event_bus.unsubscribe(VADStartSpeaking, self._on_speech_start)
            self._event_bus.unsubscribe(VADStopSpeaking, self._on_speech_stop)
            self._started = False
        self.reset()

    def reset(self) -> None:
        """Reset detection state without unsubscribing."""
        self._speech_start = None
        self._has_emitted = False
        self._tone_duration_s = 0.0
        self._beep_detected = False

    async def _on_speech_start(self, event: VADStartSpeaking) -> None:
        """Track start of speech for monologue detection."""
        self._speech_start = event.timestamp

    async def _on_speech_stop(self, event: VADStopSpeaking) -> None:
        """Check if speech duration exceeds monologue threshold."""
        if self._has_emitted or self._speech_start is None:
            return

        duration = event.timestamp - self._speech_start
        self._speech_start = None

        if duration >= self._config.monologue_threshold_s:
            self._has_emitted = True
            await self._event_bus.emit(VoicemailDetected(result="machine"))

    async def process_audio(self, pcm16_data: bytes, sample_rate: int | None = None) -> bool:
        """Analyze a PCM16 audio chunk for beep detection.

        Args:
            pcm16_data: Raw PCM16 (signed 16-bit little-endian) audio bytes.
            sample_rate: Sample rate in Hz. Defaults to config value.

        Returns:
            ``True`` if a beep was detected in this chunk.
        """
        if self._beep_detected or self._has_emitted:
            return False

        sr = sample_rate or self._config.beep.sample_rate
        cfg = self._config.beep

        # Decode PCM16 samples
        num_samples = len(pcm16_data) // 2
        if num_samples == 0:
            return False
        samples = struct.unpack(f"<{num_samples}h", pcm16_data[: num_samples * 2])

        # Compute RMS energy (normalized to [-1, 1])
        float_samples = [s / 32768.0 for s in samples]
        rms = math.sqrt(sum(s * s for s in float_samples) / len(float_samples))

        chunk_duration_s = num_samples / sr

        if rms < cfg.energy_threshold:
            # Silence or very low energy — reset tone tracking
            self._tone_duration_s = 0.0
            return False

        # Zero-crossing rate for simple frequency estimation
        zero_crossings = sum(
            1
            for i in range(1, len(float_samples))
            if (float_samples[i] >= 0) != (float_samples[i - 1] >= 0)
        )
        estimated_freq = zero_crossings / (2.0 * chunk_duration_s) if chunk_duration_s > 0 else 0

        in_freq_range = cfg.min_frequency_hz <= estimated_freq <= cfg.max_frequency_hz

        if in_freq_range:
            self._tone_duration_s += chunk_duration_s
            if self._tone_duration_s * 1000 >= cfg.min_duration_ms:
                self._beep_detected = True
                self._has_emitted = True
                await self._event_bus.emit(VoicemailDetected(result="machine"))
                return True
        else:
            self._tone_duration_s = 0.0

        return False


# ── Task 6.7: Voicemail policy handler ───────────────────────────


class VoicemailPolicy(enum.Enum):
    """Actions to take when voicemail is detected."""

    HANG_UP = "hang_up"
    LEAVE_MESSAGE = "leave_message"
    TRANSFER = "transfer"


@dataclass
class VoicemailPolicyConfig:
    """Configuration for the voicemail policy handler."""

    policy: VoicemailPolicy = VoicemailPolicy.HANG_UP
    """Default policy action."""

    message_text: str = ""
    """Text to speak when leaving a message (used with LEAVE_MESSAGE policy)."""

    transfer_number: str = ""
    """Number to transfer to (used with TRANSFER policy)."""

    wait_for_beep: bool = True
    """Whether to wait for beep detection before leaving a message."""


class VoicemailPolicyHandler:
    """Applies a configurable policy when voicemail is detected.

    Subscribes to ``VoicemailDetected`` events and generates the appropriate
    action. Rather than calling Twilio APIs directly (which would couple this
    to a specific transport), it produces action descriptors that the
    application layer can execute.

    Supported policies:
      - ``HANG_UP``: Generate TwiML ``<Hangup>`` or call REST API.
      - ``LEAVE_MESSAGE``: Wait for beep, then trigger TTS for a message.
      - ``TRANSFER``: Generate TwiML ``<Dial>`` to transfer the call.
    """

    def __init__(
        self,
        event_bus: EventBus,
        config: VoicemailPolicyConfig | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._config = config or VoicemailPolicyConfig()
        self._started = False
        self._action_taken = False
        self._last_action: dict[str, Any] | None = None

    @property
    def last_action(self) -> dict[str, Any] | None:
        """The most recent action descriptor produced by the handler."""
        return self._last_action

    def start(self) -> None:
        """Subscribe to VoicemailDetected events."""
        if not self._started:
            self._event_bus.subscribe(VoicemailDetected, self._on_voicemail_detected)
            self._started = True

    def stop(self) -> None:
        """Unsubscribe and reset state."""
        if self._started:
            self._event_bus.unsubscribe(VoicemailDetected, self._on_voicemail_detected)
            self._started = False
        self._action_taken = False
        self._last_action = None

    async def _on_voicemail_detected(self, event: VoicemailDetected) -> None:
        """Apply policy based on detection result."""
        if self._action_taken:
            return

        # Only act on machine detections
        if event.result != "machine":
            return

        self._action_taken = True
        policy = self._config.policy

        if policy == VoicemailPolicy.HANG_UP:
            await self._handle_hang_up()
        elif policy == VoicemailPolicy.LEAVE_MESSAGE:
            await self._handle_leave_message()
        elif policy == VoicemailPolicy.TRANSFER:
            await self._handle_transfer()

    async def _handle_hang_up(self) -> None:
        """Generate a hang-up action."""
        from easycat.telephony.twiml import twiml_hangup

        self._last_action = {
            "type": "hang_up",
            "twiml": twiml_hangup(),
        }
        logger.info("Voicemail policy: hanging up")

    async def _handle_leave_message(self) -> None:
        """Generate a leave-message action.

        The action descriptor includes the message text for the application
        layer to feed to TTS. If ``wait_for_beep`` is configured, the action
        indicates the caller should wait for beep detection first.
        """
        self._last_action = {
            "type": "leave_message",
            "message_text": self._config.message_text,
            "wait_for_beep": self._config.wait_for_beep,
        }
        logger.info("Voicemail policy: leaving message")

    async def _handle_transfer(self) -> None:
        """Generate a transfer action."""
        from easycat.telephony.twiml import twiml_dial_send_digits

        twiml = twiml_dial_send_digits(self._config.transfer_number, "")
        self._last_action = {
            "type": "transfer",
            "transfer_number": self._config.transfer_number,
            "twiml": twiml,
        }
        logger.info("Voicemail policy: transferring to %s", self._config.transfer_number)
