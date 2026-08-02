"""Shared validation for session identifiers used in persistent paths."""

from __future__ import annotations

import re

_SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def validate_session_id(session_id: str) -> str:
    """Return a config-safe session id or raise ``ValueError``."""
    if not isinstance(session_id, str):
        raise ValueError("session_id must be a string")  # noqa: TRY004 domain-specific validation error
    if not session_id.strip():
        raise ValueError("session_id must not be empty")
    if _SESSION_ID_PATTERN.fullmatch(session_id) is None:
        raise ValueError(
            "session_id must be 1-128 ASCII letters, digits, '.', '_', or '-', "
            f"starting with a letter or digit: {session_id!r}"
        )
    return session_id


def validate_persistent_session_id(session_id: str) -> str:
    """Return a safe single path component while preserving legacy IDs."""
    if not isinstance(session_id, str):
        raise ValueError("session_id must be a string")  # noqa: TRY004 domain-specific validation error
    if not session_id.strip():
        raise ValueError("session_id must not be empty")
    if session_id in {".", ".."} or "/" in session_id or "\\" in session_id:
        raise ValueError(f"session_id must be a single path component: {session_id!r}")
    if "\0" in session_id:
        raise ValueError("session_id must not contain null bytes")
    return session_id
