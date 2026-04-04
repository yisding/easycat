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
        assert isinstance(action, EndCallAction)
        logger.info(
            "Agent requested end_call: reason=%s code=%s",
            action.reason,
            action.reason_code,
        )
        return SessionActionResult(
            stop_session=True,
            metadata={"reason": action.reason, "reason_code": action.reason_code},
        )
