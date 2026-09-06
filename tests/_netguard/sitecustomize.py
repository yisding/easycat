"""Block outbound (non-loopback) sockets in a child test process.

Loaded only when this directory is on ``PYTHONPATH``; the loopback exemption
keeps pytest plugins, xdist and local servers working.  Every caller MUST
canary this file -- ``sitecustomize`` is silently not imported under
``-I``/``-S``, or when another ``sitecustomize`` is earlier on ``sys.path``.
"""

import socket

_BLOCKED = "easycat test guard: outbound network blocked"
_LOOPBACK = ("127.0.0.1", "::1", "localhost", "")


def _check(address):  # type: ignore[no-untyped-def]
    host = address[0] if isinstance(address, tuple) and address else address
    if host not in _LOOPBACK:
        raise RuntimeError(f"{_BLOCKED}: {host!r}")


_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_getaddrinfo = socket.getaddrinfo


def _guarded_connect(self, address, *a, **kw):  # type: ignore[no-untyped-def]
    _check(address)
    return _connect(self, address, *a, **kw)


def _guarded_connect_ex(self, address, *a, **kw):  # type: ignore[no-untyped-def]
    _check(address)
    return _connect_ex(self, address, *a, **kw)


def _guarded_getaddrinfo(host, *a, **kw):  # type: ignore[no-untyped-def]
    _check(host)
    return _getaddrinfo(host, *a, **kw)


socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign]
socket.getaddrinfo = _guarded_getaddrinfo  # type: ignore[assignment]
