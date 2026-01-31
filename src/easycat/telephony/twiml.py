"""TwiML generation and parsing helpers for EasyCat telephony.

Task 6.2: TwiML ``<Gather>`` fallback — parse Gather webhook callbacks.
Task 6.4: DTMF tone output via ``<Play digits>`` and ``<Dial sendDigits>``.
Task 6.7 (partial): ``<Hangup>`` TwiML for voicemail policy.
"""

from __future__ import annotations

import logging
from typing import Any
from xml.sax.saxutils import escape

from easycat.events import DTMF, EventBus

logger = logging.getLogger(__name__)


# ── Task 6.2: TwiML Gather fallback ──────────────────────────────


def parse_gather_webhook(params: dict[str, Any]) -> list[DTMF]:
    """Parse a Twilio ``<Gather>`` webhook callback into DTMF events.

    Twilio POSTs form data including a ``Digits`` field containing the full
    collected digit string (e.g. ``"12345#"``).

    Args:
        params: The POST form parameters from Twilio's Gather callback.

    Returns:
        A list of ``DTMF`` events, one per digit found.
    """
    digits = params.get("Digits", "")
    if not isinstance(digits, str):
        return []

    events: list[DTMF] = []
    for ch in digits:
        ch = ch.upper()
        if ch in "0123456789*#ABCD":
            events.append(DTMF(digit=ch))
    return events


async def emit_gather_digits(
    params: dict[str, Any],
    event_bus: EventBus,
) -> list[DTMF]:
    """Parse a Gather webhook and emit individual DTMF events.

    Convenience wrapper around :func:`parse_gather_webhook`.

    Returns:
        The list of emitted ``DTMF`` events.
    """
    events = parse_gather_webhook(params)
    for event in events:
        await event_bus.emit(event)
    return events


# ── Task 6.4: DTMF output via TwiML ──────────────────────────────


def twiml_play_digits(digits: str) -> str:
    """Generate TwiML to play DTMF tones.

    Produces a ``<Response><Play digits="..."/></Response>`` fragment that
    Twilio will render as audible DTMF tones on the call.

    Args:
        digits: Digit string to play (e.g. ``"1234#"``).

    Returns:
        Complete TwiML ``<Response>`` document as a string.
    """
    safe = escape(digits)
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Play digits="{safe}"/></Response>'


def twiml_dial_send_digits(
    phone_number: str,
    send_digits: str,
    *,
    caller_id: str | None = None,
) -> str:
    """Generate TwiML to dial a number and send DTMF digits after connect.

    Useful for navigating IVR menus or dialing extensions.

    Args:
        phone_number: The number to dial (E.164 format recommended).
        send_digits: Digits to send after the call connects.
            Use ``w`` for a 0.5 s pause (e.g. ``"wwww1928#"``).
        caller_id: Optional caller ID for the outbound leg.

    Returns:
        Complete TwiML ``<Response>`` document as a string.
    """
    safe_digits = escape(send_digits)
    safe_number = escape(phone_number)
    dial_attrs = ""
    if caller_id:
        dial_attrs = f' callerId="{escape(caller_id)}"'
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Dial{dial_attrs}>"
        f'<Number sendDigits="{safe_digits}">{safe_number}</Number>'
        "</Dial></Response>"
    )


def twiml_gather(
    *,
    action_url: str,
    num_digits: int | None = None,
    timeout: int = 5,
    finish_on_key: str = "#",
    input_type: str = "dtmf",
    say_text: str | None = None,
) -> str:
    """Generate TwiML for ``<Gather>`` digit collection.

    Args:
        action_url: URL Twilio will POST collected digits to.
        num_digits: Expected number of digits (omit for variable-length).
        timeout: Seconds to wait for first/next digit.
        finish_on_key: Key that signals the end of input.
        input_type: Input mode — ``"dtmf"``, ``"speech"``, or ``"dtmf speech"``.
        say_text: Optional prompt to speak while gathering.

    Returns:
        Complete TwiML ``<Response>`` document as a string.
    """
    attrs = [
        f'action="{escape(action_url)}"',
        f'timeout="{timeout}"',
        f'finishOnKey="{escape(finish_on_key)}"',
        f'input="{escape(input_type)}"',
    ]
    if num_digits is not None:
        attrs.append(f'numDigits="{num_digits}"')

    attr_str = " ".join(attrs)
    inner = ""
    if say_text:
        inner = f"<Say>{escape(say_text)}</Say>"

    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Gather {attr_str}>{inner}</Gather></Response>"
    )


def twiml_hangup() -> str:
    """Generate TwiML to hang up the call.

    Returns:
        Complete TwiML ``<Response>`` document as a string.
    """
    return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
