"""Regression tests for the leaf networking helpers in :mod:`easycat._net`.

These helpers were consolidated out of the heavy ``transports.webrtc`` /
``transports.websocket`` modules (QW10). The merge widened behaviour: the
loopback check now accepts ``None`` (returning ``False``) and strips ``[]``
brackets off IPv6 literals, and the auth-token normalizer treats blank /
whitespace-only tokens as no token at all.
"""

from __future__ import annotations

from easycat._net import is_loopback_host, normalize_auth_token


def test_is_loopback_host_none_is_false() -> None:
    assert is_loopback_host(None) is False


def test_is_loopback_host_empty_string_is_false() -> None:
    assert is_loopback_host("") is False


def test_is_loopback_host_localhost() -> None:
    assert is_loopback_host("localhost") is True


def test_is_loopback_host_ipv4_loopback_variants() -> None:
    assert is_loopback_host("127.0.0.1") is True
    assert is_loopback_host("127.5.4.3") is True


def test_is_loopback_host_ipv6_loopback_variants() -> None:
    assert is_loopback_host("::1") is True
    assert is_loopback_host("[::1]") is True
    assert is_loopback_host("::ffff:127.0.0.1") is True


def test_is_loopback_host_public_hosts_are_false() -> None:
    assert is_loopback_host("example.com") is False
    assert is_loopback_host("0.0.0.0") is False


def test_normalize_auth_token_blank_values_are_none() -> None:
    assert normalize_auth_token(None) is None
    assert normalize_auth_token("") is None
    assert normalize_auth_token("   ") is None


def test_normalize_auth_token_passthrough_real_token() -> None:
    assert normalize_auth_token("s3cret") == "s3cret"
