"""Routing logic this workflow uses. Plain functions — unit-test them directly."""

TECH_TERMS = ("audio", "browser", "setup", "install")


def pick_specialist(text: str) -> str:
    """Return which specialist should handle this turn."""
    return "technical" if any(word in text.lower() for word in TECH_TERMS) else "billing"
