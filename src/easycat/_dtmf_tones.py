"""In-band DTMF tone synthesis (ITU-T Q.23) for telephony media streams.

This is a root leaf module: it depends only on the standard library and the
shared G.711 codec in :mod:`easycat.transports._g711`, so the session layer can
render key presses without importing :mod:`easycat.telephony`.

The renderer produces 8 kHz little-endian PCM16 (or G.711 mu-law) with a
lead-in, raised-cosine ramped dual tones, inter-key gaps, optional ``w``/``W``
pauses and a tail. Every duration is a whole number of milliseconds, so the
rendered length is always exactly ``dtmf_sequence_duration_ms(...) * 8``
samples.

Input is validated all-or-nothing against the same alphabet as
``easycat.telephony.dtmf.VALID_DTMF_OUTPUT_CHARS``; error messages never echo
the requested digits, which may be PINs or account numbers.

:class:`DTMFTonePlayback` is the handle a transport-backed playback returns:
it reports how many keys were rendered, how long the burst lasts and resolves
to a :data:`DTMFPlaybackOutcome` once the far end has (or has not) played it.
"""

from __future__ import annotations

import asyncio
import math
import struct
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from types import MappingProxyType
from typing import Literal

from easycat.transports._g711 import pcm16_to_mulaw

__all__ = [
    "DTMF_FREQUENCIES",
    "DTMF_PAUSES_MS",
    "DTMF_SAMPLE_RATE",
    "MAX_DTMF_DIGITS",
    "MAX_DTMF_INTER_DIGIT_DELAY_MS",
    "DTMFPlaybackOutcome",
    "DTMFToneConfig",
    "DTMFTonePlayback",
    "count_dtmf_keys",
    "dtmf_sequence_duration_ms",
    "render_dtmf_mulaw",
    "render_dtmf_pcm16",
    "render_dtmf_pcm16_async",
]

DTMF_SAMPLE_RATE = 8000
_SAMPLES_PER_MS = DTMF_SAMPLE_RATE // 1000

# ITU-T Q.23 keypad: rows are the low group, columns the high group.
_LOW_GROUP_HZ = (697, 770, 852, 941)
_HIGH_GROUP_HZ = (1209, 1336, 1477, 1633)
_KEYPAD_ROWS = ("123A", "456B", "789C", "*0#D")

DTMF_FREQUENCIES: Mapping[str, tuple[int, int]] = MappingProxyType(
    {
        key: (low, high)
        for row, low in zip(_KEYPAD_ROWS, _LOW_GROUP_HZ, strict=True)
        for key, high in zip(row, _HIGH_GROUP_HZ, strict=True)
    }
)
"""Q.23 ``(low_hz, high_hz)`` pair for each of the 16 DTMF keys."""

DTMF_PAUSES_MS: Mapping[str, int] = MappingProxyType({"w": 500, "W": 1000})
"""Pause markers (Twilio ``<Play digits>`` semantics): ``w`` = 0.5 s, ``W`` = 1 s."""

# Mirrors ``easycat.session.actions`` (which mirrors the telephony limits) so
# this leaf does not import either package. A test guards against drift.
MAX_DTMF_DIGITS = 128
MAX_DTMF_INTER_DIGIT_DELAY_MS = 10_000

# Peak amplitude of a 0 dBm0 sine in 16-bit linear PCM for G.711 mu-law.
_ZERO_DBM0_PEAK = 22662.0
# Largest magnitude the mu-law encoder represents without clipping.
_MAX_COMBINED_PEAK = 32635.0

# Sequences longer than this are rendered off the event loop by
# :func:`render_dtmf_pcm16_async`.
_OFFLOAD_THRESHOLD_MS = 5000

_INVALID_DIGITS_MESSAGE = "DTMF digits contain unsupported characters; allowed: 0-9 * # A-D w W"

DTMFPlaybackOutcome = Literal["played", "timeout", "disconnected", "estimated"]
"""How an in-band DTMF burst ended.

* ``"played"`` — the transport confirmed the far end played the whole burst.
* ``"timeout"`` — no confirmation arrived in time; the tones were still sent.
* ``"disconnected"`` — the stream ended (or the delivery failed) first.
* ``"estimated"`` — the transport has no playback confirmation, so completion
  was assumed after the burst's nominal duration.
"""


def _require_int(name: str, value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def _require_float(name: str, value: object, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    if not low <= number <= high:
        raise ValueError(f"{name} must be between {low:g} and {high:g}")
    return number


def _dbm0_to_peak(level_dbm0: float) -> float:
    return _ZERO_DBM0_PEAK * 10.0 ** (level_dbm0 / 20.0)


@dataclass(frozen=True)
class DTMFToneConfig:
    """Timing and level of synthesized in-band DTMF tones.

    The defaults follow ITU-T Q.24 with comfortable margins: 100 ms tones and
    gaps, the low group at -7 dBm0 and the high group at -5 dBm0 (+2 dB
    forward twist), and 2 ms raised-cosine ramps so key edges do not splatter.

    Attributes:
        tone_ms: Duration of each key's dual tone, 40-500 ms.
        gap_ms: Minimum silence between consecutive keys, 40-10000 ms.
        lead_in_ms: Silence before the first key, 0-1000 ms.
        tail_ms: Silence after the last key, 0-1000 ms.
        low_level_dbm0: Low-group (row) level, -20 to -3 dBm0.
        high_level_dbm0: High-group (column) level, -20 to -3 dBm0.
        ramp_ms: Raised-cosine attack and release per tone, 0-10 ms.
        max_total_ms: Upper bound on a rendered sequence, 100-600000 ms.
            Rendering a longer sequence raises :class:`ValueError`.

    Raises:
        TypeError: A field has the wrong type (booleans are rejected).
        ValueError: A field is out of range or not finite, or the combined
            tone level would clip the mu-law encoder.
    """

    tone_ms: int = 100
    gap_ms: int = 100
    lead_in_ms: int = 60
    tail_ms: int = 100
    low_level_dbm0: float = -7.0
    high_level_dbm0: float = -5.0
    ramp_ms: float = 2.0
    max_total_ms: int = 60_000

    def __post_init__(self) -> None:
        _require_int("tone_ms", self.tone_ms, 40, 500)
        _require_int("gap_ms", self.gap_ms, 40, 10_000)
        _require_int("lead_in_ms", self.lead_in_ms, 0, 1000)
        _require_int("tail_ms", self.tail_ms, 0, 1000)
        _require_int("max_total_ms", self.max_total_ms, 100, 600_000)
        low = _require_float("low_level_dbm0", self.low_level_dbm0, -20.0, -3.0)
        high = _require_float("high_level_dbm0", self.high_level_dbm0, -20.0, -3.0)
        _require_float("ramp_ms", self.ramp_ms, 0.0, 10.0)
        if _dbm0_to_peak(low) + _dbm0_to_peak(high) > _MAX_COMBINED_PEAK:
            raise ValueError("combined DTMF tone level would clip")

    @property
    def low_amplitude(self) -> float:
        """Peak amplitude of the low-group sine in PCM16 units."""
        return _dbm0_to_peak(self.low_level_dbm0)

    @property
    def high_amplitude(self) -> float:
        """Peak amplitude of the high-group sine in PCM16 units."""
        return _dbm0_to_peak(self.high_level_dbm0)


_DEFAULT_CONFIG = DTMFToneConfig()


@dataclass(frozen=True, kw_only=True)
class DTMFTonePlayback:
    """Handle for one in-band DTMF burst handed to a transport.

    Returned once the tone frames were accepted for sending. ``wait()``
    resolves when the transport reports the far end played them (or that it
    could not). Construct it with a future the delivery path resolves; the
    future is never cancelled by waiting on the handle.

    Attributes:
        key_count: Number of DTMF keys in the burst (pause markers excluded).
        duration_ms: Nominal length of the rendered burst.
        mark_name: Playback mark that confirms completion, when the transport
            supports marks; ``None`` otherwise.
        delivery: Always ``"in_band"``.
    """

    key_count: int
    duration_ms: int
    mark_name: str | None
    _done: asyncio.Future[DTMFPlaybackOutcome] = field(repr=False, compare=False)
    delivery: Literal["in_band"] = "in_band"

    def done(self) -> bool:
        """Return whether the playback outcome is known."""
        return self._done.done()

    async def wait(self, timeout: float | None = None) -> DTMFPlaybackOutcome:
        """Wait up to *timeout* seconds for the playback outcome.

        Returns ``"timeout"`` when the outcome is still unknown at the
        deadline; the underlying future keeps running, so a later ``wait()``
        can still observe ``"played"``. A delivery future that was cancelled
        or failed reports ``"disconnected"``. Cancelling the caller never
        cancels the delivery.
        """
        if timeout is not None:
            _require_float("timeout", timeout, 0.0, math.inf)
        future = self._done
        if not future.done():
            await asyncio.wait((future,), timeout=timeout)
            if not future.done():
                return "timeout"
        if future.cancelled() or future.exception() is not None:
            return "disconnected"
        return future.result()


def _validate_digits(digits: object) -> str:
    if not isinstance(digits, str):
        raise TypeError("DTMF digits must be a str")
    if not digits:
        raise ValueError("DTMF digits must not be empty")
    if len(digits) > MAX_DTMF_DIGITS:
        raise ValueError(f"DTMF digits must be {MAX_DTMF_DIGITS} characters or fewer")
    for char in digits:
        if char not in DTMF_FREQUENCIES and char not in DTMF_PAUSES_MS:
            raise ValueError(_INVALID_DIGITS_MESSAGE)
    return digits


def _effective_gap_ms(config: DTMFToneConfig, inter_digit_delay_ms: object) -> int:
    if isinstance(inter_digit_delay_ms, bool) or not isinstance(inter_digit_delay_ms, int):
        raise TypeError("inter_digit_delay_ms must be an integer")
    if inter_digit_delay_ms < 0:
        raise ValueError("inter_digit_delay_ms must not be negative")
    return max(config.gap_ms, min(inter_digit_delay_ms, MAX_DTMF_INTER_DIGIT_DELAY_MS))


def _layout(
    digits: object, inter_digit_delay_ms: object, config: DTMFToneConfig | None
) -> tuple[DTMFToneConfig, list[tuple[str | None, int]]]:
    """Return the config and ``(key | None, duration_ms)`` segments.

    ``None`` segments are silence. Adjacent silences are merged.
    """
    cfg = _DEFAULT_CONFIG if config is None else config
    if not isinstance(cfg, DTMFToneConfig):
        raise TypeError("config must be a DTMFToneConfig")
    text = _validate_digits(digits)
    gap_ms = _effective_gap_ms(cfg, inter_digit_delay_ms)

    segments: list[tuple[str | None, int]] = []

    def silence(duration_ms: int) -> None:
        if duration_ms <= 0:
            return
        if segments and segments[-1][0] is None:
            segments[-1] = (None, segments[-1][1] + duration_ms)
        else:
            segments.append((None, duration_ms))

    silence(cfg.lead_in_ms)
    seen_key = False
    for char in text:
        pause = DTMF_PAUSES_MS.get(char)
        if pause is not None:
            silence(pause)
            continue
        if seen_key:
            silence(gap_ms)
        segments.append((char, cfg.tone_ms))
        seen_key = True
    silence(cfg.tail_ms)
    return cfg, segments


def count_dtmf_keys(digits: str) -> int:
    """Return how many DTMF keys *digits* contains (pause markers excluded).

    Raises:
        TypeError: *digits* is not a string.
        ValueError: *digits* is empty, too long or has unsupported characters.
    """
    return sum(1 for char in _validate_digits(digits) if char in DTMF_FREQUENCIES)


def dtmf_sequence_duration_ms(
    digits: str,
    *,
    inter_digit_delay_ms: int = 0,
    config: DTMFToneConfig | None = None,
) -> int:
    """Return the exact length in milliseconds of the rendered sequence.

    Computed from the layout without synthesizing audio. Unlike the
    renderers, this does not enforce ``config.max_total_ms``.

    Raises:
        TypeError: *digits* is not a string, *inter_digit_delay_ms* is not an
            integer, or *config* has the wrong type.
        ValueError: *digits* or *inter_digit_delay_ms* is invalid.
    """
    _, segments = _layout(digits, inter_digit_delay_ms, config)
    return sum(duration for _, duration in segments)


@lru_cache(maxsize=64)
def _tone_pcm16(key: str, config: DTMFToneConfig) -> bytes:
    low_hz, high_hz = DTMF_FREQUENCIES[key]
    count = config.tone_ms * _SAMPLES_PER_MS
    ramp = round(config.ramp_ms * _SAMPLES_PER_MS)
    low_amp = config.low_amplitude
    high_amp = config.high_amplitude
    low_step = 2.0 * math.pi * low_hz / DTMF_SAMPLE_RATE
    high_step = 2.0 * math.pi * high_hz / DTMF_SAMPLE_RATE
    samples = []
    for index in range(count):
        value = low_amp * math.sin(low_step * index) + high_amp * math.sin(high_step * index)
        if ramp:
            edge = min(index, count - 1 - index)
            if edge < ramp:
                value *= 0.5 - 0.5 * math.cos(math.pi * (edge + 0.5) / ramp)
        samples.append(max(-32768, min(32767, round(value))))
    return struct.pack(f"<{count}h", *samples)


def _render(digits: object, inter_digit_delay_ms: object, config: DTMFToneConfig | None) -> bytes:
    cfg, segments = _layout(digits, inter_digit_delay_ms, config)
    total_ms = sum(duration for _, duration in segments)
    if total_ms > cfg.max_total_ms:
        raise ValueError(
            f"DTMF sequence would last {total_ms} ms, above max_total_ms={cfg.max_total_ms}"
        )
    parts = [
        bytes(duration * _SAMPLES_PER_MS * 2) if key is None else _tone_pcm16(key, cfg)
        for key, duration in segments
    ]
    return b"".join(parts)


def render_dtmf_pcm16(
    digits: str,
    *,
    inter_digit_delay_ms: int = 0,
    config: DTMFToneConfig | None = None,
) -> bytes:
    """Render *digits* as 8 kHz mono little-endian PCM16 DTMF audio.

    Layout: ``lead_in_ms`` of silence, each key's ``tone_ms`` dual tone, and
    ``max(gap_ms, inter_digit_delay_ms)`` of silence between consecutive keys
    (the delay is clamped to 10000 ms). Each ``w``/``W`` adds 500/1000 ms of
    silence on top. The sequence ends with ``tail_ms`` of silence.

    Validation is all-or-nothing: nothing is rendered if any character is
    outside ``0-9 * # A-D w W``. Error messages never echo the digits.

    Raises:
        TypeError: *digits* is not a string, *inter_digit_delay_ms* is not an
            integer, or *config* has the wrong type.
        ValueError: *digits* is empty, longer than 128 characters or has an
            unsupported character; *inter_digit_delay_ms* is negative; or the
            sequence exceeds ``config.max_total_ms``.
    """
    return _render(digits, inter_digit_delay_ms, config)


def render_dtmf_mulaw(
    digits: str,
    *,
    inter_digit_delay_ms: int = 0,
    config: DTMFToneConfig | None = None,
) -> bytes:
    """Render *digits* as 8 kHz G.711 mu-law DTMF audio.

    Same layout and validation as :func:`render_dtmf_pcm16`.
    """
    return pcm16_to_mulaw(_render(digits, inter_digit_delay_ms, config), DTMF_SAMPLE_RATE)


async def render_dtmf_pcm16_async(
    digits: str,
    *,
    inter_digit_delay_ms: int = 0,
    config: DTMFToneConfig | None = None,
) -> bytes:
    """Async :func:`render_dtmf_pcm16` that renders long sequences off the loop.

    Sequences up to 5 s render inline (the per-key tones are cached); longer
    ones are synthesized with :func:`asyncio.to_thread`.
    """
    duration_ms = dtmf_sequence_duration_ms(
        digits, inter_digit_delay_ms=inter_digit_delay_ms, config=config
    )
    if duration_ms <= _OFFLOAD_THRESHOLD_MS:
        return _render(digits, inter_digit_delay_ms, config)
    return await asyncio.to_thread(_render, digits, inter_digit_delay_ms, config)
