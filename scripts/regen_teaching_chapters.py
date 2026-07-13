"""Refresh auto-generated blocks in teaching-chapter pages.

Each chapter README and exercise page under ``docs/teaching/NN-*`` may
contain HTML-comment markers delimiting blocks that this script keeps in
sync with the chapter's source code and ladder order:

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

  Add ``trim_blank_context=true`` when the rendered Markdown should
  remove unified-diff's single-space prefix from otherwise blank
  context lines (a display-only normalization for whitespace checks).

* Source-line references that should track edits to the file::

      <!-- auto:linerange src=main.py symbol=blocking_agent -->`L83-L92`

  The script overwrites everything between the opening tag and the
  closing backtick with the symbol's current line range.

* Markdown links whose ``#Lxx-Lyy`` anchor should track the symbol::

      <!-- auto:linkhash src=main.py symbol=blocking_agent -->[`blocking_agent`](./main.py#L83-L92)

  The script rewrites the line-range fragment of the link that immediately
  follows the marker.

* A chapter breadcrumb derived from the ordered chapter directories::

      <!-- BEGIN auto:navigation -->
      [← Chapter 4 — VAD + Pre-roll](../04-vad-preroll/) ·
      [Teaching ladder](../) ·
      [Exercises](./EXERCISES.md) ·
      [Chapter 6 — Streaming Agent + Sentence TTS →](../06-streaming-agent/)
      <!-- END auto:navigation -->

  The renderer inserts this block immediately after the H1 when it is missing
  and refreshes adjacent chapter titles/links when the ladder changes. Exercise
  pages get a companion block linking back to the narrative, index, and next
  chapter.

* A chapter-local hardware-free checkpoint derived from
  ``docs/teaching/offline_spine.py``::

      <!-- BEGIN auto:offline-checkpoint -->
      > **Hardware-free checkpoint:** prove `first-audio outcomes` without a
      > microphone, speakers, or provider credentials:
      >
      > ```bash
      > uv run python docs/teaching/05-blocking-agent/tts_outcome_probe.py
      > ```
      >
      > **Evidence to find:** no chunks, all rejected, and first accepted audio
      > produce three distinct outcomes.
      >
      > [See all 16 checkpoints](../#hardware-free-checkpoint-spine).
      <!-- END auto:offline-checkpoint -->

  The renderer inserts this block after the chapter's opening summary and
  refreshes the concept and command from the spine's single manifest.

* A narrative-to-practice handoff before the chapter's next-step section::

      <!-- BEGIN auto:practice-handoff -->
      ## Practice and self-check

      Work through the chapter exercises, then try their closing self-check
      from memory.
      <!-- END auto:practice-handoff -->

* A completion footer at the end of each exercise page::

      <!-- BEGIN auto:exercise-completion -->
      ---
      Self-check complete? Prepare the cumulative spine, then replay it through
      this chapter:

      ```bash
      uv sync --extra quickstart --group dev
      uv run python docs/teaching/offline_spine.py --run --through 5 --jobs 4 --show-evidence
      ```

      - Review the chapter narrative
      - Continue to Chapter 6 →
      <!-- END auto:exercise-completion -->

  Together these blocks make the narrative → practice → recall → next-chapter
  loop explicit while deriving all chapter links from ladder order.

The blocks render fine on GitHub (the markers are HTML comments) and
also display correctly inside MkDocs. Run after editing any chapter
``main.py``; ``--check`` exits non-zero if any block would change,
which is what CI should call.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import functools
import importlib.util
import re
import sys
import textwrap
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
NAVIGATION_RE = re.compile(
    r"(?P<begin><!-- BEGIN auto:navigation -->)"
    r"(?P<body>.*?)"
    r"(?P<end><!-- END auto:navigation -->)",
    re.DOTALL,
)
OFFLINE_CHECKPOINT_RE = re.compile(
    r"(?P<begin><!-- BEGIN auto:offline-checkpoint -->)"
    r"(?P<body>.*?)"
    r"(?P<end><!-- END auto:offline-checkpoint -->)",
    re.DOTALL,
)
PRACTICE_HANDOFF_RE = re.compile(
    r"(?P<begin><!-- BEGIN auto:practice-handoff -->)"
    r"(?P<body>.*?)"
    r"(?P<end><!-- END auto:practice-handoff -->)",
    re.DOTALL,
)
EXERCISE_COMPLETION_RE = re.compile(
    r"(?P<begin><!-- BEGIN auto:exercise-completion -->)"
    r"(?P<body>.*?)"
    r"(?P<end><!-- END auto:exercise-completion -->)",
    re.DOTALL,
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
                # ast reports node.lineno at the `def`/`class` line, which drops
                # any decorators above it. Start at the first decorator so the
                # snippet stays copy-paste correct (e.g. @dataclass).
                start = node.lineno
                if node.decorator_list:
                    start = min(start, node.decorator_list[0].lineno)
                end = node.end_lineno or start
                return "\n".join(lines[start - 1 : end]) + "\n", start, end
    raise KeyError(f"symbol {symbol!r} not found")


def _resolve_child_path(base: Path, raw_path: str, attr_name: str) -> Path:
    """Resolve a marker-supplied path while keeping it under ``base``."""
    base = base.resolve()
    candidate = (base / raw_path).resolve(strict=False)
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        rel_base = base.relative_to(ROOT).as_posix()
        raise ValueError(f"{attr_name}={raw_path!r} escapes {rel_base}") from exc
    return candidate


def render_snippet(chapter: Chapter, attrs: dict[str, str]) -> str:
    src_path = _resolve_child_path(chapter.path, attrs["src"], "src")
    symbol = attrs["symbol"]
    lang = attrs.get("lang", "python")
    body, _, _ = extract_symbol(src_path.read_text(), symbol)
    return f"\n```{lang}\n{body}```\n"


def render_diff(chapter: Chapter, attrs: dict[str, str]) -> str:
    prev_slug = attrs["prev"]
    src_name = attrs.get("src", "main.py")
    prev_src = attrs.get("prev_src", src_name)
    prev_chapter = _resolve_child_path(TEACHING, prev_slug, "prev")
    prev_path = _resolve_child_path(prev_chapter, prev_src, "prev_src")
    cur_path = _resolve_child_path(chapter.path, src_name, "src")
    prev_lines = prev_path.read_text().splitlines(keepends=True)
    cur_lines = cur_path.read_text().splitlines(keepends=True)
    rel_prev = prev_path.relative_to(ROOT).as_posix()
    rel_cur = cur_path.relative_to(ROOT).as_posix()
    diff = difflib.unified_diff(prev_lines, cur_lines, fromfile=rel_prev, tofile=rel_cur, n=3)
    diff_text = "".join(diff)
    if attrs.get("trim_blank_context") == "true":
        # Unified diffs prefix blank context lines with one space. That is
        # correct patch syntax but becomes trailing whitespace inside the
        # rendered Markdown fence, so opt affected teaching blocks into the
        # display-only normalization when `git diff --check` would otherwise
        # reject a regenerated README.
        diff_text = re.sub(r"(?m)^ $", "", diff_text)
    diff_text = diff_text.rstrip() + "\n"
    summary = f"Full unified diff vs <code>{prev_slug}/{prev_src}</code> (auto-generated)"
    return f"\n<details>\n<summary>{summary}</summary>\n\n```diff\n{diff_text}```\n\n</details>\n"


def render_linerange(chapter: Chapter, attrs: dict[str, str]) -> str:
    src_path = _resolve_child_path(chapter.path, attrs["src"], "src")
    _, start, end = extract_symbol(src_path.read_text(), attrs["symbol"])
    return f"`L{start}-L{end}`" if end != start else f"`L{start}`"


def render_linkhash(chapter: Chapter, attrs: dict[str, str], prefix: str) -> str:
    src_path = _resolve_child_path(chapter.path, attrs["src"], "src")
    _, start, end = extract_symbol(src_path.read_text(), attrs["symbol"])
    anchor = f"#L{start}-L{end}" if end != start else f"#L{start}"
    return prefix + anchor


def _chapter_title(chapter: Chapter) -> str:
    readme = chapter.path / "README.md"
    heading = readme.read_text(encoding="utf-8").splitlines()[0]
    if not heading.startswith("# "):
        raise ValueError(f"{readme.relative_to(ROOT)} must start with an H1")
    return heading.removeprefix("# ")


def _chapter_position(chapter: Chapter) -> tuple[list[Chapter], int]:
    chapters = discover_chapters()
    try:
        index = next(i for i, candidate in enumerate(chapters) if candidate.path == chapter.path)
    except StopIteration as exc:
        raise ValueError(f"unknown teaching chapter: {chapter.slug}") from exc
    return chapters, index


def render_navigation(chapter: Chapter) -> str:
    chapters, index = _chapter_position(chapter)

    links: list[str] = []
    if index:
        previous = chapters[index - 1]
        links.append(f"[← {_chapter_title(previous)}](../{previous.slug}/)")
    links.append("[Teaching ladder](../)")
    links.append("[Exercises](./EXERCISES.md)")
    if index + 1 < len(chapters):
        following = chapters[index + 1]
        links.append(f"[{_chapter_title(following)} →](../{following.slug}/)")
    return " · ".join(links)


def render_exercise_navigation(chapter: Chapter) -> str:
    chapters, index = _chapter_position(chapter)

    links = ["[← Chapter narrative](./README.md)", "[Teaching ladder](../)"]
    if index + 1 < len(chapters):
        following = chapters[index + 1]
        links.append(f"[{_chapter_title(following)} →](../{following.slug}/)")
    return " · ".join(links)


def render_practice_handoff() -> str:
    return (
        "## Practice and self-check\n\n"
        "Work through [the chapter exercises](./EXERCISES.md), then try their closing\n"
        "self-check from memory. If an answer is weak, rerun the hardware-free\n"
        "checkpoint or revisit the section that owns the gap."
    )


def render_exercise_completion(chapter: Chapter) -> str:
    chapters, index = _chapter_position(chapter)
    checkpoint = _offline_checkpoint_for(chapter)
    links = ["[Review the chapter narrative](./README.md)"]
    if index + 1 < len(chapters):
        following = chapters[index + 1]
        links.append(f"[Continue to {_chapter_title(following)} →](../{following.slug}/)")
    else:
        links.append("[Return to the teaching ladder](../)")
    return (
        "---\nSelf-check complete? Prepare the cumulative spine, then replay it through "
        "this chapter:\n\n"
        "```bash\n"
        f"{checkpoint['setup_command']}\n"
        "uv run python docs/teaching/offline_spine.py --run "
        f"--through {checkpoint['chapter']} --jobs 4 --show-evidence\n"
        "```\n\n" + "\n".join(f"- {link}" for link in links)
    )


@functools.cache
def _offline_checkpoints_by_folder() -> dict[str, dict[str, object]]:
    spine_path = TEACHING / "offline_spine.py"
    module_name = "_easycat_teaching_offline_spine_for_regen"
    spec = importlib.util.spec_from_file_location(module_name, spine_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load {spine_path.relative_to(ROOT)}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    rows = module.catalog()
    checkpoints = {str(row["folder"]): row for row in rows}
    if len(checkpoints) != len(rows):
        raise ValueError(f"duplicate chapter folders in {spine_path.relative_to(ROOT)}")
    return checkpoints


def _offline_checkpoint_for(chapter: Chapter) -> dict[str, object]:
    checkpoint = _offline_checkpoints_by_folder().get(chapter.slug)
    if checkpoint is None:
        raise ValueError(f"no offline checkpoint for teaching chapter {chapter.slug}")
    return checkpoint


def render_offline_checkpoint(chapter: Chapter) -> str:
    checkpoint = _offline_checkpoint_for(chapter)
    concept = checkpoint["concept"]
    command = checkpoint["command"]
    evidence_lines = textwrap.wrap(
        f"**Evidence to find:** {checkpoint['evidence']}.",
        width=95,
        break_long_words=False,
        break_on_hyphens=False,
    )
    return (
        f"\n> **Hardware-free checkpoint:** prove `{concept}` without a microphone,\n"
        "> speakers, or provider credentials:\n"
        ">\n"
        "> ```bash\n"
        f"> {command}\n"
        "> ```\n"
        ">\n"
        + "".join(f"> {line}\n" for line in evidence_lines)
        + ">\n"
        + "> [See all 16 checkpoints](../#hardware-free-checkpoint-spine).\n"
    )


def _render_navigation_block(navigation: str) -> str:
    return f"<!-- BEGIN auto:navigation -->\n{navigation}\n<!-- END auto:navigation -->"


def _render_offline_checkpoint_block(chapter: Chapter) -> str:
    return (
        "<!-- BEGIN auto:offline-checkpoint -->"
        f"{render_offline_checkpoint(chapter)}"
        "<!-- END auto:offline-checkpoint -->"
    )


def _render_practice_handoff_block() -> str:
    return (
        "<!-- BEGIN auto:practice-handoff -->\n"
        f"{render_practice_handoff()}\n"
        "<!-- END auto:practice-handoff -->"
    )


def _render_exercise_completion_block(chapter: Chapter) -> str:
    return (
        "<!-- BEGIN auto:exercise-completion -->\n"
        f"{render_exercise_completion(chapter)}\n"
        "<!-- END auto:exercise-completion -->"
    )


def _ensure_navigation(chapter: Chapter, text: str, navigation: str) -> str:
    block = _render_navigation_block(navigation)
    if NAVIGATION_RE.search(text):
        updated = NAVIGATION_RE.sub(lambda _match: block, text)
        match = NAVIGATION_RE.search(updated)
        assert match is not None
        remainder = updated[match.end() :].lstrip("\n")
        return f"{updated[: match.end()]}\n\n{remainder}"

    heading = re.match(r"^# [^\n]+\n", text)
    if heading is None:
        readme = chapter.path / "README.md"
        raise ValueError(f"{readme.relative_to(ROOT)} must start with an H1")
    remainder = text[heading.end() :].lstrip("\n")
    return f"{text[: heading.end()]}\n{block}\n\n{remainder}"


def _ensure_offline_checkpoint(chapter: Chapter, text: str) -> str:
    block = _render_offline_checkpoint_block(chapter)
    if OFFLINE_CHECKPOINT_RE.search(text):
        return OFFLINE_CHECKPOINT_RE.sub(lambda _match: block, text)

    navigation = NAVIGATION_RE.search(text)
    if navigation is None:
        raise ValueError(f"{chapter.slug}/README.md is missing generated navigation")
    summary_start = navigation.end()
    while summary_start < len(text) and text[summary_start] == "\n":
        summary_start += 1
    summary = re.match(r"(?:>[^\n]*(?:\n|$))+", text[summary_start:])
    if summary is None:
        raise ValueError(f"{chapter.slug}/README.md must open with a blockquote summary")
    summary_end = summary_start + summary.end()
    prefix = text[:summary_end].rstrip("\n")
    remainder = text[summary_end:].lstrip("\n")
    return f"{prefix}\n\n{block}\n\n{remainder}"


def _ensure_practice_handoff(chapter: Chapter, text: str) -> str:
    block = _render_practice_handoff_block()
    if PRACTICE_HANDOFF_RE.search(text):
        return PRACTICE_HANDOFF_RE.sub(lambda _match: block, text)

    target = re.search(
        r"^## (?:What's next|The ladder, complete \(really\))$",
        text,
        re.MULTILINE,
    )
    if target is None:
        raise ValueError(f"{chapter.slug}/README.md is missing its closing handoff section")
    try_breaking = text.find("## Try breaking it")
    if try_breaking < 0 or try_breaking > target.start():
        raise ValueError(f"{chapter.slug}/README.md must practice before its closing handoff")
    prefix = text[: target.start()].rstrip("\n")
    remainder = text[target.start() :].lstrip("\n")
    return f"{prefix}\n\n{block}\n\n{remainder}"


def _ensure_exercise_completion(chapter: Chapter, text: str) -> str:
    block = _render_exercise_completion_block(chapter)
    if "## Self-check" not in text:
        raise ValueError(f"{chapter.slug}/EXERCISES.md is missing its self-check")
    if EXERCISE_COMPLETION_RE.search(text):
        updated = EXERCISE_COMPLETION_RE.sub(lambda _match: block, text)
        return updated.rstrip() + "\n"
    return text.rstrip() + f"\n\n{block}\n"


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

    updated = _ensure_navigation(chapter, original, render_navigation(chapter))
    updated = _ensure_offline_checkpoint(chapter, updated)
    updated = _ensure_practice_handoff(chapter, updated)
    updated = SNIPPET_RE.sub(_snippet_sub, updated)
    updated = DIFF_RE.sub(_diff_sub, updated)
    updated = LINERANGE_RE.sub(_linerange_sub, updated)
    updated = LINKHASH_RE.sub(_linkhash_sub, updated)
    return original, updated


def regen_exercises(chapter: Chapter) -> tuple[str, str]:
    exercises_path = chapter.path / "EXERCISES.md"
    original = exercises_path.read_text(encoding="utf-8")
    updated = _ensure_navigation(
        chapter,
        original,
        render_exercise_navigation(chapter),
    )
    updated = _ensure_exercise_completion(chapter, updated)
    return original, updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chapter", help="Restrict to one chapter slug, e.g. 05-blocking-agent")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if any README or exercise page would change. Writes nothing.",
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
        for filename, regenerator in (
            ("README.md", regen_readme),
            ("EXERCISES.md", regen_exercises),
        ):
            path = chapter.path / filename
            if not path.exists():
                continue
            original, updated = regenerator(chapter)
            if original == updated:
                continue
            if args.check:
                drift = True
                print(f"would update {path.relative_to(ROOT)}", file=sys.stderr)
                sys.stderr.write(
                    "".join(
                        difflib.unified_diff(
                            original.splitlines(keepends=True),
                            updated.splitlines(keepends=True),
                            fromfile=str(path),
                            tofile=f"{path} (regenerated)",
                        )
                    )
                )
            else:
                path.write_text(updated)
                print(f"updated {path.relative_to(ROOT)}")

    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
