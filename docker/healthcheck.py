#!/usr/bin/env python3
"""Container HEALTHCHECK probe for EasyCat server images.

Two modes, selected by environment variable — no image rebuild needed to
switch:

- ``EASYCAT_HEALTH_URL`` set (e.g. ``http://127.0.0.1:8080/health/ready``) —
  GET that URL and require HTTP 200. Use this for any server built on
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

import os
import socket
import sys
import urllib.error
import urllib.request

TIMEOUT_S = 2.0


def _check_http(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as response:  # noqa: S310
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _check_tcp(host: str, port: int) -> bool:
    # 0.0.0.0 is a bind address, not a connect address — probe loopback instead.
    connect_host = "127.0.0.1" if host == "0.0.0.0" else host
    try:
        with socket.create_connection((connect_host, port), timeout=TIMEOUT_S):
            return True
    except OSError:
        return False


def main() -> int:
    health_url = os.environ.get("EASYCAT_HEALTH_URL", "").strip()
    if health_url:
        ok = _check_http(health_url)
    else:
        host = os.environ.get("EASYCAT_WS_HOST", "127.0.0.1")
        port = int(os.environ.get("EASYCAT_WS_PORT", "8765"))
        ok = _check_tcp(host, port)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
