"""Whitespace-normalizing assertion helpers for docs/markdown prose checks.

Guard tests that read prose out of Markdown files often assert that a
sentence or phrase appears (or does not appear) in the file. Markdown
line-wrapping means the same sentence can be split across lines, or indented
under a list item, without changing its rendered meaning. A naive
``"needle" in haystack`` check is brittle to those cosmetic reflows:
rewording or re-wrapping a paragraph can break the test even though nothing
meaningful changed about the claim being asserted.

These helpers collapse all runs of whitespace (including embedded newlines
and indentation) to a single space before matching, so tests can assert on
the *content* of the prose without also pinning its exact line breaks or
indentation. This is a mechanical robustness change, not a loosening of what
the docs must say -- the semantic needle text is preserved verbatim, only
its whitespace is normalized before comparison.

Do NOT use these for exact command strings, code blocks, or generated-block
markers (e.g. ``<!-- BEGIN GENERATED -->``) -- those should keep exact
``in``/``not in`` checks, since their exact formatting is the thing under
test.
"""

from __future__ import annotations

import re

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_whitespace(text: str) -> str:
    """Collapse all runs of whitespace to a single space and strip the ends."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def assert_prose_in(needle: str, haystack: str, *, msg: str | None = None) -> None:
    """Assert ``needle`` appears in ``haystack`` after whitespace normalization.

    Both strings are normalized (runs of whitespace, including newlines and
    indentation, collapsed to a single space) before the containment check,
    so the needle does not need to match the haystack's exact line-wrapping.
    """
    normalized_needle = normalize_whitespace(needle)
    normalized_haystack = normalize_whitespace(haystack)
    if normalized_needle not in normalized_haystack:
        raise AssertionError(
            (msg or "expected prose not found in text")
            + f"\n\nnormalized needle:\n  {normalized_needle!r}"
        )


def assert_prose_not_in(needle: str, haystack: str, *, msg: str | None = None) -> None:
    """Assert ``needle`` does not appear in ``haystack`` after normalization.

    Mirrors :func:`assert_prose_in` for negative assertions -- useful when a
    guard test checks that stale prose was removed/reworded away.
    """
    normalized_needle = normalize_whitespace(needle)
    normalized_haystack = normalize_whitespace(haystack)
    if normalized_needle in normalized_haystack:
        raise AssertionError(
            (msg or "unexpected prose found in text")
            + f"\n\nnormalized needle:\n  {normalized_needle!r}"
        )
