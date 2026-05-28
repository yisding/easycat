"""Refresh auto-generated blocks in teaching-chapter READMEs.

Each chapter README under ``docs/teaching/NN-*/README.md`` may contain
HTML-comment markers delimiting blocks that this script keeps in sync
with the chapter's source code:

* Embedded function/class bodies extracted from a sibling source file::

      <!-- BEGIN auto:snippet src=main.py symbol=blocking_agent -->
      ```python
      ...auto-filled function body...
      ```
      <!-- END auto:snippet -->

* The unified diff against the previous chapter's source::

      <!-- BEGIN auto:diff prev=04-vad-preroll src=main.py -->
      <details>
      <summary>...</summary>

      ```diff
      ...auto-filled unified diff...
      ```

      </details>
      <!-- END auto:diff -->

* Source-line references that should track edits to the file::

      <!-- auto:linerange src=main.py symbol=blocking_agent -->`L83-L92`

  The script overwrites everything between the opening tag and the
  closing backtick with the symbol's current line range.

* Markdown links whose ``#Lxx-Lyy`` anchor should track the symbol::

      <!-- auto:linkhash src=main.py symbol=blocking_agent -->[`blocking_agent`](./main.py#L83-L92)

  The script rewrites the line-range fragment of the link that immediately
  follows the marker.

The blocks render fine on GitHub (the markers are HTML comments) and
also display correctly inside MkDocs. Run after editing any chapter
``main.py``; ``--check`` exits non-zero if any block would change,
which is what CI should call.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEACHING = ROOT / "docs" / "teaching"

CHAPTER_RE = re.compile(r"^\d{2}-")

SNIPPET_RE = re.compile(
    r"(?P<begin><!-- BEGIN auto:snippet (?P<attrs>[^>]*?) -->)"
    r"(?P<body>.*?)"
    r"(?P<end><!-- END auto:snippet -->)",
    re.DOTALL,
)
DIFF_RE = re.compile(
    r"(?P<begin><!-- BEGIN auto:diff (?P<attrs>[^>]*?) -->)"
    r"(?P<body>.*?)"
    r"(?P<end><!-- END auto:diff -->)",
    re.DOTALL,
)
LINERANGE_RE = re.compile(r"(<!-- auto:linerange (?P<attrs>[^>]*?) -->)`L\d+(?:-L\d+)?`")
LINKHASH_RE = re.compile(
    r"(<!-- auto:linkhash (?P<attrs>[^>]*?) -->\s*\[[^\]]*\]\([^)\s]+?\.py)"
    r"#L\d+(?:-L\d+)?"
)
ATTR_RE = re.compile(r"(\w+)=(?:\"([^\"]*)\"|(\S+))")


@dataclass
class Chapter:
    path: Path

    @property
    def slug(self) -> str:
        return self.path.name


def discover_chapters() -> list[Chapter]:
    chapters = [
        Chapter(p) for p in sorted(TEACHING.iterdir()) if p.is_dir() and CHAPTER_RE.match(p.name)
    ]
    return chapters


def parse_attrs(raw: str) -> dict[str, str]:
    return {k: (q or u) for k, q, u in ATTR_RE.findall(raw)}


def extract_symbol(source: str, symbol: str) -> tuple[str, int, int]:
    """Return (source_text, start_line, end_line) for a top-level symbol."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == symbol:
                lines = source.splitlines()
                start = node.lineno
                end = node.end_lineno or start
                return "\n".join(lines[start - 1 : end]) + "\n", start, end
    raise KeyError(f"symbol {symbol!r} not found")


def render_snippet(chapter: Chapter, attrs: dict[str, str]) -> str:
    src_path = chapter.path / attrs["src"]
    symbol = attrs["symbol"]
    lang = attrs.get("lang", "python")
    body, _, _ = extract_symbol(src_path.read_text(), symbol)
    return f"\n```{lang}\n{body}```\n"


def render_diff(chapter: Chapter, attrs: dict[str, str]) -> str:
    prev_slug = attrs["prev"]
    src_name = attrs.get("src", "main.py")
    prev_path = TEACHING / prev_slug / src_name
    cur_path = chapter.path / src_name
    prev_lines = prev_path.read_text().splitlines(keepends=True)
    cur_lines = cur_path.read_text().splitlines(keepends=True)
    rel_prev = prev_path.relative_to(ROOT).as_posix()
    rel_cur = cur_path.relative_to(ROOT).as_posix()
    diff = difflib.unified_diff(prev_lines, cur_lines, fromfile=rel_prev, tofile=rel_cur, n=3)
    diff_text = "".join(diff).rstrip() + "\n"
    summary = f"Full unified diff vs <code>{prev_slug}/{src_name}</code> (auto-generated)"
    return f"\n<details>\n<summary>{summary}</summary>\n\n```diff\n{diff_text}```\n\n</details>\n"


def render_linerange(chapter: Chapter, attrs: dict[str, str]) -> str:
    src_path = chapter.path / attrs["src"]
    _, start, end = extract_symbol(src_path.read_text(), attrs["symbol"])
    return f"`L{start}-L{end}`" if end != start else f"`L{start}`"


def render_linkhash(chapter: Chapter, attrs: dict[str, str], prefix: str) -> str:
    src_path = chapter.path / attrs["src"]
    _, start, end = extract_symbol(src_path.read_text(), attrs["symbol"])
    anchor = f"#L{start}-L{end}" if end != start else f"#L{start}"
    return prefix + anchor


def regen_readme(chapter: Chapter) -> tuple[str, str]:
    readme_path = chapter.path / "README.md"
    original = readme_path.read_text()

    def _snippet_sub(m: re.Match[str]) -> str:
        attrs = parse_attrs(m.group("attrs"))
        return m.group("begin") + render_snippet(chapter, attrs) + m.group("end")

    def _diff_sub(m: re.Match[str]) -> str:
        attrs = parse_attrs(m.group("attrs"))
        return m.group("begin") + render_diff(chapter, attrs) + m.group("end")

    def _linerange_sub(m: re.Match[str]) -> str:
        attrs = parse_attrs(m.group("attrs"))
        return m.group(1) + render_linerange(chapter, attrs)

    def _linkhash_sub(m: re.Match[str]) -> str:
        attrs = parse_attrs(m.group("attrs"))
        return render_linkhash(chapter, attrs, m.group(1))

    updated = SNIPPET_RE.sub(_snippet_sub, original)
    updated = DIFF_RE.sub(_diff_sub, updated)
    updated = LINERANGE_RE.sub(_linerange_sub, updated)
    updated = LINKHASH_RE.sub(_linkhash_sub, updated)
    return original, updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chapter", help="Restrict to one chapter slug, e.g. 05-blocking-agent")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if any README would change. Writes nothing.",
    )
    args = parser.parse_args(argv)

    chapters = discover_chapters()
    if args.chapter:
        chapters = [c for c in chapters if c.slug == args.chapter]
        if not chapters:
            print(f"no chapter matches {args.chapter!r}", file=sys.stderr)
            return 2

    drift = False
    for chapter in chapters:
        readme = chapter.path / "README.md"
        if not readme.exists():
            continue
        original, updated = regen_readme(chapter)
        if original == updated:
            continue
        if args.check:
            drift = True
            print(f"would update {readme.relative_to(ROOT)}", file=sys.stderr)
            sys.stderr.write(
                "".join(
                    difflib.unified_diff(
                        original.splitlines(keepends=True),
                        updated.splitlines(keepends=True),
                        fromfile=str(readme),
                        tofile=f"{readme} (regenerated)",
                    )
                )
            )
        else:
            readme.write_text(updated)
            print(f"updated {readme.relative_to(ROOT)}")

    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
