"""Refresh ``docs/reference/error-codes.md`` from the ``easycat.errors`` registry.

``src/easycat/errors.py`` is the single source of truth for every
``EASYCAT_Exxx`` code: each :func:`easycat.errors.register` call is both a
runtime factory and the documentation entry ``easycat explain`` reads. This
script renders that same registry as a browsable page so the codes are
readable without running the CLI, and so nothing has to be retyped by hand.

Run after adding or editing a code; ``--check`` exits non-zero if the page
would change, which is what CI should call.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from easycat.errors import REGISTRY, ErrorEntry

ROOT = Path(__file__).resolve().parent.parent
ERROR_CODES_DOC = ROOT / "docs" / "reference" / "error-codes.md"

# Namespace headings, mirroring the ranges documented in ``errors.py``.
RANGES: tuple[tuple[str, str, str], ...] = (
    ("E1xx", "Scaffolding", "`easycat init`, templates, and config JSON."),
    ("E2xx", "Environment", "`easycat doctor` checks: credentials, extras, reachability."),
    ("E3xx", "Runtime", "Session execution: providers, transports, turns."),
    ("E4xx", "Bundle and replay", "Debug bundles, journals, and replay inputs."),
    ("E5xx", "CLI usage", "Command invocation and argument problems."),
    ("E6xx", "Project manifest", "`easycat.toml` loading and validation."),
)

_HEADER = """\
# Error Code Reference

Every EasyCat failure with a stable identity carries an `EASYCAT_Exxx` code.
This page is **generated** from the registry in
[`src/easycat/errors.py`](../../src/easycat/errors.py) by
`scripts/regen_error_codes.py` — the same registry `easycat explain` reads, so
the two can never disagree. Do not edit it by hand; edit the `register(...)`
call and re-run the script.

For one code in the terminal, with your own context substituted into the
headline:

```bash
uv run easycat explain EASYCAT_E304
uv run easycat explain --list
```

Maintainers: after editing a `register(...)` call, refresh this page and verify
it is current with

```bash
uv run python scripts/regen_error_codes.py
uv run python scripts/regen_error_codes.py --check
```

Several fixes below tell you to verify credentials with
`uv run easycat doctor --env-file .env`; add `--json`
(`uv run easycat doctor --env-file .env --json`) when a script needs to read
the result instead of a person.

Codes are namespaced by range, and every entry below lists what the code
means, what causes it, and how to fix it. Headlines are `str.format` templates:
the raising code substitutes its own context, which `easycat explain` also does
for the fix text.
"""


def _sorted_entries() -> list[ErrorEntry]:
    return [REGISTRY[code] for code in sorted(REGISTRY)]


def _range_prefix(code: str) -> str:
    """Return the ``Exxx`` namespace label for *code* (``EASYCAT_E304`` → ``E3xx``)."""
    digits = code.removeprefix("EASYCAT_E")
    return f"E{digits[0]}xx"


def render_error_codes_doc() -> str:
    entries = _sorted_entries()
    by_range: dict[str, list[ErrorEntry]] = {label: [] for label, _, _ in RANGES}
    unclassified: list[ErrorEntry] = []
    for entry in entries:
        bucket = by_range.get(_range_prefix(entry.code))
        if bucket is None:
            unclassified.append(entry)
        else:
            bucket.append(entry)
    if unclassified:
        codes = ", ".join(entry.code for entry in unclassified)
        raise ValueError(
            f"No documented range for {codes}. Add the range to RANGES in "
            "scripts/regen_error_codes.py alongside the errors.py namespace comment."
        )

    lines = [_HEADER]
    for label, title, blurb in RANGES:
        lines += [f"## {label} — {title}", "", blurb, ""]
        for entry in by_range[label]:
            lines += [
                f"### {entry.code}",
                "",
                f"**{entry.headline}**",
                "",
                f"- **Cause:** {entry.cause}",
                f"- **Fix:** {entry.fix}",
            ]
            if entry.example:
                lines.append(f"- **Example:** `{entry.example}`")
            if entry.related:
                related = ", ".join(f"[{code}](#{code.lower()})" for code in entry.related)
                lines.append(f"- **Related:** {related}")
            lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if docs/reference/error-codes.md would change.",
    )
    args = parser.parse_args(argv)

    rendered = render_error_codes_doc()
    current = ERROR_CODES_DOC.read_text(encoding="utf-8") if ERROR_CODES_DOC.exists() else None
    if current == rendered:
        return 0
    rel = ERROR_CODES_DOC.relative_to(ROOT).as_posix()
    if args.check:
        print(
            f"Stale generated file: {rel}. Run `uv run python scripts/regen_error_codes.py`.",
            file=sys.stderr,
        )
        return 1
    ERROR_CODES_DOC.write_text(rendered, encoding="utf-8")
    print(f"wrote {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
