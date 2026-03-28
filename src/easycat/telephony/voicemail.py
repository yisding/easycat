"""Voicemail / answering machine detection for EasyCat telephony."""

from __future__ import annotations

import asyncio
import enum
import logging
import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from easycat.events import (
    CallAnswered,
    CallScreening,
    EventBus,
    STTFinal,
    VADStartSpeaking,
    VADStopSpeaking,
    VoicemailDetected,
)

logger = logging.getLogger(__name__)


# ── Shared audio analysis helpers ────────────────────────────────


def _pcm16_rms(samples: Sequence[int]) -> float:
    """Compute RMS energy from raw PCM16 samples, normalized to [0, 1]."""
    return math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0


def _zero_crossing_freq(samples: Sequence[int], sample_rate: int) -> float:
    """Estimate dominant frequency via zero-crossing rate."""
    n = len(samples)
    if n < 2:
        return 0.0
    crossings = sum(1 for i in range(1, n) if (samples[i] >= 0) != (samples[i - 1] >= 0))
    return crossings * sample_rate / (2 * n)


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
            await self._event_bus.emit(VoicemailDetected(result="machine", source="detector"))

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

        num_samples = len(pcm16_data) // 2
        if num_samples == 0:
            return False
        samples = struct.unpack(f"<{num_samples}h", pcm16_data[: num_samples * 2])

        rms = _pcm16_rms(samples)
        chunk_duration_s = num_samples / sr

        if rms < cfg.energy_threshold:
            self._tone_duration_s = 0.0
            return False

        estimated_freq = _zero_crossing_freq(samples, sr)

        in_freq_range = cfg.min_frequency_hz <= estimated_freq <= cfg.max_frequency_hz

        if in_freq_range:
            self._tone_duration_s += chunk_duration_s
            if self._tone_duration_s * 1000 >= cfg.min_duration_ms:
                self._beep_detected = True
                self._has_emitted = True
                await self._event_bus.emit(VoicemailDetected(result="machine", source="detector"))
                return True
        else:
            self._tone_duration_s = 0.0

        return False


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
        *,
        expect_fused: bool = False,
    ) -> None:
        self._event_bus = event_bus
        self._config = config or VoicemailPolicyConfig()
        self._expect_fused = expect_fused
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

        # When fusion is active, ignore raw AMD events.
        if self._expect_fused and not event.source:
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


# ── Enhanced voicemail: STT-based greeting classification ──────────

_VOICEMAIL_PHRASES: list[str] = [
    "leave a message",
    "leave me a message",
    "not available right now",
    "not available to take your call",
    "can't come to the phone",
    "cannot come to the phone",
    "you've reached",
    "you have reached",
    "voicemail box",
    "mailbox is full",
    "cannot accept messages",
    "at the tone",
    "after the beep",
    "after the tone",
    "record your message",
    "the person you are trying to reach",
    "the google subscriber",
    "subscriber you are trying to reach",
    "please try again later",
    "press 1 to leave a callback",
    "if you know your party's extension",
]

_HUMAN_PHRASES: list[str] = [
    "hello?",
    "what's up",
    "who is this",
    "how can i help",
    "speaking",
    "yes?",
    "yeah?",
]


def classify_greeting(text: str) -> str:
    """Classify a greeting transcript as ``"human"``, ``"machine"``, or ``"unknown"``.

    Uses phrase-matching heuristics on the transcribed greeting text.
    """
    if not text or not text.strip():
        return "unknown"

    lower = text.lower().strip()

    # Very short ambiguous greetings.
    if len(lower) <= 3:
        return "unknown"

    for phrase in _VOICEMAIL_PHRASES:
        if phrase in lower:
            return "machine"

    # "Hello?" with a question mark or short conversational openers.
    for phrase in _HUMAN_PHRASES:
        if phrase in lower:
            return "human"

    # Double-hello pattern: "Hello? ... Hello?" is human, not machine.
    if lower.count("hello") >= 2 and len(lower) < 40:
        return "human"

    return "unknown"


# ── Enhanced voicemail: SIT tone detection ─────────────────────────

# Special Information Tones (SIT) are three tones in sequence:
# 950 Hz, 1400 Hz, 1800 Hz — used to signal "number not in service".
# YouMail plays these to trick autodialers.

_SIT_FREQUENCIES: list[tuple[float, float]] = [
    (900.0, 1000.0),  # ~950 Hz
    (1350.0, 1450.0),  # ~1400 Hz
    (1750.0, 1850.0),  # ~1800 Hz
]


def detect_sit_tones(
    pcm16_data: bytes,
    sample_rate: int = 16000,
    *,
    energy_threshold: float = 0.02,
    min_tone_duration_ms: int = 200,
) -> bool:
    """Detect SIT (Special Information Tones) in PCM16 audio.

    Returns True if the three-tone SIT sequence (950→1400→1800 Hz) is detected.
    """
    if len(pcm16_data) < 4:
        return False

    num_samples = len(pcm16_data) // 2
    samples = struct.unpack(f"<{num_samples}h", pcm16_data[: num_samples * 2])

    # Divide audio into 50ms windows and analyze each.
    window_size = sample_rate // 20  # 50ms
    if window_size < 2:
        return False

    detected_tones: list[int] = []  # indices of SIT frequency bands detected in order
    window_ms = 50
    min_windows = max(1, min_tone_duration_ms // window_ms)
    current_tone_idx: int | None = None
    current_tone_windows = 0

    for start in range(0, num_samples - window_size, window_size):
        window = samples[start : start + window_size]

        rms = _pcm16_rms(window)
        if rms < energy_threshold:
            if current_tone_idx is not None and current_tone_windows >= min_windows:
                if not detected_tones or detected_tones[-1] < current_tone_idx:
                    detected_tones.append(current_tone_idx)
            current_tone_idx = None
            current_tone_windows = 0
            continue

        freq_estimate = _zero_crossing_freq(window, sample_rate)

        # Check against each SIT frequency band.
        matched_idx: int | None = None
        for idx, (lo, hi) in enumerate(_SIT_FREQUENCIES):
            if lo <= freq_estimate <= hi:
                matched_idx = idx
                break

        if matched_idx is not None and matched_idx == current_tone_idx:
            current_tone_windows += 1
        elif matched_idx is not None:
            # New tone — commit the previous one if it met the duration threshold.
            if current_tone_idx is not None and current_tone_windows >= min_windows:
                if not detected_tones or detected_tones[-1] < current_tone_idx:
                    detected_tones.append(current_tone_idx)
            current_tone_idx = matched_idx
            current_tone_windows = 1
        else:
            if current_tone_idx is not None and current_tone_windows >= min_windows:
                if not detected_tones or detected_tones[-1] < current_tone_idx:
                    detected_tones.append(current_tone_idx)
            current_tone_idx = None
            current_tone_windows = 0

    # Commit the last tone if it met the duration threshold.
    if current_tone_idx is not None and current_tone_windows >= min_windows:
        if not detected_tones or detected_tones[-1] < current_tone_idx:
            detected_tones.append(current_tone_idx)

    # All three tones detected in sequence?
    return detected_tones == [0, 1, 2]


# ── Enhanced voicemail: CNG (Comfort Noise) detection ──────────────


def is_comfort_noise(
    pcm16_data: bytes,
    sample_rate: int = 16000,
    *,
    max_rms: float = 0.005,
) -> bool:
    """Detect comfort noise generation in PCM16 audio.

    CNG has very low amplitude and flat spectral characteristics.
    Returns True if the audio is likely CNG (should be treated as silence).
    """
    if len(pcm16_data) < 4:
        return False

    num_samples = len(pcm16_data) // 2
    samples = struct.unpack(f"<{num_samples}h", pcm16_data[: num_samples * 2])

    return _pcm16_rms(samples) < max_rms


# ── Post-screening voicemail detection ────────────────────────────


class PostScreeningVoicemailDetector:
    """Detects voicemail after call screening completes.

    When screening is detected, the initial voicemail/AMD classification
    may have already fired for the screening prompt. This detector watches
    for a subsequent greeting after screening and applies the greeting
    classifier to determine whether it went to voicemail or a human picked up.

    Subscribes to ``STTFinal`` events. After activation (by screening),
    classifies the next greeting-length utterance.
    """

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._active = False
        self._started = False
        self._classified = False

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> None:
        if self._started:
            return
        self._event_bus.subscribe(STTFinal, self._on_stt_final)
        self._started = True

    def stop(self) -> None:
        if self._started:
            self._event_bus.unsubscribe(STTFinal, self._on_stt_final)
        self._started = False
        self._active = False
        self._classified = False

    def activate(self) -> None:
        """Start watching for post-screening greeting."""
        self._active = True
        self._classified = False

    async def _on_stt_final(self, event: STTFinal) -> None:
        if not self._active or self._classified:
            return

        text = event.text.strip()
        if not text:
            return

        result = classify_greeting(text)
        if result == "unknown":
            return  # Not enough signal yet.

        self._classified = True
        self._active = False
        # Emit with source="fusion" so downstream consumers that filter on
        # expect_fused (e.g. OutboundCallStateMachine, VoicemailPolicyHandler)
        # accept the event without waiting for the AMD fallback timer.
        await self._event_bus.emit(VoicemailDetected(result=result, source="fusion"))


# ── STT + AMD fusion classifier ──────────────────────────────────


class STTAMDFusionClassifier:
    """Fuses AMD webhook results with STT-based greeting classification.

    When AMD arrives first, it takes effect. If STT classification arrives
    first, it takes effect. When both arrive, the configured priority wins.

    Emits a single ``VoicemailDetected`` based on the fused result.
    """

    def __init__(
        self,
        event_bus: EventBus,
        *,
        prefer_stt: bool = True,
        stt_timeout_s: float = 5.0,
    ) -> None:
        self._event_bus = event_bus
        self._prefer_stt = prefer_stt
        self._stt_timeout_s = stt_timeout_s

        self._amd_result: str | None = None
        self._stt_result: str | None = None
        self._emitted = False
        self._started = False
        self._call_answered = False
        self._screening_active = False
        self._timeout_task: asyncio.Task[None] | None = None

    @property
    def amd_result(self) -> str | None:
        return self._amd_result

    @property
    def stt_result(self) -> str | None:
        return self._stt_result

    def start(self) -> None:
        if self._started:
            return
        self._event_bus.subscribe(CallAnswered, self._on_call_answered)
        self._event_bus.subscribe(VoicemailDetected, self._on_voicemail_detected)
        self._event_bus.subscribe(STTFinal, self._on_stt_final)
        self._event_bus.subscribe(CallScreening, self._on_screening)
        self._started = True

    def stop(self) -> None:
        if self._started:
            self._event_bus.unsubscribe(CallAnswered, self._on_call_answered)
            self._event_bus.unsubscribe(VoicemailDetected, self._on_voicemail_detected)
            self._event_bus.unsubscribe(STTFinal, self._on_stt_final)
            self._event_bus.unsubscribe(CallScreening, self._on_screening)
        self._cancel_timeout()
        self._started = False
        self._amd_result = None
        self._stt_result = None
        self._emitted = False
        self._call_answered = False
        self._screening_active = False

    def _cancel_timeout(self) -> None:
        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()
        self._timeout_task = None

    async def _on_call_answered(self, event: CallAnswered) -> None:
        self._call_answered = True

    async def _on_screening(self, event: CallScreening) -> None:
        """Cancel AMD-only fallback and stop STT classification when screening is detected."""
        self._cancel_timeout()
        self._screening_active = True

    async def _on_voicemail_detected(self, event: VoicemailDetected) -> None:
        """Receive AMD result (upstream VoicemailDetected)."""
        if self._emitted:
            return
        if event.source == "fusion":
            return  # Ignore our own emissions.
        self._amd_result = event.result
        await self._try_fuse()

    async def _on_stt_final(self, event: STTFinal) -> None:
        """Classify greeting text via STT."""
        if self._emitted or self._screening_active or not self._call_answered:
            return
        text = event.text.strip()
        if not text:
            return
        result = classify_greeting(text)
        if result == "unknown":
            return
        self._stt_result = result
        await self._try_fuse()

    async def _try_fuse(self) -> None:
        """Attempt to produce a fused classification."""
        if self._emitted:
            return

        # If both signals available, fuse them.
        if self._amd_result is not None and self._stt_result is not None:
            # Agreement — easy.
            if self._amd_result == self._stt_result:
                await self._emit(self._amd_result)
            else:
                # Disagreement — use preference.
                winner = self._stt_result if self._prefer_stt else self._amd_result
                await self._emit(winner)
            return

        # Only one signal — use it if it's decisive, else wait.
        if self._amd_result is not None and self._amd_result != "unknown":
            if self._stt_result is None and self._timeout_task is None:
                # Start timeout to wait for STT.
                self._start_timeout()
            return

        if self._stt_result is not None:
            # STT classified before AMD arrived — emit immediately.
            await self._emit(self._stt_result)

    async def _emit(self, result: str) -> None:
        if self._emitted:
            return
        self._emitted = True
        self._cancel_timeout()
        await self._event_bus.emit(VoicemailDetected(result=result, source="fusion"))

    def _start_timeout(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._timeout_task = loop.create_task(self._timeout_coro())

    async def _timeout_coro(self) -> None:
        """Timeout waiting for STT — use AMD result alone."""
        try:
            await asyncio.sleep(self._stt_timeout_s)
            if not self._emitted and self._amd_result is not None:
                await self._emit(self._amd_result)
        except asyncio.CancelledError:
            pass
