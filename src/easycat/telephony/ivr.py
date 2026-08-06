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
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from easycat._epoch import Epoch, Lease
from easycat.events import EventBus, IVRAction, IVRActionType, STTFinal
from easycat.runtime.scope import BackgroundTaskScope
from easycat.telephony._ivr_decision import IVRAgentDecision, parse_ivr_agent_decision
from easycat.telephony.dtmf import is_valid_dtmf_output
from easycat.telephony.screening import EARLY_MEDIA_PHRASES as _EARLY_MEDIA_PATTERNS
from easycat.telephony.twiml import twiml_play_digits

logger = logging.getLogger(__name__)
_IVR_PROMPT_TIMER_MEMBER = "ivr_prompt_timeout"

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
    hold_silence_threshold_s: float = 10.0

    def __post_init__(self) -> None:
        if isinstance(self.max_depth, bool) or not isinstance(self.max_depth, int):
            raise ValueError("max_depth must be a positive integer")  # noqa: TRY004 domain-specific validation error
        if self.max_depth <= 0:
            raise ValueError("max_depth must be a positive integer")
        _validate_positive_number("prompt_timeout_s", self.prompt_timeout_s)
        _validate_positive_number("agent_timeout_s", self.agent_timeout_s)
        _validate_non_negative_number("agent_retry_delay_s", self.agent_retry_delay_s)
        _validate_non_negative_number("hold_silence_threshold_s", self.hold_silence_threshold_s)


def _validate_positive_number(name: str, value: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be a positive number")


def _validate_non_negative_number(name: str, value: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{name} must be a non-negative number")


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
        # single source of truth in dtmf.py) to prevent TwiML injection via the
        # agent callback.  This is an all-or-nothing contract: if any character
        # is invalid the whole input is suspect, so reject it rather than play a
        # partial.  We check the charset directly (rather than calling
        # sanitize_dtmf_digits, which logs its own "stripped" warning) so this
        # rejection path emits exactly one, accurate log line.
        if not is_valid_dtmf_output(digits):
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

    async def send_dtmf_with_retry(
        self,
        digits: str,
        *,
        should_continue: Callable[[], bool] | None = None,
    ) -> bool:
        """Send DTMF with retry and fallback to speech."""
        if should_continue is not None and not should_continue():
            return False
        success = await self.send_dtmf(digits)
        if not success and (should_continue is None or should_continue()):
            await asyncio.sleep(0.5)
            if should_continue is not None and not should_continue():
                return False
            success = await self.send_dtmf(digits)
        return success


# Type alias for the agent callback.
AgentCallback = Callable[[dict[str, object]], Awaitable[Mapping[str, object]]]
_AGENT_CALL_FAILED = object()
_AGENT_CALL_TIMED_OUT = object()


@dataclass(frozen=True)
class _AgentCallbackRaised:
    error: Exception


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
        # Every inactive -> active transition starts a distinct ownership
        # epoch. Agent callbacks and delivery retries may outlive a call-state
        # transition, so active state alone cannot distinguish old work from a
        # newly activated call.
        self._activation_epoch: Epoch[bool] = Epoch(False)
        self._started = False
        self._menu_depth = 0
        self._history: list[tuple[str, dict[str, str]]] = []
        self._timer_tasks = BackgroundTaskScope(name="ivr-navigator")
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

    @property
    def _active(self) -> bool:
        """Return the active payload without exposing the epoch mechanism."""
        return self._activation_epoch.capture().value

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
        self.deactivate()
        self._started = False

    def activate(self) -> None:
        if self._activation_epoch.capture().value:
            return
        self._activation_epoch.bump(True)

    def deactivate(self) -> None:
        # Invalidate work before publishing the inactive state. A later
        # activate receives another epoch, so old callbacks stay stale even
        # after navigation becomes active again for a different call.
        self._activation_epoch.bump(False)
        self._cancel_prompt_timeout()

    def reset_for_call(self) -> None:
        """Clear navigation state owned by the previous outbound call.

        Activation can occur more than once while traversing one call's state
        machine, so it deliberately does not reset depth or history. The
        outbound callback coordinator invokes this method only at the
        ``CallInitiated`` boundary for a new call.
        """
        self._cancel_prompt_timeout()
        self._menu_depth = 0
        self._history.clear()
        self._in_hold = False

    @staticmethod
    def _is_current_activation(activation: Lease[bool]) -> bool:
        return activation.value and activation.guard()

    # ── STT handler ───────────────────────────────────────────────

    async def _on_stt_final(self, event: STTFinal) -> None:
        activation = self._activation_epoch.capture()
        if not activation.value:
            return

        self._cancel_prompt_timeout()
        self._in_hold = False

        # Check if a human answered after IVR navigation.  A human can pick up
        # even when no digits were sent (e.g. agent chose "wait"), so this check
        # does not require menu_depth > 0.
        if detect_human_after_ivr(event.text):
            if not self._is_current_activation(activation):
                return
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

        result = await self._call_agent_with_retry(context, activation)
        if result is _AGENT_CALL_FAILED:
            # Retry path already handled escalation (hangup) or re-arm (wait).
            return
        if not self._is_current_activation(activation):
            return

        await self._apply_agent_decision(
            event.text,
            parse_ivr_agent_decision(result),
            activation,
        )

    async def _apply_agent_decision(
        self,
        prompt: str,
        decision: IVRAgentDecision,
        activation: Lease[bool],
    ) -> None:
        if not self._is_current_activation(activation):
            return
        if decision.advances_menu:
            await self._advance_menu(prompt, decision, activation)
        elif decision.type is IVRActionType.HANGUP:
            await self._escalate_to_hangup(activation)
        else:
            self._start_prompt_timeout(activation)

    async def _advance_menu(
        self,
        prompt: str,
        decision: IVRAgentDecision,
        activation: Lease[bool],
    ) -> None:
        if not self._is_current_activation(activation):
            return
        self._history.append((prompt, decision.history_entry()))
        self._menu_depth += 1
        if self._menu_depth > self._config.max_depth:
            await self._escalate_to_hangup(activation)
            return

        await self._event_bus.emit(decision.to_event(menu_depth=self._menu_depth))
        if not self._is_current_activation(activation):
            return
        self._start_prompt_timeout(activation)
        await self._deliver_dtmf_or_fallback(decision, activation)

    async def _deliver_dtmf_or_fallback(
        self,
        decision: IVRAgentDecision,
        activation: Lease[bool],
    ) -> None:
        if (
            not self._is_current_activation(activation)
            or decision.type is not IVRActionType.DTMF
            or self._dtmf_delivery is None
        ):
            return
        delivered = await self._dtmf_delivery.send_dtmf_with_retry(
            decision.payload,
            should_continue=lambda: self._is_current_activation(activation),
        )
        if not self._is_current_activation(activation) or delivered:
            return
        await self._event_bus.emit(
            IVRAction(
                type=IVRActionType.SPEAK,
                text=decision.payload,
                menu_depth=self._menu_depth,
            )
        )

    async def _call_agent_with_retry(
        self,
        context: dict[str, object],
        activation: Lease[bool],
    ) -> object:
        """Call the agent callback with one delayed retry.

        Returns the agent's raw result on success. Returns a private sentinel
        when the attempt could not be completed and escalation has already
        been handled:

        * A slow/timed-out retry is treated as **transient** — the prompt
          timeout is re-armed and we wait for the next prompt.
        * A crashing retry is treated as **deterministic** — we escalate to
          hangup rather than pointlessly re-arming.

        The first failure (timeout or crash) is always retried once after a
        delay; only the second attempt's outcome decides re-arm vs hangup.
        """
        assert self._agent_callback is not None  # guarded by caller
        first_result = await self._call_agent_once(context, activation)
        if first_result is _AGENT_CALL_FAILED:
            return _AGENT_CALL_FAILED
        if first_result is _AGENT_CALL_TIMED_OUT:
            logger.warning("IVR agent timed out, retrying after delay")
        elif isinstance(first_result, _AgentCallbackRaised):
            logger.warning("IVR agent callback crashed, retrying after delay")
        else:
            return first_result

        await asyncio.sleep(self._config.agent_retry_delay_s)
        if not self._is_current_activation(activation):
            return _AGENT_CALL_FAILED
        retry_result = await self._call_agent_once(context, activation)
        if retry_result is _AGENT_CALL_FAILED:
            return _AGENT_CALL_FAILED
        if retry_result is _AGENT_CALL_TIMED_OUT:
            # Transient: the agent is slow/unreachable. Re-arm the prompt
            # timeout and wait for the next prompt rather than hanging up.
            logger.warning("IVR agent retry timed out")
            self._start_prompt_timeout(activation)
            return _AGENT_CALL_FAILED
        if isinstance(retry_result, _AgentCallbackRaised):
            # Deterministic failure (e.g. a crashing callback) on the retry too:
            # escalate to hangup instead of pointlessly re-arming.
            logger.warning(
                "IVR agent retry crashed; escalating to hangup",
                exc_info=(
                    type(retry_result.error),
                    retry_result.error,
                    retry_result.error.__traceback__,
                ),
            )
            await self._escalate_to_hangup(activation)
            return _AGENT_CALL_FAILED
        return retry_result

    async def _call_agent_once(
        self,
        context: dict[str, object],
        activation: Lease[bool],
    ) -> object:
        """Run one fenced callback attempt and classify its outcome."""
        assert self._agent_callback is not None  # guarded by caller
        if not self._is_current_activation(activation):
            return _AGENT_CALL_FAILED
        try:
            result: object = await asyncio.wait_for(
                self._agent_callback(context),
                timeout=self._config.agent_timeout_s,
            )
        except TimeoutError:
            result = _AGENT_CALL_TIMED_OUT
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            result = _AgentCallbackRaised(exc)
        if not self._is_current_activation(activation):
            return _AGENT_CALL_FAILED
        return result

    async def _escalate_to_hangup(self, activation: Lease[bool]) -> None:
        """Deactivate navigation and emit a terminal HANGUP action."""
        if not self._is_current_activation(activation):
            return
        menu_depth = self._menu_depth
        self.deactivate()
        await self._event_bus.emit(IVRAction(type=IVRActionType.HANGUP, menu_depth=menu_depth))

    # ── Hold detection ─────────────────────────────────────────────

    def notify_silence(self, duration_s: float) -> None:
        """Called by the session when extended silence is detected.

        If silence exceeds the threshold while active, transition to hold state.
        """
        if self._active and duration_s >= self._config.hold_silence_threshold_s:
            self._in_hold = True

    # ── Timeout ───────────────────────────────────────────────────

    def _start_prompt_timeout(self, activation: Lease[bool]) -> None:
        if not self._is_current_activation(activation):
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._prompt_timeout_task = self._timer_tasks.create_task(
            _IVR_PROMPT_TIMER_MEMBER,
            self._prompt_timeout_coro(activation),
            replace=True,
        )

    def _cancel_prompt_timeout(self) -> None:
        task = self._prompt_timeout_task
        if task is None or task.done():
            return
        self._timer_tasks.cancel(_IVR_PROMPT_TIMER_MEMBER)
        task.cancel()
        self._prompt_timeout_task = None

    async def _prompt_timeout_coro(self, activation: Lease[bool]) -> None:
        try:
            await asyncio.sleep(self._config.prompt_timeout_s)
            if self._is_current_activation(activation):
                await self._event_bus.emit(
                    IVRAction(type=IVRActionType.WAIT, menu_depth=self._menu_depth)
                )
        finally:
            if self._prompt_timeout_task is asyncio.current_task():
                self._prompt_timeout_task = None
