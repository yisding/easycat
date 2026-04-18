"""Plan 10 — Exit-code contract stability.

Guards:
* Every mapped ``EASYCAT_Exxx`` in ``_CODE_TO_EXIT`` is in ``REGISTRY``.
* The ``exit-codes`` meta body lists every non-default code used by the
  CLI.
* Unlisted codes fall back to 1.

See ``TEST_PLANS.md`` §10.
"""

from __future__ import annotations

import re

from easycat.cli._errors import _CODE_TO_EXIT, exit_code_for
from easycat.cli.diagnose._codes import META_ENTRIES
from easycat.errors import REGISTRY

_DOCUMENTED_CODES = {0, 1, 2, 3, 4, 5, 6, 101, 130}


def test_every_mapped_code_has_registry_entry() -> None:
    """Catch a CLI-side mapping that references an error code that was
    deleted from the registry (or was never registered at all)."""
    for code in _CODE_TO_EXIT:
        assert code in REGISTRY, (
            f"{code} is in _CODE_TO_EXIT but has no REGISTRY entry — "
            f"`easycat explain` would fail for it"
        )


def test_unlisted_codes_default_to_one() -> None:
    assert exit_code_for("EASYCAT_E999") == 1
    assert exit_code_for("") == 1


def test_documented_exit_code_body_lists_every_non_default() -> None:
    body = META_ENTRIES["exit-codes"].body
    # Pull every numeric exit code referenced in _CODE_TO_EXIT, plus the
    # documented-but-unmapped ones (130 for SIGINT hard exit, 0 for OK).
    mapped_values = set(_CODE_TO_EXIT.values())
    expected = mapped_values | _DOCUMENTED_CODES
    for code in expected:
        # Each code should appear in the body as a line starting with a
        # small-int prefix; allow leading whitespace and dash delimiter.
        assert re.search(rf"\b{code}\b", body), (
            f"exit code {code} missing from the `exit-codes` meta body"
        )


def test_mapping_value_set_subset_of_documented() -> None:
    mapped_values = set(_CODE_TO_EXIT.values())
    # No mapping should produce an undocumented exit code.  New codes
    # must update both the mapping and the `exit-codes` doc body.
    assert mapped_values <= _DOCUMENTED_CODES, (
        f"mapped exit codes include undocumented values: "
        f"{sorted(mapped_values - _DOCUMENTED_CODES)}"
    )
