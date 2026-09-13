"""Echo cancellation using LiveKit's AudioProcessingModule (WebRTC AEC3).

Provides an optional AEC pipeline stage that processes the raw near-end
(microphone) signal before noise reduction and VAD. The far-end (speaker)
signal is fed as a reference via ``feed_reference``.

LiveKit APM requires 10 ms int16 PCM frames — the same encoding as EasyCat's
``AudioChunk``. Stateful per-direction buffers submit only complete frames and
carry partial input into the next chunk without injecting padding silence.

Requires the ``livekit`` package (``uv add 'easycat[aec]'``). From the
EasyCat repo, use ``uv sync --extra aec --group dev``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from easycat._extras import require_module
from easycat._pipeline_decisions import (
    ECHO_CANCELLER_INSTANCE_METHODS,
    has_provider_shape,
)
from easycat._provider_catalog import ProviderCatalog
from easycat.audio_format import AudioChunk

logger = logging.getLogger(__name__)

# 10 ms frame sizes at common sample rates (samples per frame).
_FRAME_SAMPLES_BY_RATE: dict[int, int] = {
    8000: 80,
    16000: 160,
    24000: 240,
    48000: 480,
}
EchoCancellationFallbackPolicy: TypeAlias = Literal["passthrough", "error"]
_VALID_AEC_FALLBACK_POLICIES: tuple[EchoCancellationFallbackPolicy, ...] = (
    "passthrough",
    "error",
)
_BUILTIN_ECHO_CANCELLERS = frozenset({"passthrough", "livekit"})
ECHO_CANCELLER_PROVIDER_ENTRY_POINT_GROUP = "easycat.echo_canceller_providers"
_CATALOG = ProviderCatalog(
    specs={},
    kind="echo canceller",
    entry_point_group=ECHO_CANCELLER_PROVIDER_ENTRY_POINT_GROUP,
)


def _validate_aec_fallback_policy(policy: str) -> EchoCancellationFallbackPolicy:
    if policy not in _VALID_AEC_FALLBACK_POLICIES:
        allowed = ", ".join(_VALID_AEC_FALLBACK_POLICIES)
        raise ValueError(
            f"Unknown echo cancellation fallback_policy '{policy}'. Expected one of: {allowed}."
        )
    return policy


def _frame_samples_for_rate(sample_rate: int) -> int:
    """Return the number of samples in a 10 ms frame at the given rate.

    WebRTC's AudioProcessingModule (AEC3) only supports a fixed set of sample
    rates. Feeding it an ``AudioFrame`` at an arbitrary rate either errors
    inside LiveKit or silently degrades cancellation, so unsupported rates are
    rejected here rather than fed through with a best-effort ``//100`` frame.
    """
    samples = _FRAME_SAMPLES_BY_RATE.get(sample_rate)
    if samples is None:
        supported = ", ".join(str(r) for r in sorted(_FRAME_SAMPLES_BY_RATE))
        raise ValueError(
            f"Unsupported sample rate {sample_rate} Hz for WebRTC AEC. "
            f"Supported rates: {supported}. Resample to one of these "
            "(e.g. 16000 or 48000) before echo cancellation."
        )
    return samples


# ── LiveKit AEC ───────────────────────────────────────────────────


class LiveKitAEC:
    """Echo canceller using LiveKit's AudioProcessingModule (WebRTC AEC3).

    Requires the ``livekit`` package.
    """

    def __init__(self) -> None:
        self._rtc = require_module("livekit.rtc", extra="aec", purpose="Echo cancellation")
        self._apm: Any = self._rtc.AudioProcessingModule(echo_cancellation=True)
        # AEC requires the near-end (process) and far-end (feed_reference)
        # streams to share a sample rate. We capture the first rate seen on
        # either side and reject any later mismatch.
        self._stream_rate: int | None = None
        # Per-stream remainder buffers. AEC3 advances its adaptive filter a
        # whole 10 ms frame at a time, so chunks that are not exact multiples
        # of a frame must not be zero-padded mid-stream (that injects silence
        # into the filter and desyncs near-end/far-end alignment). Instead we
        # accumulate raw PCM per direction and only submit whole frames,
        # retaining the sub-frame remainder for the next call.
        self._near_buffer: bytes = b""
        self._far_buffer: bytes = b""
        logger.info("LiveKit AEC initialized")

    def _check_stream_rate(self, sample_rate: int) -> None:
        """Ensure near-end and far-end streams use the same sample rate."""
        if self._stream_rate is None:
            self._stream_rate = sample_rate
        elif sample_rate != self._stream_rate:
            raise ValueError(
                "AEC near-end and far-end sample rates must match: "
                f"already processing at {self._stream_rate} Hz but received "
                f"{sample_rate} Hz. Resample both streams to a common rate."
            )

    def close(self) -> None:
        """Release AudioProcessingModule resources."""
        self._apm = None
        self._near_buffer = b""
        self._far_buffer = b""

    def __del__(self) -> None:
        self.close()

    async def process(self, chunk: AudioChunk) -> AudioChunk:
        """Process a near-end (microphone) audio chunk through AEC.

        LiveKit's APM modifies the ``AudioFrame`` in place, so we wrap
        each whole 10 ms slice, invoke ``process_stream``, then reassemble
        from the frame's (now-processed) data buffer.

        AEC3 advances its adaptive filter a full 10 ms per frame, so a
        chunk that does not end on a frame boundary leaves a sub-frame
        remainder buffered for the next call rather than being zero-padded
        into the filter. The returned chunk therefore covers exactly the
        whole frames processed this call, which may differ in length from
        the input by less than one 10 ms frame.
        """
        fmt = chunk.format
        frame_samples = _frame_samples_for_rate(fmt.sample_rate)
        self._check_stream_rate(fmt.sample_rate)
        frame_bytes = frame_samples * fmt.frame_size

        self._near_buffer += chunk.data
        processed_parts: list[bytes] = []
        while len(self._near_buffer) >= frame_bytes:
            frame_data = self._near_buffer[:frame_bytes]
            self._near_buffer = self._near_buffer[frame_bytes:]
            af = self._rtc.AudioFrame(
                data=frame_data,
                sample_rate=fmt.sample_rate,
                num_channels=fmt.channels,
                samples_per_channel=frame_samples,
            )
            self._apm.process_stream(af)
            processed_parts.append(bytes(af.data))

        joined = b"".join(processed_parts)
        return AudioChunk(data=joined, format=fmt, timestamp=chunk.timestamp)

    def feed_reference(self, chunk: AudioChunk) -> None:
        """Feed a far-end (speaker) audio chunk as the AEC reference signal.

        Mirrors ``process``: only whole 10 ms frames are submitted to
        ``process_reverse_stream`` and the sub-frame remainder is buffered
        for the next call so the reverse stream stays frame-aligned with the
        near-end stream rather than being zero-padded mid-stream.
        """
        fmt = chunk.format
        frame_samples = _frame_samples_for_rate(fmt.sample_rate)
        self._check_stream_rate(fmt.sample_rate)
        frame_bytes = frame_samples * fmt.frame_size

        self._far_buffer += chunk.data
        while len(self._far_buffer) >= frame_bytes:
            frame_data = self._far_buffer[:frame_bytes]
            self._far_buffer = self._far_buffer[frame_bytes:]
            af = self._rtc.AudioFrame(
                data=frame_data,
                sample_rate=fmt.sample_rate,
                num_channels=fmt.channels,
                samples_per_channel=frame_samples,
            )
            self._apm.process_reverse_stream(af)

    def version_info(self) -> dict[str, str]:
        sdk_ver = "unknown"
        try:
            from importlib.metadata import version

            sdk_ver = version("livekit")
        except Exception:  # noqa: BLE001, S110 intentional boundary or best-effort cleanup
            pass
        return {
            "provider": "livekit",
            "model": "webrtc-aec3",
            "api_version": "unknown",
            "sdk_version": sdk_ver,
        }


# ── Passthrough (no-op) ──────────────────────────────────────────


class PassthroughAEC:
    """No-op echo canceller that passes audio through unchanged."""

    is_passthrough_provider = True

    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk

    def feed_reference(self, chunk: AudioChunk) -> None:
        pass

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "passthrough",
            "model": "unknown",
            "api_version": "unknown",
            "sdk_version": "unknown",
        }


# ── Config & factory ─────────────────────────────────────────────


@dataclass
class EchoCancellationConfig:
    """Configuration for echo cancellation."""

    enabled: bool = False
    fallback_policy: EchoCancellationFallbackPolicy = "passthrough"

    def __post_init__(self) -> None:
        self.fallback_policy = _validate_aec_fallback_policy(self.fallback_policy)


def register_echo_canceller_provider(
    name: str,
    provider_cls: type,
    config_cls: type,
    *,
    env_var: str | None = None,
    extra: str | None = None,
    api_domains: tuple[str, ...] = (),
    probe_module: str | None = None,
    capabilities: frozenset[str] = frozenset(),
) -> None:
    """Register a third-party echo canceller and its discovery metadata."""
    normalized = name.strip().lower() if isinstance(name, str) else ""
    if normalized in _BUILTIN_ECHO_CANCELLERS:
        raise ValueError(
            f"Echo canceller provider name {normalized!r} is reserved by a built-in backend."
        )
    _CATALOG.register(
        name,
        provider_cls,
        config_cls,
        env_var=env_var,
        extra=extra,
        api_domains=api_domains,
        probe_module=probe_module,
        capabilities=capabilities,
    )


def available_echo_canceller_providers() -> list[str]:
    """Return every built-in or registered echo-canceller provider name."""
    return sorted(_BUILTIN_ECHO_CANCELLERS | set(_CATALOG.available_names()))


def is_echo_canceller_config(value: object) -> bool:
    """True when ``value`` is a built-in or registered echo-canceller config."""
    return isinstance(value, EchoCancellationConfig) or _CATALOG.is_config_instance(value)


def parse_echo_canceller_string(spec: str) -> Any:
    """Parse a built-in or registered ``provider/model`` shortcut."""
    provider, separator, model = spec.partition("/")
    normalized = provider.strip().lower()
    if normalized in _BUILTIN_ECHO_CANCELLERS:
        if separator and model.strip():
            raise ValueError(f"Built-in echo canceller {normalized!r} does not accept a model.")
        return EchoCancellationConfig(enabled=normalized == "livekit")
    return _CATALOG.parse_string(spec)


def create_echo_canceller(config: Any = None) -> Any:
    """Create an echo canceller based on configuration.

    Returns LiveKitAEC when enabled and the livekit package is available.
    Missing LiveKit falls back to PassthroughAEC when fallback_policy is
    "passthrough", or raises RuntimeError when fallback_policy is "error".
    """
    if isinstance(config, str):
        config = parse_echo_canceller_string(config)
    if config is not None and has_provider_shape(config, ECHO_CANCELLER_INSTANCE_METHODS):
        return config
    if _CATALOG.is_config_instance(config):
        return _CATALOG.create_from_config(config, event_bus=None)

    cfg = config or EchoCancellationConfig()
    if not isinstance(cfg, EchoCancellationConfig):
        raise ValueError(  # noqa: TRY004 domain-specific validation error
            f"Unsupported echo canceller configuration type: {type(cfg).__name__!r}. "
            "Pass EchoCancellationConfig, a registered config, or an echo-canceller instance."
        )
    cfg.fallback_policy = _validate_aec_fallback_policy(cfg.fallback_policy)

    if not cfg.enabled:
        return PassthroughAEC()

    try:
        return LiveKitAEC()
    except (ImportError, RuntimeError) as exc:
        if cfg.fallback_policy == "error":
            raise RuntimeError(
                f"Echo cancellation requested but LiveKit AEC is unavailable: {exc}"
            ) from exc
        logger.warning("LiveKit AEC not available, falling back to passthrough: %s", exc)
        return PassthroughAEC()
