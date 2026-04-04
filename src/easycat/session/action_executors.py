"""Built-in session action executors."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from easycat.session.actions import (
    EndCallAction,
    SessionAction,
    SessionActionExecutor,
    SessionActionResult,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CoreSessionActionExecutor(SessionActionExecutor):
    """Executor for provider-neutral core session actions."""

    def supports(self, action: SessionAction) -> bool:
        return isinstance(action, EndCallAction)

    async def execute(self, session: Any, action: SessionAction) -> SessionActionResult:
        if not isinstance(action, EndCallAction):
            raise TypeError(f"Expected EndCallAction, got {type(action).__name__}")
        logger.info("Agent requested end_call: reason=%s", action.reason)
        return SessionActionResult(stop_session=True, metadata={"reason": action.reason})
