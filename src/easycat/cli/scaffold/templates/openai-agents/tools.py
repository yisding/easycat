"""Tools this agent can call. Plain functions — unit-test them directly."""

from datetime import datetime


def current_time() -> str:
    """Return the current local time as HH:MM."""
    return datetime.now().astimezone().strftime("%H:%M")
