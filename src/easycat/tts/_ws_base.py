"""Shared WebSocket lifecycle helpers for streaming TTS providers.

The Cartesia, Deepgram, and ElevenLabs WebSocket TTS providers all repeat the
same mechanism: a ``ReconnectingWebSocket`` held in ``self._ws`` and an
idempotent ``_close_ws``. This base holds *only* that WebSocket mechanism; the
fire-and-forget ``Error``-emit path (shared with the STT WebSocket base and the
OpenAI HTTP TTS provider) lives on
:class:`~easycat._provider_helpers.ProviderErrorEmitter`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from easycat._provider_helpers import ProviderErrorEmitter
from easycat.audio_format import PCM16_MONO_24K, AudioFormat
from easycat.events import ErrorStage
from easycat.reconnecting_ws import ReconnectingWebSocket
from easycat.runtime.scope import RuntimeScope, RuntimeScopeState
from easycat.tts._multi_context_ws import MultiContextAdapter, MultiContextWSManager
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
    # Provider config, set by subclasses (see class docstring).
    _config: Any

    def __init__(self, output_format: AudioFormat = PCM16_MONO_24K) -> None:
        super().__init__(output_format=output_format)
        self._ws: ReconnectingWebSocket | None = None
        # Persistent multi-context socket manager. Stays ``None`` (and the
        # one-shot-per-synthesize path runs byte-for-byte) unless the provider's
        # config enables ``persistent_ws`` and the provider builds it lazily.
        # Deepgram and custom providers never create one.
        self._mgr: MultiContextWSManager | None = None
        self._runtime_scope: RuntimeScope | None = None
        self._owns_runtime_scope = False
        self._init_emit_tasks()

    def set_runtime_scope(self, parent: RuntimeScope, *, name: str) -> None:
        """Attach persistent WebSocket work to an application lifecycle."""
        if not name:
            raise ValueError("TTS RuntimeScope name must be non-empty")
        current = self._runtime_scope
        if current is not None:
            if current.parent is parent:
                return
            if self._mgr is not None or current.tasks():
                raise RuntimeError("Cannot reattach TTS runtime work after manager creation")
        self._runtime_scope = parent.create_child(name)
        self._owns_runtime_scope = False

    def _ensure_runtime_scope(self) -> RuntimeScope:
        scope = self._runtime_scope
        if scope is None:
            scope = RuntimeScope(name="tts-provider-runtime")
            self._runtime_scope = scope
            self._owns_runtime_scope = True
        return scope

    def _make_multi_context_manager(
        self,
        adapter: MultiContextAdapter,
    ) -> MultiContextWSManager:
        """Build a manager beneath this provider's lifecycle scope."""
        return MultiContextWSManager(adapter, runtime_scope=self._ensure_runtime_scope())

    def _persistent_enabled(self) -> bool:
        """Whether the opt-in persistent multi-context socket is enabled.

        Reads ``persistent_ws`` off the provider config by name so providers
        whose config lacks the field (including custom out-of-tree providers)
        are unaffected and always run the default one-shot path. Deepgram owns
        its serialized persistent socket directly and does not call this helper.
        """
        return bool(getattr(self._config, "persistent_ws", False))

    @staticmethod
    def _parse_frame(frame: Any) -> dict[str, Any] | None:
        """Parse a raw wire frame to a dict once (None for non-text/non-object)."""
        if not isinstance(frame, str):
            return None
        try:
            parsed = json.loads(frame)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    async def _close_ws(self) -> None:
        """Close the current WebSocket connection (idempotent)."""
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.close()
        except Exception:
            # Keep the exact wrapper reachable so stop()/close() can retry a
            # fail-once provider close instead of reporting false success.
            logger.debug("Error closing %s WebSocket", self._provider_log_label, exc_info=True)
            raise
        else:
            if self._ws is ws:
                self._ws = None

    async def _replace_oneshot_ws(
        self,
        factory: Callable[[], ReconnectingWebSocket],
    ) -> ReconnectingWebSocket:
        """Close retained ownership before publishing a fresh one-shot socket.

        A failed provider close deliberately leaves the exact wrapper in
        ``self._ws`` so teardown can retry it. One-shot synthesis must honor
        that retry ledger: creating first and assigning over the retained
        wrapper would leak the old connection permanently.
        """
        await self._close_ws()
        ws = factory()
        self._ws = ws
        return ws

    async def close(self) -> None:
        """Close the WebSocket and drain in-flight Error-emit tasks.

        Shared teardown for every WS TTS provider (Cartesia/Deepgram inherit
        this as-is; ElevenLabs extends it for its HTTP client) so the
        fire-and-forget ``_emit_provider_error`` tasks are awaited rather than
        left dangling into interpreter shutdown.

        When a persistent multi-context manager is in use, it owns the socket,
        so it is closed first; ``_close_ws`` is then a no-op (``self._ws`` is
        ``None`` in persistent mode).
        """
        try:
            if self._mgr is not None:
                await self._mgr.aclose()
            await self._close_ws()
        finally:
            try:
                await self._drain_emit_tasks()
            finally:
                await self._close_owned_runtime_scope_if_idle()

    async def _close_owned_runtime_scope_if_idle(self) -> None:
        scope = self._runtime_scope
        manager = self._mgr
        if (
            not self._owns_runtime_scope
            or scope is None
            or scope.state is not RuntimeScopeState.OPEN
            or not scope.empty
            or (manager is not None and not manager.runtime_cleanup_complete)
        ):
            return
        await scope.close()
        if self._runtime_scope is scope:
            self._runtime_scope = None
            self._owns_runtime_scope = False

    def _require_terminal_response(self, terminal_received: bool, *, terminal_label: str) -> None:
        """Reject provider EOF before a terminal response unless cancelled."""
        if not self._cancelled and not terminal_received:
            raise ConnectionError(
                f"{self._provider_log_label} TTS stream ended before a terminal "
                f"{terminal_label} response"
            )
