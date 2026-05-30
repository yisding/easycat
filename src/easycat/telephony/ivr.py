"""IVR navigator: agent-driven menu traversal for outbound calls."""

from __future__ import annotations

__all__ = [
    "AgentCallback",
    "DTMFDelivery",
    "IVRAction",
    "IVRActionType",
    "IVRNavigator",
    "IVRNavigatorConfig",
    "classify_ivr_prompt",
    "detect_human_after_ivr",
]

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from easycat.events import EventBus, IVRAction, IVRActionType, STTFinal
from easycat.telephony.screening import EARLY_MEDIA_PHRASES as _EARLY_MEDIA_PATTERNS
from easycat.telephony.twiml import VALID_DTMF_OUTPUT_CHARS, twiml_play_digits

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

# Patterns that indicate a human receptionist answered after IVR navigation.
# Avoid generic phrases like "thank you for calling" or "speaking" — those
# commonly appear in IVR prompts themselves and cause false human-detection.
_HUMAN_AFTER_IVR_PATTERNS: list[str] = [
    "how can i help",
    "how may i help",
    "what can i do for you",
    "hi, this is",
    "hello, this is",
]


def classify_ivr_prompt(text: str) -> bool:
    """Return True if *text* looks like an IVR prompt."""
    lower = text.lower()
    for phrase in _EARLY_MEDIA_PATTERNS:
        if phrase in lower:
            return False
    return any(p.search(text) for p in _IVR_PATTERNS)


def detect_human_after_ivr(text: str) -> bool:
    """Return True if *text* suggests a human answered after IVR navigation."""
    lower = text.lower()
    for phrase in _HUMAN_AFTER_IVR_PATTERNS:
        if phrase in lower:
            return True
    return False


@dataclass
class IVRNavigatorConfig:
    max_depth: int = 10
    prompt_timeout_s: float = 15.0
    agent_timeout_s: float = 10.0
    agent_retry_delay_s: float = 2.0
    dtmf_inter_digit_delay: bool = True
    ivr_dtmf_verify: bool = False
    hold_silence_threshold_s: float = 10.0


class DTMFDelivery:
    """Sends DTMF digits via Twilio REST API (not WebSocket).

    Twilio doesn't support outbound DTMF through bidirectional Media Streams.
    Instead, we update the call with TwiML containing ``<Play digits="..."/>``.
    """

    def __init__(
        self,
        *,
        twilio_client: Any = None,
        call_sid: str = "",
        inter_digit_delay: bool = True,
        verify: bool = False,
    ) -> None:
        self._client = twilio_client
        self._call_sid = call_sid
        self._inter_digit_delay = inter_digit_delay
        self._verify = verify

    @property
    def call_sid(self) -> str:
        return self._call_sid

    @call_sid.setter
    def call_sid(self, value: str) -> None:
        self._call_sid = value

    async def send_speech(self, text: str) -> bool:
        """Send speech via REST API ``<Say>`` TwiML. Returns True on success."""
        if not self._client or not self._call_sid:
            return False

        from xml.sax.saxutils import escape

        safe_text = escape(text, {'"': "&quot;", "'": "&apos;"})
        twiml = f'<Response><Say>{safe_text}</Say><Pause length="30"/></Response>'

        try:
            await asyncio.to_thread(self._client.calls(self._call_sid).update, twiml=twiml)
            return True
        except Exception:
            logger.exception("Speech delivery failed for call %s", self._call_sid)
            return False

    async def send_dtmf(self, digits: str) -> bool:
        """Send DTMF digits via REST API. Returns True on success."""
        if not self._client or not self._call_sid:
            return False

        # Validate against the shared whitelist (VALID_DTMF_OUTPUT_CHARS, the
        # single source of truth in twiml.py) to prevent TwiML injection via the
        # agent callback.  This is an all-or-nothing contract: if any character
        # is invalid the whole input is suspect, so reject it rather than play a
        # partial.  We check the charset directly (rather than calling
        # sanitize_dtmf_digits, which logs its own "stripped" warning) so this
        # rejection path emits exactly one, accurate log line.
        if not digits or any(c not in VALID_DTMF_OUTPUT_CHARS for c in digits):
            logger.warning("Invalid DTMF digits rejected: %r", digits)
            return False

        # Insert W (1-second delay) between digits if inter-digit delay is enabled.
        if self._inter_digit_delay and len(digits) > 1:
            digits = "W".join(digits)

        # Route through the shared output helper for the ``<Play>`` element, then
        # append the keep-alive pause this REST update needs.
        play = twiml_play_digits(digits)
        inner = play[play.index("<Response>") + len("<Response>") : play.index("</Response>")]
        twiml = f'<Response>{inner}<Pause length="30"/></Response>'

        try:
            await asyncio.to_thread(self._client.calls(self._call_sid).update, twiml=twiml)
            return True
        except Exception:
            logger.exception("DTMF delivery failed for call %s", self._call_sid)
            return False

    async def send_dtmf_with_retry(self, digits: str) -> bool:
        """Send DTMF with retry and fallback to speech."""
        success = await self.send_dtmf(digits)
        if not success:
            await asyncio.sleep(0.5)
            success = await self.send_dtmf(digits)
        return success


# Type alias for the agent callback.
AgentCallback = Callable[[dict[str, object]], Awaitable[dict[str, str]]]


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
        dtmf_delivery: DTMFDelivery | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._agent_callback = agent_callback
        self._config = config or IVRNavigatorConfig()
        self._dtmf_delivery = dtmf_delivery
        self._active = False
        self._started = False
        self._menu_depth = 0
        self._history: list[tuple[str, dict[str, str]]] = []
        self._prompt_timeout_task: asyncio.Task[None] | None = None
        self._in_hold = False

    @property
    def menu_depth(self) -> int:
        return self._menu_depth

    @property
    def history(self) -> list[tuple[str, dict[str, str]]]:
        return list(self._history)

    @property
    def in_hold(self) -> bool:
        return self._in_hold

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
        self._in_hold = False

        # Check if a human answered after IVR navigation.  A human can pick up
        # even when no digits were sent (e.g. agent chose "wait"), so this check
        # does not require menu_depth > 0.
        if detect_human_after_ivr(event.text):
            await self._event_bus.emit(
                IVRAction(type=IVRActionType.HUMAN_DETECTED, menu_depth=self._menu_depth)
            )
            return

        if not self._agent_callback:
            return

        # Build context for the agent.
        context = {
            "prompt": event.text,
            "menu_depth": self._menu_depth,
            "history": [{"prompt": p, "action": a} for p, a in self._history],
        }

        try:
            result = await asyncio.wait_for(
                self._agent_callback(context),
                timeout=self._config.agent_timeout_s,
            )
        except TimeoutError:
            logger.warning("IVR agent timed out, retrying after delay")
            await asyncio.sleep(self._config.agent_retry_delay_s)
            try:
                result = await asyncio.wait_for(
                    self._agent_callback(context),
                    timeout=self._config.agent_timeout_s,
                )
            except TimeoutError:
                # Transient: the agent is slow/unreachable. Re-arm the prompt
                # timeout and wait for the next prompt rather than hanging up.
                logger.warning("IVR agent retry timed out")
                self._start_prompt_timeout()
                return
            except Exception:
                # Hard failure (e.g. a crashing callback) on the retry too —
                # this is deterministic, not transient, so escalate to hangup
                # instead of pointlessly re-arming the prompt timeout.
                logger.exception("IVR agent retry crashed; escalating to hangup")
                self._active = False
                await self._event_bus.emit(
                    IVRAction(type=IVRActionType.HANGUP, menu_depth=self._menu_depth)
                )
                return
        except Exception:
            # Hard failure on the first attempt: give the callback one more
            # chance after a delay (transient bugs / flaky deps), then escalate.
            logger.warning("IVR agent callback crashed, retrying after delay")
            await asyncio.sleep(self._config.agent_retry_delay_s)
            try:
                result = await asyncio.wait_for(
                    self._agent_callback(context),
                    timeout=self._config.agent_timeout_s,
                )
            except TimeoutError:
                # Transient: a slow retry is the same condition the timeout path
                # treats leniently, regardless of how the first attempt failed.
                # Re-arm the prompt timeout and wait rather than hanging up.
                logger.warning("IVR agent retry timed out")
                self._start_prompt_timeout()
                return
            except Exception:
                logger.exception("IVR agent retry also crashed; escalating to hangup")
                self._active = False
                await self._event_bus.emit(
                    IVRAction(type=IVRActionType.HANGUP, menu_depth=self._menu_depth)
                )
                return

        action_str = result.get("action", "wait")

        if action_str in ("dtmf", "speak"):
            if action_str == "dtmf":
                payload = result.get("digits", "")
                action_type = IVRActionType.DTMF
                history_entry = {"action": "dtmf", "digits": payload}
            else:
                payload = result.get("text", "")
                action_type = IVRActionType.SPEAK
                history_entry = {"action": "speak", "text": payload}

            self._history.append((event.text, history_entry))
            self._menu_depth += 1

            if self._menu_depth > self._config.max_depth:
                self._active = False
                await self._event_bus.emit(
                    IVRAction(type=IVRActionType.HANGUP, menu_depth=self._menu_depth)
                )
                return

            action = IVRAction(
                type=action_type,
                digits=payload if action_str == "dtmf" else "",
                text=payload if action_str == "speak" else "",
                menu_depth=self._menu_depth,
            )
            await self._event_bus.emit(action)
            self._start_prompt_timeout()

            # Deliver DTMF via REST API if available.
            if action_str == "dtmf" and self._dtmf_delivery:
                success = await self._dtmf_delivery.send_dtmf_with_retry(payload)
                if not success:
                    await self._event_bus.emit(
                        IVRAction(
                            type=IVRActionType.SPEAK,
                            text=payload,
                            menu_depth=self._menu_depth,
                        )
                    )

        elif action_str == "hangup":
            self._active = False
            await self._event_bus.emit(
                IVRAction(type=IVRActionType.HANGUP, menu_depth=self._menu_depth)
            )

        else:
            # "wait" — do nothing, wait for next prompt.
            self._start_prompt_timeout()

    # ── Hold detection ─────────────────────────────────────────────

    def notify_silence(self, duration_s: float) -> None:
        """Called by the session when extended silence is detected.

        If silence exceeds the threshold while active, transition to hold state.
        """
        if self._active and duration_s >= self._config.hold_silence_threshold_s:
            self._in_hold = True

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
        try:
            await asyncio.sleep(self._config.prompt_timeout_s)
            if self._active:
                await self._event_bus.emit(
                    IVRAction(type=IVRActionType.WAIT, menu_depth=self._menu_depth)
                )
        except asyncio.CancelledError:
            pass
