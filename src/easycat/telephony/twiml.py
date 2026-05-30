"""TwiML generation and parsing helpers for EasyCat telephony."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from easycat.events import DTMF, EventBus
from easycat.telephony.dtmf import VALID_DTMF_DIGITS

logger = logging.getLogger(__name__)

# Characters allowed in TwiML ``digits``/``sendDigits`` attributes: standard
# DTMF digits (ITU-T Q.23) plus ``W``/``w`` inter-digit pause markers.  This is
# the single source of truth shared with :class:`DTMFDelivery` so both DTMF
# output paths enforce the same anti-injection whitelist.
VALID_DTMF_OUTPUT_CHARS = VALID_DTMF_DIGITS | frozenset("wW")


def sanitize_dtmf_digits(digits: str) -> str:
    """Strip characters outside :data:`VALID_DTMF_OUTPUT_CHARS` from *digits*.

    TwiML ``digits``/``sendDigits`` attributes must only carry DTMF digits and
    ``W``/``w`` pause markers.  Stripping anything else closes the TwiML
    injection / garbage surface that bare XML attribute quoting would otherwise
    leave open.  Dropped characters are logged at WARNING level.
    """
    if not digits:
        return ""
    sanitized = "".join(c for c in digits if c in VALID_DTMF_OUTPUT_CHARS)
    if sanitized != digits:
        logger.warning(
            "Stripped invalid DTMF characters from output digits: %r -> %r",
            digits,
            sanitized,
        )
    return sanitized


# ── Twilio webhook validation ────────────────────────────────────


def validate_twilio_webhook_signature(
    *,
    auth_token: str,
    url: str | Iterable[str],
    params: Mapping[str, Any] | Sequence[tuple[str, Any]],
    signature: str | None,
) -> bool:
    """Validate Twilio's ``X-Twilio-Signature`` webhook header.

    ``url`` must be the exact public URL Twilio requested, including query
    string.  Apps behind a TLS-terminating proxy/load balancer should
    reconstruct that public URL with :func:`reconstruct_public_url` (or pass a
    list of candidate URLs) so a rewritten scheme/host does not silently break
    validation.

    Args:
        auth_token: The Twilio auth token used to sign the request.
        url: The exact public URL (string) Twilio requested, or an iterable of
            candidate URLs to try (validation succeeds if any candidate
            matches).  Candidate lists are useful behind proxies where the
            reconstructed scheme/host is ambiguous.
        params: The POST form parameters from the webhook request.
        signature: The value of the ``X-Twilio-Signature`` header.
    """
    if not auth_token or not signature:
        return False

    candidates = [url] if isinstance(url, str) else list(url)
    if not candidates:
        return False

    provided = signature.strip()
    for candidate in candidates:
        expected = compute_twilio_webhook_signature(
            auth_token=auth_token,
            url=candidate,
            params=params,
        )
        if hmac.compare_digest(expected, provided):
            return True
    return False


def reconstruct_public_url(
    headers: Mapping[str, Any],
    path: str,
    *,
    trust_proxy: bool = False,
    default_scheme: str = "https",
) -> str:
    """Reconstruct the public URL Twilio requested from request headers.

    Behind a TLS-terminating load balancer the request the app sees (``http``,
    internal host) differs from the public URL Twilio signed (``https``, public
    host), which is the most common cause of silent signature-validation
    failures.  This helper rebuilds that public URL.

    Header lookups are case-insensitive.  ``X-Forwarded-Proto`` and
    ``X-Forwarded-Host`` are only honored when *trust_proxy* is ``True`` — these
    headers are client-controllable and must only be trusted when the app sits
    behind a proxy that overwrites them.  When *trust_proxy* is ``False`` the
    scheme falls back to *default_scheme* and the host comes from the ``Host``
    header.

    Args:
        headers: The request headers (any case-insensitive mapping).
        path: The request path including any query string (e.g. ``"/twiml?x=1"``).
        trust_proxy: Honor ``X-Forwarded-*`` headers when ``True``.
        default_scheme: Scheme to assume when no trusted proxy scheme is found.

    Returns:
        The reconstructed absolute public URL, or the bare *path* if no host
        header is available.
    """
    lookup = {str(key).lower(): value for key, value in headers.items()}

    def _first(value: Any) -> str:
        """Take the first comma-separated entry of a forwarded header value."""
        return str(value).split(",")[0].strip()

    scheme = default_scheme
    host = lookup.get("host")
    if trust_proxy:
        forwarded_proto = lookup.get("x-forwarded-proto")
        if forwarded_proto:
            scheme = _first(forwarded_proto)
        forwarded_host = lookup.get("x-forwarded-host")
        if forwarded_host:
            host = _first(forwarded_host)

    if not host:
        return path

    if not path.startswith("/"):
        path = "/" + path
    return f"{scheme}://{str(host).strip()}{path}"


def compute_twilio_webhook_signature(
    *,
    auth_token: str,
    url: str,
    params: Mapping[str, Any] | Sequence[tuple[str, Any]],
) -> str:
    """Compute Twilio's HMAC-SHA1 webhook signature for form parameters."""
    signed = url + "".join(
        key + value
        for key, values in sorted(_twilio_signature_values(params).items())
        for value in sorted(set(values))
    )
    digest = hmac.new(auth_token.encode("utf-8"), signed.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def _twilio_signature_values(
    params: Mapping[str, Any] | Sequence[tuple[str, Any]],
) -> dict[str, list[str]]:
    values_by_key: dict[str, list[str]] = {}
    source = params.items() if isinstance(params, Mapping) else params
    for key, value in source:
        key_text = str(key)
        values = values_by_key.setdefault(key_text, [])
        if isinstance(value, (list, tuple)):
            for item in value:
                values.append("" if item is None else str(item))
        else:
            values.append("" if value is None else str(value))
    return values_by_key


# ── TwiML Gather fallback ────────────────────────────────────────


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


# ── DTMF output via TwiML ────────────────────────────────────────


def twiml_play_digits(digits: str) -> str:
    """Generate TwiML to play DTMF tones.

    Produces a ``<Response><Play digits="..."/></Response>`` fragment that
    Twilio will render as audible DTMF tones on the call.

    Characters outside :data:`VALID_DTMF_OUTPUT_CHARS` (DTMF digits plus
    ``W``/``w`` pauses) are stripped before rendering to prevent injecting
    arbitrary text into Twilio's ``digits`` attribute.

    Args:
        digits: Digit string to play (e.g. ``"1234#"``).

    Returns:
        Complete TwiML ``<Response>`` document as a string.
    """
    safe = quoteattr(sanitize_dtmf_digits(digits))
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Play digits={safe}/></Response>'


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
    safe_digits = quoteattr(sanitize_dtmf_digits(send_digits))
    safe_number = escape(phone_number)
    dial_attrs = ""
    if caller_id:
        dial_attrs = f" callerId={quoteattr(caller_id)}"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Dial{dial_attrs}>"
        f"<Number sendDigits={safe_digits}>{safe_number}</Number>"
        "</Dial></Response>"
    )


def twiml_dial_number(
    phone_number: str,
    *,
    caller_id: str | None = None,
    send_digits: str = "",
    preamble: str | None = None,
) -> str:
    """Generate TwiML to optionally speak a message and then dial a number."""
    safe_number = escape(phone_number)
    dial_attrs = ""
    if caller_id:
        dial_attrs = f" callerId={quoteattr(caller_id)}"
    number_attrs = ""
    safe_send_digits = sanitize_dtmf_digits(send_digits)
    if safe_send_digits:
        number_attrs = f" sendDigits={quoteattr(safe_send_digits)}"
    say = f"<Say>{escape(preamble)}</Say>" if preamble else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response>{say}<Dial{dial_attrs}>"
        f"<Number{number_attrs}>{safe_number}</Number>"
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
        f"action={quoteattr(action_url)}",
        f'timeout="{timeout}"',
        f"finishOnKey={quoteattr(finish_on_key)}",
        f"input={quoteattr(input_type)}",
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


def twiml_say_and_hangup(text: str) -> str:
    """Generate TwiML to say text and then hang up the call."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Say>{escape(text)}</Say><Hangup/></Response>"
    )
