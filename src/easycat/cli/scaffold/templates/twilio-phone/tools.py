"""Tools this agent can call. Plain functions — unit-test them directly."""


def take_message(name: str, message: str) -> str:
    """Record a caller message for later follow-up."""
    return f"Message saved for {name}: {message}"
