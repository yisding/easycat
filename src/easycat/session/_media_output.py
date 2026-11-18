"""Weakly-bound outbound media facade over a :class:`Session`.

Telephony helpers that are built before (and may outlive) the session they
serve — such as the in-band IVR DTMF sender — need to play tones and bypass
speech on that session's stream without keeping a stopped session alive or
importing it at module scope. :class:`SessionMediaOutput` holds only a
``weakref`` to the session and exposes the two outbound primitives they need.
"""

from __future__ import annotations

import weakref
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from easycat._dtmf_tones import DTMFToneConfig, DTMFTonePlayback
    from easycat.session._session import Session

__all__ = ["SessionMediaOutput"]


class SessionMediaOutput:
    """Outbound media for one session, held by weak reference.

    Every call raises :class:`RuntimeError` once the session has been
    garbage-collected; the session's own lifecycle checks (not running,
    stopping, ``text_session`` mode) apply as usual.
    """

    __slots__ = ("__weakref__", "_session_ref")

    def __init__(self, session: Session) -> None:
        self._session_ref: weakref.ref[Session] = weakref.ref(session)

    @property
    def session(self) -> Session | None:
        """The bound session, or ``None`` once it has been collected."""
        return self._session_ref()

    def _require_session(self) -> Session:
        session = self._session_ref()
        if session is None:
            raise RuntimeError("The EasyCat session for this media output is gone")
        return session

    async def play_dtmf_tones(
        self,
        digits: str,
        *,
        inter_digit_delay_ms: int = 0,
        tone_config: DTMFToneConfig | None = None,
        fence_timeout_s: float = 10.0,
        should_continue: Callable[[], bool] | None = None,
    ) -> DTMFTonePlayback:
        """Delegate to :meth:`Session.play_dtmf_tones` on the bound session."""
        return await self._require_session().play_dtmf_tones(
            digits,
            inter_digit_delay_ms=inter_digit_delay_ms,
            tone_config=tone_config,
            fence_timeout_s=fence_timeout_s,
            should_continue=should_continue,
        )

    async def synthesize_bypass(self, text: str) -> None:
        """Delegate to :meth:`Session.synthesize_bypass` on the bound session."""
        await self._require_session().synthesize_bypass(text)
