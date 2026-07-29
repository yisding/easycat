"""Shared memory and wire-size limits for network transports."""

# A WebSocket message is either one PCM frame or one small control envelope.
# Keeping this well below websockets' 1 MiB default bounds parsing and
# resampling work before transport-specific queue controls can run.
MAX_WEBSOCKET_MESSAGE_BYTES = 64 * 1024

# A second, byte-based inbound limit complements the frame-count limit. The
# default holds roughly 131 seconds of 16 kHz PCM16 mono audio, while normal
# 20 ms traffic still reaches the 200-frame count limit first.
DEFAULT_INBOUND_AUDIO_MAX_BYTES = 4 * 1024 * 1024
