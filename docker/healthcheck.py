#!/usr/bin/env python3
"""Container HEALTHCHECK probe for EasyCat server images.

Two modes, selected by environment variable — no image rebuild needed to
switch:

- ``EASYCAT_HEALTH_URL`` set (e.g. ``http://127.0.0.1:8080/health/ready``) —
  GET that URL and require an HTTP 2xx response. Use this for any server built on
  :class:`easycat.server.VoiceServer` (``run_webrtc_config_server()``, a
  custom ``VoiceServer.from_app(...)``, etc.) — those processes serve the
  real readiness endpoint documented in ``src/easycat/server/health.py``
  (``/health/ready`` fails while draining, at capacity, or — for a
  manifest-backed server — before the provider plan is loaded).
- ``EASYCAT_HEALTH_URL`` unset — falls back to a raw TCP connect against
  ``EASYCAT_WS_HOST``/``EASYCAT_WS_PORT`` (defaults ``127.0.0.1``/``8765``).
  This is the right fallback for the image's default CMD,
  ``examples/ws_server.py``: it speaks the raw ``websockets`` protocol only
  and does not serve an HTTP readiness endpoint, so a TCP-connect probe is
  the most honest liveness signal available without switching server
  scripts. It confirms the listener accepts connections; it does NOT confirm
  draining/capacity/manifest state the way ``/health/ready`` does.

Exits 0 (healthy) or 1 (unhealthy) per the Docker HEALTHCHECK contract.
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx

TIMEOUT_S = 2.0


async def _check_http(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=False) as client:
            response = await client.get(url)
        return response.is_success
    except (httpx.HTTPError, OSError, ValueError):
        return False


async def _check_tcp(host: str, port: int) -> bool:
    # 0.0.0.0 is a bind address, not a connect address — probe loopback instead.
    connect_host = "127.0.0.1" if host == "0.0.0.0" else host
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(connect_host, port),
            timeout=TIMEOUT_S,
        )
    except (OSError, TimeoutError, ValueError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


async def main() -> int:
    health_url = os.environ.get("EASYCAT_HEALTH_URL", "").strip()
    if health_url:
        ok = await _check_http(health_url)
    else:
        host = os.environ.get("EASYCAT_WS_HOST", "127.0.0.1")
        try:
            port = int(os.environ.get("EASYCAT_WS_PORT", "8765"))
        except ValueError:
            return 1
        ok = await _check_tcp(host, port)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
