"""Shared WebSocket lifecycle helpers for streaming TTS providers.

The Cartesia, Deepgram, and ElevenLabs WebSocket TTS providers all repeat the
same mechanism: a ``ReconnectingWebSocket`` held in ``self._ws`` and an
idempotent ``_close_ws``. This base holds *only* that WebSocket mechanism; the
fire-and-forget ``Error``-emit path (shared with the STT WebSocket base and the
OpenAI HTTP TTS provider) lives on
:class:`~easycat._provider_helpers.ProviderErrorEmitter`.
"""

from __future__ import annotations

import logging

from easycat._provider_helpers import ProviderErrorEmitter
from easycat.audio_format import PCM16_MONO_24K, AudioFormat
from easycat.events import ErrorStage
from easycat.reconnecting_ws import ReconnectingWebSocket
from easycat.tts.base import TTSBase

logger = logging.getLogger(__name__)


class _WSTTSBase(ProviderErrorEmitter, TTSBase):
    """Base class for TTS providers backed by a streaming WebSocket.

    Subclasses must set :attr:`_provider_error_name` (the ``provider`` string
    attached to emitted :class:`~easycat.events.Error` events) and
    :attr:`_provider_log_label` (the human-readable name used in log lines),
    expose their config on ``self._config`` (with an ``event_bus`` attribute),
    and assign the active socket to ``self._ws``.

    The fire-and-forget ``Error``-emit path (with strong task references so the
    loop does not GC them mid-emit) and ``_drain_emit_tasks`` are inherited from
    :class:`~easycat._provider_helpers.ProviderErrorEmitter`. Each provider
    still owns its frame builders, URL/auth, and the provider-specific context
    keys it passes to :meth:`_emit_provider_error` (``http_status`` / ``body``
    / ``ws_close_code`` / ``status_code``), since those are legitimately
    per-provider.
    """

    _error_stage = ErrorStage.TTS
    # Human-readable label used in log lines (e.g. ``"Cartesia"``).
    _provider_log_label: str = "TTS"

    def __init__(self, output_format: AudioFormat = PCM16_MONO_24K) -> None:
        super().__init__(output_format=output_format)
        self._ws: ReconnectingWebSocket | None = None
        self._init_emit_tasks()

    async def _close_ws(self) -> None:
        """Close the current WebSocket connection (idempotent)."""
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                logger.debug("Error closing %s WebSocket", self._provider_log_label, exc_info=True)
            finally:
                self._ws = None
