"""Guards for the generated error-code reference page."""

from __future__ import annotations

from easycat.errors import REGISTRY
from scripts.regen_error_codes import (
    ERROR_CODES_DOC,
    RANGES,
    _range_prefix,
    render_error_codes_doc,
)


def test_error_codes_page_matches_the_registry() -> None:
    assert ERROR_CODES_DOC.exists(), (
        "docs/reference/error-codes.md is missing. Run "
        "`uv run python scripts/regen_error_codes.py`."
    )
    assert ERROR_CODES_DOC.read_text(encoding="utf-8") == render_error_codes_doc(), (
        "docs/reference/error-codes.md is stale. Run `uv run python scripts/regen_error_codes.py`."
    )


def test_every_registered_code_is_documented_once() -> None:
    rendered = render_error_codes_doc()

    assert REGISTRY, "the error registry should not be empty"
    for code, entry in REGISTRY.items():
        assert rendered.count(f"### {code}\n") == 1, f"{code} is not documented exactly once"
        assert entry.cause in rendered, f"{code} cause is missing"
        assert entry.fix in rendered, f"{code} fix is missing"


def test_every_range_has_a_documented_heading() -> None:
    """A new namespace must be described, not silently bucketed elsewhere."""
    labels = {label for label, _title, _blurb in RANGES}
    used = {_range_prefix(code) for code in REGISTRY}

    assert used <= labels, f"undocumented code ranges: {sorted(used - labels)}"
