"""IVR navigator: agent-driven menu traversal for outbound calls."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from enum import Enum

from easycat.events import EventBus, STTFinal

logger = logging.getLogger(__name__)

# Heuristic patterns that indicate IVR prompts.
_IVR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"press\s+\d", re.IGNORECASE),
    re.compile(r"dial\s+\d", re.IGNORECASE),
    re.compile(r"say\s+\w+\s+or\s+", re.IGNORECASE),
    re.compile(r"for\s+\w+,?\s+press", re.IGNORECASE),
    re.compile(r"if you know your party", re.IGNORECASE),
    re.compile(r"press\s+(one|1)\s+to\s+accept", re.IGNORECASE),
    re.compile(r"you have a call", re.IGNORECASE),
    re.compile(r"extension.{0,10}dial", re.IGNORECASE),
]

# Early-media phrases that should NOT be classified as IVR.
_EARLY_MEDIA_PATTERNS: list[str] = [
    "this call may be monitored",
    "please hold while we connect",
    "call may be recorded",
]


def classify_ivr_prompt(text: str) -> bool:
    """Return True if *text* looks like an IVR prompt."""
    lower = text.lower()
    for phrase in _EARLY_MEDIA_PATTERNS:
        if phrase in lower:
            return False
    return any(p.search(text) for p in _IVR_PATTERNS)


class IVRActionType(Enum):
    DTMF = "dtmf"
    SPEAK = "speak"
    WAIT = "wait"
    HANGUP = "hangup"


@dataclass(frozen=True)
class IVRAction:
    """Emitted when the navigator decides on an action."""

    type: IVRActionType
    digits: str = ""
    text: str = ""
    menu_depth: int = 0


@dataclass
class IVRNavigatorConfig:
    max_depth: int = 10
    prompt_timeout_s: float = 15.0


class IVRNavigator:
    """Agent-driven IVR menu traversal.

    When activated, subscribes to :class:`STTFinal` events and passes IVR
    prompts to an ``agent_callback`` which returns a dict with an action.

    The ``agent_callback`` signature::

        async def agent_callback(context: dict) -> dict
            # context: {"prompt": str, "menu_depth": int, "history": list}
            # returns: {"action": "dtmf"|"speak"|"wait"|"hangup", ...}
    """

    def __init__(
        self,
        event_bus: EventBus,
        *,
        agent_callback: AgentCallback | None = None,
        config: IVRNavigatorConfig | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._agent_callback = agent_callback
        self._config = config or IVRNavigatorConfig()
        self._active = False
        self._started = False
        self._menu_depth = 0
        self._history: list[tuple[str, dict[str, str]]] = []
        self._prompt_timeout_task: asyncio.Task[None] | None = None

    @property
    def menu_depth(self) -> int:
        return self._menu_depth

    @property
    def history(self) -> list[tuple[str, dict[str, str]]]:
        return list(self._history)

    # ── Lifecycle ─────────────────────────────────────────────────

    def start(self) -> None:
        if self._started:
            return
        self._event_bus.subscribe(STTFinal, self._on_stt_final)
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self._event_bus.unsubscribe(STTFinal, self._on_stt_final)
        self._cancel_prompt_timeout()
        self._started = False

    def activate(self) -> None:
        self._active = True

    def deactivate(self) -> None:
        self._active = False
        self._cancel_prompt_timeout()

    # ── STT handler ───────────────────────────────────────────────

    async def _on_stt_final(self, event: STTFinal) -> None:
        if not self._active:
            return

        self._cancel_prompt_timeout()

        if not self._agent_callback:
            return

        # Build context for the agent.
        context = {
            "prompt": event.text,
            "menu_depth": self._menu_depth,
            "history": [{"prompt": p, "action": a} for p, a in self._history],
        }

        try:
            result = await self._agent_callback(context)
        except Exception:
            logger.exception("IVR agent callback failed")
            self._start_prompt_timeout()
            return

        action_str = result.get("action", "wait")

        if action_str == "dtmf":
            digits = result.get("digits", "")
            action = IVRAction(
                type=IVRActionType.DTMF,
                digits=digits,
                menu_depth=self._menu_depth,
            )
            self._history.append((event.text, {"action": "dtmf", "digits": digits}))
            self._menu_depth += 1

            if self._menu_depth > self._config.max_depth:
                await self._event_bus.emit(
                    IVRAction(type=IVRActionType.HANGUP, menu_depth=self._menu_depth)
                )
                return

            await self._event_bus.emit(action)

        elif action_str == "speak":
            text = result.get("text", "")
            action = IVRAction(
                type=IVRActionType.SPEAK,
                text=text,
                menu_depth=self._menu_depth,
            )
            self._history.append((event.text, {"action": "speak", "text": text}))
            self._menu_depth += 1

            if self._menu_depth > self._config.max_depth:
                await self._event_bus.emit(
                    IVRAction(type=IVRActionType.HANGUP, menu_depth=self._menu_depth)
                )
                return

            await self._event_bus.emit(action)

        elif action_str == "hangup":
            await self._event_bus.emit(
                IVRAction(type=IVRActionType.HANGUP, menu_depth=self._menu_depth)
            )

        else:
            # "wait" — do nothing, wait for next prompt.
            self._start_prompt_timeout()

    # ── Timeout ───────────────────────────────────────────────────

    def _start_prompt_timeout(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._prompt_timeout_task = loop.create_task(self._prompt_timeout_coro())

    def _cancel_prompt_timeout(self) -> None:
        if self._prompt_timeout_task and not self._prompt_timeout_task.done():
            self._prompt_timeout_task.cancel()
            self._prompt_timeout_task = None

    async def _prompt_timeout_coro(self) -> None:
        await asyncio.sleep(self._config.prompt_timeout_s)
        if self._active:
            await self._event_bus.emit(
                IVRAction(type=IVRActionType.WAIT, menu_depth=self._menu_depth)
            )


# Type alias for the agent callback.
from collections.abc import Awaitable, Callable  # noqa: E402

AgentCallback = Callable[[dict[str, object]], Awaitable[dict[str, str]]]
