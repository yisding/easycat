"""Minimal async Telnyx Call Control API client.

EasyCat deliberately avoids the heavyweight ``telnyx`` SDK: every command the
telephony integration needs is one authenticated POST against
``https://api.telnyx.com/v2``, which aiohttp (already required by the
``telnyx`` extra) handles cleanly.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Mapping
from typing import Any

import aiohttp

TELNYX_API_BASE_URL = "https://api.telnyx.com/v2"

_RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_RETRY_BACKOFF_S = 8.0

logger = logging.getLogger(__name__)


class TelnyxApiError(RuntimeError):
    """Raised when the Telnyx Call Control API returns an error response."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"Telnyx API error {status}: {detail}")
        self.status = status
        self.detail = detail


class TelnyxCallControlClient:
    """Async client for the Telnyx Call Control v2 command surface.

    Args:
        api_key: Bearer token from ``TELNYX_API_KEY``.
        base_url: Override for tests / private gateways.
        timeout_s: Per-request timeout in seconds.
        max_retries: Maximum retry attempts after an initial transient failure.
        retry_backoff_s: Base exponential backoff delay between retries.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = TELNYX_API_BASE_URL,
        timeout_s: float = 15.0,
        max_retries: int = 2,
        retry_backoff_s: float = 0.5,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must be non-empty")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if retry_backoff_s < 0:
            raise ValueError("retry_backoff_s must be non-negative")
        self._base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._retry_backoff_s = retry_backoff_s
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=self._timeout_s),
            )
        return self._session

    async def close(self) -> None:
        """Release the underlying HTTP session."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        retryable = bool(payload.get("command_id"))

        last_error: Exception | None = None
        for attempt in range(1 + self._max_retries):
            session = await self._ensure_session()
            try:
                async with session.post(url, json=dict(payload)) as response:
                    body = await response.json(content_type=None)
                    if response.status >= 400:
                        detail = _error_detail(body)
                        error = TelnyxApiError(response.status, detail)
                        if response.status not in _RETRYABLE_HTTP_STATUSES:
                            raise error
                        last_error = error
                    else:
                        return body if isinstance(body, dict) else {}
            except aiohttp.ClientError as exc:
                last_error = exc

            if attempt < self._max_retries:
                if not retryable:
                    logger.warning(
                        "Telnyx API command without command_id failed (%s); "
                        "not retrying to avoid duplicate non-idempotent commands",
                        last_error,
                    )
                    break
                delay = min(
                    self._retry_backoff_s * (2**attempt) * random.uniform(0.5, 1.0),
                    _MAX_RETRY_BACKOFF_S,
                )
                logger.warning(
                    "Telnyx API attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt + 1,
                    1 + self._max_retries,
                    last_error,
                    delay,
                )
                await asyncio.sleep(delay)

        assert last_error is not None
        raise last_error

    async def answer(self, call_control_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Answer an inbound call and attach the bidirectional media stream."""
        return await self._post(f"/calls/{call_control_id}/actions/answer", payload)

    async def dial(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Place an outbound call with media-stream parameters."""
        return await self._post("/calls", payload)

    async def hangup(
        self,
        call_control_id: str,
        *,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Hang up a live call."""
        body: dict[str, Any] = {}
        if command_id:
            body["command_id"] = command_id
        return await self._post(f"/calls/{call_control_id}/actions/hangup", body)

    async def transfer(
        self,
        call_control_id: str,
        to: str,
        *,
        from_: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Transfer a live call to another destination."""
        body: dict[str, Any] = {"to": to}
        if from_:
            body["from"] = from_
        if command_id:
            body["command_id"] = command_id
        return await self._post(f"/calls/{call_control_id}/actions/transfer", body)

    async def send_dtmf(
        self,
        call_control_id: str,
        digits: str,
        *,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        """Send DTMF digits on a live call."""
        body: dict[str, Any] = {"digits": digits}
        if command_id:
            body["command_id"] = command_id
        return await self._post(f"/calls/{call_control_id}/actions/send_dtmf", body)

    async def send_sms(
        self,
        *,
        to: str,
        from_: str,
        text: str,
        connection_id: str | None = None,
    ) -> dict[str, Any]:
        """Send an SMS via ``POST /v2/messages``."""
        body: dict[str, Any] = {"to": to, "from": from_, "text": text}
        if connection_id:
            body["connection_id"] = connection_id
        return await self._post("/messages", body)


def _error_detail(body: Any) -> str:
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                title = first.get("title") or first.get("detail") or "unknown error"
                code = first.get("code")
                return f"{code}: {title}" if code else str(title)
        return str(body.get("detail") or body)
    return str(body)
