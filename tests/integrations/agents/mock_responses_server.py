"""Mock ASGI server for the OpenAI Responses API.

Provides a minimal in-process server for testing
:class:`ResponsesAPIBridge` without network I/O.  Uses raw ASGI
(no starlette dependency) and runs via ``httpx.ASGITransport``.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4


class MockResponsesServer:
    """ASGI application that mimics ``POST /v1/responses`` SSE streams.

    Attributes
    ----------
    received_requests:
        List of parsed request bodies for assertion.
    easycat_metadata:
        Metadata dict returned in response objects (for capability
        discovery tests).
    tool_calls:
        List of ``(name, arguments, output)`` tuples to simulate
        function call output items in the SSE stream.
    response_text:
        Static response text to stream.  Defaults to ``"Hello from mock!"``.
    fail_on_next:
        If set, the next request returns a ``response.failed`` event
        with this message.
    status_code_override:
        If set, respond with this HTTP status instead of 200.
    """

    def __init__(self) -> None:
        self.received_requests: list[dict[str, Any]] = []
        self.easycat_metadata: dict[str, Any] = {}
        self.tool_calls: list[tuple[str, str, str]] = []
        self.response_text: str = "Hello from mock!"
        self.fail_on_next: str | None = None
        self.status_code_override: int | None = None
        self._responses: dict[str, dict[str, Any]] = {}
        self._turn_count: int = 0

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        if scope["type"] != "http":
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")

        if method == "POST" and path == "/v1/responses":
            await self._handle_responses(scope, receive, send)
        else:
            await self._send_404(send)

    async def _handle_responses(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        # Read request body.
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break

        request_data = json.loads(body) if body else {}
        self.received_requests.append(request_data)
        self._turn_count += 1

        # Check for forced HTTP error.
        if self.status_code_override is not None:
            status = self.status_code_override
            self.status_code_override = None
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        [b"content-type", b"application/json"],
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": json.dumps({"error": {"message": "Mock error"}}).encode(),
                }
            )
            return

        response_id = f"resp_{uuid4().hex[:12]}"

        # Store for chaining tests.
        self._responses[response_id] = {
            "id": response_id,
            "request": request_data,
        }

        # Build SSE event stream.
        events = self._build_sse_events(response_id, request_data)

        # Send response.
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    [b"content-type", b"text/event-stream"],
                    [b"cache-control", b"no-cache"],
                ],
            }
        )

        sse_body = "\n".join(events) + "\n"
        await send(
            {
                "type": "http.response.body",
                "body": sse_body.encode(),
            }
        )

    def _build_sse_events(
        self,
        response_id: str,
        request_data: dict[str, Any],
    ) -> list[str]:
        events: list[str] = []

        # Check for forced failure.
        if self.fail_on_next:
            msg = self.fail_on_next
            self.fail_on_next = None
            fail_data = {
                "type": "response.failed",
                "response": {
                    "id": response_id,
                    "error": {"message": msg},
                },
            }
            events.append(f"data: {json.dumps(fail_data)}")
            return events

        response_obj: dict[str, Any] = {
            "id": response_id,
            "model": request_data.get("model", "unknown"),
            "output": [],
        }

        if self.easycat_metadata:
            response_obj["metadata"] = self.easycat_metadata

        # Emit tool calls if configured.
        for name, arguments, output in self.tool_calls:
            call_id = f"call_{uuid4().hex[:8]}"

            # Function call output item.
            fc_item = {
                "type": "function_call",
                "id": f"fc_{uuid4().hex[:8]}",
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
            }
            events.append(
                f"data: {json.dumps({'type': 'response.output_item.done', 'item': fc_item})}"
            )
            response_obj["output"].append(fc_item)

            # Function call result.
            fco_item = {
                "type": "function_call_output",
                "id": f"fco_{uuid4().hex[:8]}",
                "call_id": call_id,
                "output": output,
            }
            events.append(
                f"data: {json.dumps({'type': 'response.output_item.done', 'item': fco_item})}"
            )
            response_obj["output"].append(fco_item)

        # Emit text deltas.
        text = self.response_text
        if text:
            # Split into word-level chunks for realism.
            words = text.split(" ")
            for i, word in enumerate(words):
                chunk = word if i == len(words) - 1 else word + " "
                events.append(
                    f"data: {json.dumps({'type': 'response.output_text.delta', 'delta': chunk})}"
                )

        # Response completed.
        events.append(
            f"data: {json.dumps({'type': 'response.completed', 'response': response_obj})}"
        )

        return events

    async def _send_404(self, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 404,
                "headers": [[b"content-type", b"application/json"]],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"error": "not found"}',
            }
        )
