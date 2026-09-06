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

* Previous/index/next navigation derived from the chapter folders::

      <!-- BEGIN auto:navigation -->
      **Progress: 2 of 16** · [← Chapter 0 — Hello, Audio](../00-hello-audio/) ·
      [Ladder index](../) · [Progress worksheet](../PROGRESS.md) ·
      [Exercises](./EXERCISES.md) ·
      [Chapter 2 — Transcribe →](../02-transcribe/)
      <!-- END auto:navigation -->

  The renderer inserts this block immediately after the H1 when it is missing
  and refreshes adjacent chapter titles/links when the ladder changes. Exercise
  pages get a companion block linking back to the narrative, index, and next
  chapter.

* The unified diff against the previous chapter's source::

      <!-- BEGIN auto:diff prev=04-vad-preroll src=main.py -->
      <details markdown="1">
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

* A chapter-local hardware-free checkpoint derived from
  ``docs/teaching/offline_spine.py``::

      <!-- BEGIN auto:offline-checkpoint -->
      > **Hardware-free checkpoint:** prove `first-audio outcomes` without a
      > microphone, speakers, or provider credentials:
      >
      > **Predict first:** Which delivery outcome will appear when no chunks,
      > rejected chunks, or accepted audio are produced?
      >
      > ```bash
      > uv run python docs/teaching/05-blocking-agent/tts_outcome_probe.py
      > ```
      >
      > **Evidence to find:** no chunks, all rejected, and first accepted audio
      > produce three distinct outcomes.
      >
      > **Explain the result:** connect each outcome to its accepted/rejected
      > counts and first-audio milestone.
      >
      > [See all 16 checkpoints](../#hardware-free-checkpoint-spine).
      <!-- END auto:offline-checkpoint -->

  The renderer inserts this block after the chapter's opening summary and
  refreshes the concept and command from the spine's single manifest.

* A two-chapter-lag retrieval prompt before chapters 2-15::

      <!-- BEGIN auto:spaced-retrieval -->
      ## Recall before reading

      > **Following the ladder? Spaced retrieval — Chapter 3**
      >
      > Close earlier chapters and answer the Chapter 3 checkpoint prediction
      > from memory before reading further. Then connect its concept to the
      > current chapter and rerun its probe only after recording an answer.
      <!-- END auto:spaced-retrieval -->

  The prompt reuses the earlier checkpoint manifest without revealing its
  expected evidence. Learners entering at a later chapter may skip it; learners
  following the ladder get delayed, interleaved recall before new material.

* A narrative-to-practice handoff before the chapter's next-step section::

      <!-- BEGIN auto:practice-handoff -->
      ## Practice and self-check

      Work through the chapter exercises, then try their closing self-check
      from memory.
      <!-- END auto:practice-handoff -->

* A completion-evidence protocol before the first exercise task::

      <!-- BEGIN auto:exercise-protocol -->
      > **Completion evidence for every task**
      >
      > 1. **Before hints:** keep your initial prediction or plan.
      > 2. **After the attempt:** keep the exact command or change and one
      >    observed field, measurement, or behavior.
      > 3. **Before moving on:** explain why the evidence supports or changes
      >    your model.
      >
      > A task is complete when all three are present.
      <!-- END auto:exercise-protocol -->

  The renderer places this after any exercise-page introduction and before the
  first task, so every applied exercise shares one concrete completion contract.

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
      - Update the progress worksheet
      - Continue to Chapter 6 →
      <!-- END auto:exercise-completion -->

  Together these blocks make the narrative → practice → recall → next-chapter
  loop explicit while deriving all chapter links from ladder order.

* Hand-authored exercise hints revealed progressively between attempts::

      <!-- BEGIN auto:exercise-hints -->
      **Hints**

      After your first attempt, open Hint 1 only. Try again before opening
      the next hint.

      <details markdown="1">
      <summary>Hint 1 of 2</summary>

      The hand-authored hint content stays here.

      </details>

      <details markdown="1">
      <summary>Hint 2 of 2</summary>

      Each numbered hint gets its own disclosure.

      </details>
      <!-- END auto:exercise-hints -->

  The renderer preserves each numbered hint while adding or refreshing one
  disclosure per clue. Learners make a fresh attempt between hints instead of
  exposing the whole sequence after the first try.

* A closed-book retrieval gate under every self-check heading::

      ## Self-check

      <!-- BEGIN auto:self-check-protocol -->
      > **Closed-book retrieval gate**
      >
      > 1. Close the chapter narrative and every hint disclosure.
      > 2. Answer every numbered question below from memory, aloud or in writing.
      > 3. Support each answer with at least one observed field, measurement, or behavior
      >    from your attempt record.
      > 4. Mark each answer **pass** or **retry** in your progress record.
      >
      > If an answer needs notes, reopen only the section that owns the weak concept,
      > correct your explanation, close it, and retry. Continue only when every answer
      > passes without looking.
      <!-- END auto:self-check-protocol -->

  The hand-authored chapter questions stay below this generated protocol; the
  gate makes recall and targeted retry consistent across the ladder.

The blocks render fine on GitHub (the markers are HTML comments) and
also display correctly inside MkDocs. Run after editing any chapter
``main.py``; ``--check`` exits non-zero if any block would change,
which is what CI should call.

The script also regenerates ``docs/teaching/PROGRESS.md`` from chapter titles
and the offline checkpoint manifest. That file is a blank worksheet template;
learners copy it before checking boxes so regeneration cannot erase their state.
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
PROGRESS_WORKSHEET = TEACHING / "PROGRESS.md"

CHAPTER_RE = re.compile(r"^\d{2}-")


@dataclass(frozen=True)
class PhaseReview:
    title: str
    prompt: str
    criteria: tuple[tuple[str, str], ...]


PHASE_REVIEW_CRITERION_LABELS = ("Coverage", "Causality", "Evidence", "Limits")


PHASE_REVIEWS = {
    9: PhaseReview(
        title="Build phase review",
        prompt=(
            "Without notes, draw one turn from raw input format through STT partial/final, "
            "endpointing, agent/TTS, transport acceptance, and barge-in cancellation"
        ),
        criteria=(
            (
                "Coverage",
                "the drawing includes every stage from input format through interruption",
            ),
            (
                "Causality",
                (
                    "it marks which observations may revise, which actions commit, and why "
                    "cancellation ordering preserves the next utterance"
                ),
            ),
            (
                "Evidence",
                (
                    "it cites attempt evidence for format, partial/final policy, first audio, and "
                    "interruption ordering"
                ),
            ),
            (
                "Limits",
                "it states one caller-heard claim that transport acceptance still cannot prove",
            ),
        ),
    ),
    12: PhaseReview(
        title="Operate phase review",
        prompt=("Given an unfamiliar bad call, write the diagnostic order before proposing a fix"),
        criteria=(
            (
                "Coverage",
                "the order covers NR/AEC, journal queries, and latency/eval coverage",
            ),
            (
                "Causality",
                (
                    "it explains why each check precedes the proposed fix and names the strongest "
                    "supported cause"
                ),
            ),
            (
                "Evidence",
                "it cites one bundle or probe result that identifies the bottleneck",
            ),
            (
                "Limits",
                "it names missing evidence and the metric that would catch a regression",
            ),
        ),
    ),
    14: PhaseReview(
        title="Generalise phase review",
        prompt=(
            "Design one provider × transport × agent comparison that changes one axis at a time "
            "while preserving the session and bridge contracts"
        ),
        criteria=(
            (
                "Coverage",
                "the design names the config or bridge boundary for every axis",
            ),
            (
                "Causality",
                (
                    "it explains what changes on the selected axis and how the other axes stay "
                    "controlled"
                ),
            ),
            (
                "Evidence",
                (
                    "it names one invariant event/state shape and the measurement "
                    "that decides the tradeoff"
                ),
            ),
            (
                "Limits",
                "it states one comparison claim that the selected measurement cannot support",
            ),
        ),
    ),
}
SHIP_PHASE_REVIEW = PhaseReview(
    title="Ship phase review and finish the ladder",
    prompt=(
        "Run `uv run python docs/teaching/offline_spine.py --run --jobs 4 --show-evidence`. "
        "Then, without notes, explain the path from raw PCM through a multi-session production "
        "service, including ownership, start rollback, shutdown, and postmortem evidence"
    ),
    criteria=(
        (
            "Coverage",
            "the explanation connects raw audio, turn processing, and multi-session operation",
        ),
        (
            "Causality",
            "it explains how ownership, start rollback, and shutdown protect peer sessions",
        ),
        (
            "Evidence",
            (
                "all 16 offline checkpoints pass and it cites the result that "
                "changed the learner's model most"
            ),
        ),
        (
            "Limits",
            (
                "it states one production claim the postmortem evidence cannot prove and the next "
                "measurement needed"
            ),
        ),
    ),
)
SHIP_PHASE_REVIEW_TITLE = SHIP_PHASE_REVIEW.title

SNIPPET_RE = re.compile(
    r"(?P<begin><!-- BEGIN auto:snippet (?P<attrs>[^>]*?) -->)"
    r"(?P<body>.*?)"
    r"(?P<end><!-- END auto:snippet -->)",
    re.DOTALL,
)
NAVIGATION_RE = re.compile(
    r"(?P<begin><!-- BEGIN auto:navigation -->)"
    r"(?P<body>.*?)"
    r"(?P<end><!-- END auto:navigation -->)",
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
OFFLINE_CHECKPOINT_RE = re.compile(
    r"(?P<begin><!-- BEGIN auto:offline-checkpoint -->)"
    r"(?P<body>.*?)"
    r"(?P<end><!-- END auto:offline-checkpoint -->)",
    re.DOTALL,
)
SPACED_RETRIEVAL_RE = re.compile(
    r"(?P<begin><!-- BEGIN auto:spaced-retrieval -->)"
    r"(?P<body>.*?)"
    r"(?P<end><!-- END auto:spaced-retrieval -->)",
    re.DOTALL,
)
PRACTICE_HANDOFF_RE = re.compile(
    r"(?P<begin><!-- BEGIN auto:practice-handoff -->)"
    r"(?P<body>.*?)"
    r"(?P<end><!-- END auto:practice-handoff -->)",
    re.DOTALL,
)
EXERCISE_PROTOCOL_RE = re.compile(
    r"(?P<begin><!-- BEGIN auto:exercise-protocol -->)"
    r"(?P<body>.*?)"
    r"(?P<end><!-- END auto:exercise-protocol -->)",
    re.DOTALL,
)
EXERCISE_COMPLETION_RE = re.compile(
    r"(?P<begin><!-- BEGIN auto:exercise-completion -->)"
    r"(?P<body>.*?)"
    r"(?P<end><!-- END auto:exercise-completion -->)",
    re.DOTALL,
)
EXERCISE_HINTS_RE = re.compile(
    r"(?P<begin><!-- BEGIN auto:exercise-hints -->)"
    r"(?P<body>.*?)"
    r"(?P<end><!-- END auto:exercise-hints -->)",
    re.DOTALL,
)
LEGACY_EXERCISE_HINTS_RE = re.compile(
    r"<details(?: markdown=\"1\")?>\n"
    r"<summary>[^\n]*</summary>\n\n"
    r"\*\*Hints\*\*\n\n"
    r"(?P<body>.*?)\n\n?"
    r"</details>",
    re.DOTALL,
)
HINT_DISCLOSURE_RE = re.compile(
    r"<details markdown=\"1\">\n"
    r"<summary>Hint (?P<number>\d+) of (?P<total>\d+)</summary>\n\n"
    r"(?P<body>.*?)\n"
    r"</details>",
    re.DOTALL,
)
NUMBERED_HINT_RE = re.compile(r"^(?P<number>\d+)\. ", re.MULTILINE)
MARKDOWN_FENCE_RE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})(?P<rest>[^\n]*)$")
BARE_EXERCISE_HINTS_RE = re.compile(
    r"^\*\*Hints\*\*\n\n(?P<body>.*?)(?=^## )",
    re.DOTALL | re.MULTILINE,
)
SELF_CHECK_PROTOCOL_RE = re.compile(
    r"(?P<begin><!-- BEGIN auto:self-check-protocol -->)"
    r"(?P<body>.*?)"
    r"(?P<end><!-- END auto:self-check-protocol -->)",
    re.DOTALL,
)
SELF_CHECK_QUESTION_RE = re.compile(r"^(?P<number>\d+)\. ", re.MULTILINE)
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
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):  # noqa: SIM102 nested branches preserve decision context
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


def _chapter_title(chapter: Chapter) -> str:
    readme = chapter.path / "README.md"
    heading = readme.read_text(encoding="utf-8").splitlines()[0]
    if not heading.startswith("# "):
        raise ValueError(f"{readme.relative_to(ROOT)} must start with an H1")
    return heading.removeprefix("# ")


def _chapter_position(chapter: Chapter) -> tuple[list[Chapter], int]:
    chapters = discover_chapters()
    try:
        index = next(i for i, candidate in enumerate(chapters) if candidate.slug == chapter.slug)
    except StopIteration as exc:
        raise ValueError(f"unknown teaching chapter: {chapter.slug}") from exc
    return chapters, index


def render_navigation(chapter: Chapter) -> str:
    chapters, index = _chapter_position(chapter)

    links: list[str] = []
    if index:
        previous = chapters[index - 1]
        links.append(f"[← {_chapter_title(previous)}](../{previous.slug}/)")
    links.append("[Ladder index](../)")
    links.append("[Progress worksheet](../PROGRESS.md)")
    links.append("[Exercises](./EXERCISES.md)")
    if index + 1 < len(chapters):
        following = chapters[index + 1]
        links.append(f"[{_chapter_title(following)} →](../{following.slug}/)")

    progress = f"**Progress: {index + 1} of {len(chapters)}**"
    return f"{progress} · {' · '.join(links)}"


def render_exercise_navigation(chapter: Chapter) -> str:
    chapters, index = _chapter_position(chapter)

    links = [
        "[← Back to chapter](./README.md)",
        "[Ladder index](../)",
        "[Progress worksheet](../PROGRESS.md)",
    ]
    if index + 1 < len(chapters):
        following = chapters[index + 1]
        links.append(f"[{_chapter_title(following)} →](../{following.slug}/)")
    return " · ".join(links)


def _render_navigation_block(navigation: str) -> str:
    return f"<!-- BEGIN auto:navigation -->\n{navigation}\n<!-- END auto:navigation -->"


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


def _spaced_retrieval_for(chapter: Chapter) -> tuple[Chapter, dict[str, object]] | None:
    chapters, index = _chapter_position(chapter)
    if index < 2:
        return None
    earlier = chapters[index - 2]
    return earlier, _offline_checkpoint_for(earlier)


def render_exercise_protocol() -> str:
    return (
        "> **Completion evidence for every task**\n"
        ">\n"
        "> 1. **Before hints:** keep your initial prediction or plan.\n"
        "> 2. **After the attempt:** keep the exact command or change and one observed field,\n"
        ">    measurement, or behavior.\n"
        "> 3. **Before moving on:** explain in one sentence why the evidence supports or changes\n"
        ">    your model.\n"
        ">\n"
        "> A task is complete when all three are present. Keep a wrong first answer visible;\n"
        "> it is evidence to explain after revealing hints, not an answer to rewrite."
    )


def render_practice_handoff() -> str:
    return (
        "## Practice and self-check\n\n"
        "Work through [the chapter exercises](./EXERCISES.md), then try their closing\n"
        "self-check from memory. If an answer is weak, rerun the hardware-free\n"
        "checkpoint or revisit the section that owns the gap."
    )


def render_spaced_retrieval(chapter: Chapter) -> str | None:
    retrieval = _spaced_retrieval_for(chapter)
    if retrieval is None:
        return None
    earlier, earlier_checkpoint = retrieval
    current_checkpoint = _offline_checkpoint_for(chapter)
    prediction_lines = textwrap.wrap(
        str(earlier_checkpoint["prediction"]),
        width=93,
        break_long_words=False,
        break_on_hyphens=False,
    )
    connection_lines = textwrap.wrap(
        f"After recording your answer, explain one way `{earlier_checkpoint['concept']}` "
        f"changes how you reason about `{current_checkpoint['concept']}`. Keep the first "
        "answer visible.",
        width=93,
        break_long_words=False,
        break_on_hyphens=False,
    )
    return (
        "## Recall before reading\n\n"
        f"> **Following the ladder? Spaced retrieval — {_chapter_title(earlier)}**\n"
        ">\n"
        "> Close earlier chapters and answer from memory before reading further. If this\n"
        "> chapter is your starting point, skip this block.\n"
        ">\n"
        "> **Answer from memory:**\n"
        ">\n"
        + "".join(f"> {line}\n" for line in prediction_lines)
        + ">\n"
        + "".join(f"> {line}\n" for line in connection_lines)
        + ">\n"
        + "> **Check only after answering:**\n"
        ">\n"
        "> ```bash\n"
        f"> {earlier_checkpoint['command']}\n"
        "> ```\n"
        ">\n"
        "> Cite one observed field, measurement, or behavior; repair only the part your\n"
        "> evidence disproved.\n"
    )


def render_exercise_completion(chapter: Chapter) -> str:
    chapters, index = _chapter_position(chapter)
    checkpoint = _offline_checkpoint_for(chapter)
    links = [
        "[Review the chapter narrative](./README.md)",
        "[Update the progress worksheet](../PROGRESS.md)",
    ]
    phase_review_title = None
    if phase_review := PHASE_REVIEWS.get(checkpoint["chapter"]):
        phase_review_title = phase_review.title
    elif index + 1 == len(chapters):
        phase_review_title = SHIP_PHASE_REVIEW_TITLE
    if phase_review_title:
        anchor = re.sub(r"[^a-z0-9]+", "-", phase_review_title.lower()).strip("-")
        links.append(f"[Complete the {phase_review_title}](../PROGRESS.md#{anchor})")
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


def render_exercise_hints(body: str) -> str:
    hints = _split_numbered_hints(body)
    total = len(hints)
    sections = [
        "<!-- BEGIN auto:exercise-hints -->",
        "**Hints**",
        "",
        "After your first attempt, open Hint 1 only. Close it and try again before opening",
        "the next hint; keep each attempt in your evidence record.",
    ]
    for number, hint in enumerate(hints, start=1):
        hint_content = NUMBERED_HINT_RE.sub("", hint, count=1)
        sections.extend(
            [
                "",
                '<details markdown="1">',
                f"<summary>Hint {number} of {total}</summary>",
                "",
                hint_content,
                "",
                "</details>",
            ]
        )
    sections.append("<!-- END auto:exercise-hints -->")
    return "\n".join(sections)


def _split_numbered_hints(body: str) -> list[str]:
    body = body.strip()
    starts: list[tuple[int, int]] = []
    active_fence: tuple[str, int] | None = None
    offset = 0
    for line in body.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        if fence := MARKDOWN_FENCE_RE.match(content):
            marker = fence.group("marker")
            fence_key = (marker[0], len(marker))
            if active_fence is None:
                active_fence = fence_key
            elif (
                fence_key[0] == active_fence[0]
                and fence_key[1] >= active_fence[1]
                and not fence.group("rest").strip()
            ):
                active_fence = None
        elif active_fence is None and (numbered := NUMBERED_HINT_RE.match(content)):
            starts.append((offset, int(numbered.group("number"))))
        offset += len(line)

    if not starts or starts[0][0] != 0:
        raise ValueError("exercise hints must start with a numbered `1. ` item")
    numbers = [number for _, number in starts]
    expected = list(range(1, len(starts) + 1))
    if numbers != expected:
        raise ValueError(f"exercise hints must be sequential; found {numbers}")
    return [
        body[start : starts[index + 1][0]].strip()
        if index + 1 < len(starts)
        else body[start:].strip()
        for index, (start, _) in enumerate(starts)
    ]


def _exercise_hint_source(rendered_body: str) -> str:
    rendered_body = rendered_body.strip()
    if legacy := LEGACY_EXERCISE_HINTS_RE.fullmatch(rendered_body):
        return legacy.group("body").strip()
    disclosures = list(HINT_DISCLOSURE_RE.finditer(rendered_body))
    if disclosures:
        hints = []
        for disclosure in disclosures:
            content = disclosure.group("body").strip()
            if NUMBERED_HINT_RE.match(content) is None:
                content = f"{disclosure.group('number')}. {content}"
            hints.append(content)
        return "\n\n".join(hints)
    return rendered_body


def render_self_check_protocol() -> str:
    return (
        "> **Closed-book retrieval gate**\n"
        ">\n"
        "> 1. Close the chapter narrative and every hint disclosure.\n"
        "> 2. Answer every numbered question below from memory, aloud or in writing.\n"
        "> 3. Support each answer with at least one observed field, measurement, or behavior\n"
        ">    from your attempt record.\n"
        "> 4. Mark each answer **pass** or **retry** in your progress record.\n"
        ">\n"
        "> If an answer needs notes, reopen only the section that owns the weak concept,\n"
        "> correct your explanation, close it, and retry. Continue only when every answer\n"
        "> passes without looking."
    )


def _progress_item(label: str, content: str) -> str:
    return textwrap.fill(
        f"- [ ] **{label}:** {content}",
        width=99,
        subsequent_indent="  ",
        break_long_words=False,
        break_on_hyphens=False,
    )


def _progress_command_item(
    label: str,
    command: object,
    *,
    prefix: str = "",
    suffix: str = ".",
) -> str:
    head = f"- [ ] **{label}:** {prefix}`{command}`"
    if len(head) + len(suffix) <= 99:
        return f"{head}{suffix}"
    # The command stays on one line: it has to remain copy-pasteable, and the
    # worksheet guard matches it verbatim. Only the trailing prose wraps, as a
    # lazy list continuation.
    return (
        head
        + "\n"
        + textwrap.fill(
            suffix.strip(),
            width=99,
            initial_indent="  ",
            subsequent_indent="  ",
            break_long_words=False,
            break_on_hyphens=False,
        )
    )


def _render_phase_review(review: PhaseReview) -> list[str]:
    labels = tuple(label for label, _ in review.criteria)
    if labels != PHASE_REVIEW_CRITERION_LABELS:
        raise ValueError(
            f"{review.title} criteria must be {PHASE_REVIEW_CRITERION_LABELS}; found {labels}"
        )
    return [
        f"## {review.title}",
        "",
        textwrap.fill(f"{review.prompt}.", width=99),
        "",
        "Score the result against all four criteria below. Mark each criterion **pass** or",
        "**retry** in your progress record; advance only at 4/4 and redo only missed criteria.",
        "",
        *[_progress_item(label, f"{criterion}.") for label, criterion in review.criteria],
    ]


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


def self_check_questions(chapter: Chapter, *, exercises: str | None = None) -> list[str]:
    if exercises is None:
        exercises_path = chapter.path / "EXERCISES.md"
        exercises = exercises_path.read_text(encoding="utf-8")
    heading = re.search(r"^## Self-check$", exercises, re.MULTILINE)
    if heading is None:
        raise ValueError(f"{chapter.slug}/EXERCISES.md is missing its self-check")
    protocol = SELF_CHECK_PROTOCOL_RE.search(exercises, heading.end())
    if protocol is None:
        raise ValueError(f"{chapter.slug}/EXERCISES.md is missing its retrieval gate")
    completion = exercises.find("<!-- BEGIN auto:exercise-completion -->", protocol.end())
    if completion < 0:
        raise ValueError(f"{chapter.slug}/EXERCISES.md is missing its completion footer")
    next_heading = exercises.find("\n## ", protocol.end(), completion)
    question_end = next_heading if next_heading >= 0 else completion
    body = exercises[protocol.end() : question_end].strip()
    starts = list(SELF_CHECK_QUESTION_RE.finditer(body))
    if not 3 <= len(starts) <= 6:
        raise ValueError(
            f"{chapter.slug}/EXERCISES.md needs between three and six self-check questions"
        )
    numbers = [int(match.group("number")) for match in starts]
    if numbers != list(range(1, len(starts) + 1)):
        raise ValueError(f"{chapter.slug}/EXERCISES.md self-check numbering is not sequential")
    questions = [
        body[start.start() : starts[index + 1].start()].strip()
        if index + 1 < len(starts)
        else body[start.start() :].strip()
        for index, start in enumerate(starts)
    ]
    if any("?" not in question for question in questions):
        raise ValueError(f"{chapter.slug}/EXERCISES.md has a non-question self-check prompt")
    if "You should" in body:
        raise ValueError(f"{chapter.slug}/EXERCISES.md uses a declarative self-check outcome")
    return questions


def render_progress_worksheet() -> str:
    sections = [
        "<!-- Generated by scripts/regen_teaching_chapters.py; do not edit this template. -->",
        "# Teaching ladder progress worksheet",
        "",
        "[← Teaching ladder](./)",
        "",
        "This is a generated **blank template**. Copy it to a personal notes location before",
        "checking boxes; regeneration restores this file to its blank state. Keep commands,",
        "record names, measurements, and explanations in your copy—not credentials, raw audio,",
        "or sensitive transcript text.",
        "",
        "A chapter is complete only when every box on its card is checked and its self-check",
        "score reaches N/N. Retry only missed answers; preserve wrong predictions because they",
        "are evidence to explain, not history to rewrite. Chapters 2-15 begin with a",
        "two-chapter-lag recall. At each phase boundary, pass all four integration criteria at",
        "4/4 before starting the next chapter card.",
        "",
        "Every card's **Prepare** and **Run** pair is the chapter's *hardware-free checkpoint*",
        "probe — it needs no microphone and no provider keys. That is deliberately a smaller",
        "install than the chapter's own `main.py`, whose prerequisites (extra provider markers,",
        "API keys, transport extras) live in each chapter README and are linked from every",
        "Prepare line. Follow the README when you run the chapter itself.",
    ]
    for chapter in discover_chapters():
        checkpoint = _offline_checkpoint_for(chapter)
        chapter_number = checkpoint["chapter"]
        # Derive progress from the exercise page write mode would produce. In
        # check mode the on-disk page may still be missing newly generated
        # gates, because drift reporting deliberately performs no writes.
        _, regenerated_exercises = regen_exercises(chapter)
        question_count = len(self_check_questions(chapter, exercises=regenerated_exercises))
        sections.extend(
            [
                "",
                f"## {_chapter_title(chapter)}",
                "",
                f"[Narrative](./{chapter.slug}/) · [Exercises](./{chapter.slug}/EXERCISES.md)",
                "",
            ]
        )
        if spaced_retrieval := _spaced_retrieval_for(chapter):
            earlier, _ = spaced_retrieval
            sections.append(
                _progress_item(
                    "Recall earlier",
                    f"before the narrative, retrieve {_chapter_title(earlier)} in "
                    f"[Recall before reading](./{chapter.slug}/#recall-before-reading); keep "
                    "the first answer and its evidence-backed repair.",
                )
            )
        sections.extend(
            [
                _progress_item("Predict", str(checkpoint["prediction"])),
                _progress_command_item(
                    "Prepare",
                    checkpoint["setup_command"],
                    prefix="from the repository root, run ",
                    suffix=(
                        " — enough for this card's hardware-free checkpoint. The chapter's "
                        f"own `main.py` may need more; see [its prerequisites]"
                        f"(./{chapter.slug}/#prerequisites)."
                    ),
                ),
                _progress_command_item("Run", checkpoint["command"]),
                _progress_item(
                    "Find",
                    f"{checkpoint['evidence']}.",
                ),
                _progress_item(
                    "Reflect",
                    f"{checkpoint['reflection']}.",
                ),
                _progress_item(
                    "Practice",
                    f"complete [the chapter exercises](./{chapter.slug}/EXERCISES.md) and keep "
                    "an attempt record for every task.",
                ),
                _progress_item(
                    "Retrieve",
                    f"pass all {question_count} numbered questions in "
                    f"[the closed-book self-check](./{chapter.slug}/EXERCISES.md#self-check); "
                    f"record {question_count}/{question_count} with one attempt-evidence "
                    "citation per answer.",
                ),
                _progress_command_item(
                    "Replay",
                    "uv run python docs/teaching/offline_spine.py --run "
                    f"--through {chapter_number} --jobs 4 --show-evidence",
                    prefix="run ",
                    suffix=" and explain any mismatch.",
                ),
            ]
        )
        if phase_review := PHASE_REVIEWS.get(chapter_number):
            sections.extend(["", *_render_phase_review(phase_review)])
    sections.extend(["", *_render_phase_review(SHIP_PHASE_REVIEW), ""])
    return "\n".join(sections)


def render_offline_checkpoint(chapter: Chapter) -> str:
    checkpoint = _offline_checkpoint_for(chapter)
    concept = checkpoint["concept"]
    command = checkpoint["command"]
    prediction_lines = textwrap.wrap(
        f"**Predict first:** {checkpoint['prediction']}",
        width=95,
        break_long_words=False,
        break_on_hyphens=False,
    )
    evidence_lines = textwrap.wrap(
        f"**Evidence to find:** {checkpoint['evidence']}.",
        width=95,
        break_long_words=False,
        break_on_hyphens=False,
    )
    reflection_lines = textwrap.wrap(
        f"**Explain the result:** {checkpoint['reflection']}.",
        width=95,
        break_long_words=False,
        break_on_hyphens=False,
    )
    return (
        f"\n> **Hardware-free checkpoint:** prove `{concept}` without a microphone,\n"
        "> speakers, or provider credentials:\n"
        ">\n" + "".join(f"> {line}\n" for line in prediction_lines) + ">\n" + "> ```bash\n"
        f"> {command}\n"
        "> ```\n"
        ">\n"
        + "".join(f"> {line}\n" for line in evidence_lines)
        + ">\n"
        + "".join(f"> {line}\n" for line in reflection_lines)
        + ">\n"
        + "> [See all 16 checkpoints](../#hardware-free-checkpoint-spine).\n"
    )


def _render_offline_checkpoint_block(chapter: Chapter) -> str:
    return (
        "<!-- BEGIN auto:offline-checkpoint -->"
        f"{render_offline_checkpoint(chapter)}"
        "<!-- END auto:offline-checkpoint -->"
    )


def _render_spaced_retrieval_block(chapter: Chapter) -> str | None:
    rendered = render_spaced_retrieval(chapter)
    if rendered is None:
        return None
    return f"<!-- BEGIN auto:spaced-retrieval -->\n{rendered}<!-- END auto:spaced-retrieval -->"


def _render_practice_handoff_block() -> str:
    return (
        "<!-- BEGIN auto:practice-handoff -->\n"
        f"{render_practice_handoff()}\n"
        "<!-- END auto:practice-handoff -->"
    )


def _render_exercise_protocol_block() -> str:
    return (
        "<!-- BEGIN auto:exercise-protocol -->\n"
        f"{render_exercise_protocol()}\n"
        "<!-- END auto:exercise-protocol -->"
    )


def _render_exercise_completion_block(chapter: Chapter) -> str:
    return (
        "<!-- BEGIN auto:exercise-completion -->\n"
        f"{render_exercise_completion(chapter)}\n"
        "<!-- END auto:exercise-completion -->"
    )


def _render_self_check_protocol_block() -> str:
    return (
        "<!-- BEGIN auto:self-check-protocol -->\n"
        f"{render_self_check_protocol()}\n"
        "<!-- END auto:self-check-protocol -->"
    )


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


def _ensure_spaced_retrieval(chapter: Chapter, text: str) -> str:
    block = _render_spaced_retrieval_block(chapter)
    if block is None:
        if SPACED_RETRIEVAL_RE.search(text):
            return SPACED_RETRIEVAL_RE.sub("", text).replace("\n\n\n", "\n\n")
        return text
    if SPACED_RETRIEVAL_RE.search(text):
        return SPACED_RETRIEVAL_RE.sub(lambda _match: block, text)

    checkpoint = OFFLINE_CHECKPOINT_RE.search(text)
    if checkpoint is None:
        raise ValueError(f"{chapter.slug}/README.md is missing its hardware-free checkpoint")
    prefix = text[: checkpoint.start()].rstrip("\n")
    remainder = text[checkpoint.start() :].lstrip("\n")
    return f"{prefix}\n\n{block}\n\n{remainder}"


def _ensure_practice_handoff(chapter: Chapter, text: str) -> str:
    block = _render_practice_handoff_block()
    text = PRACTICE_HANDOFF_RE.sub("", text)

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


def _ensure_exercise_protocol(chapter: Chapter, text: str) -> str:
    block = _render_exercise_protocol_block()
    if EXERCISE_PROTOCOL_RE.search(text):
        return EXERCISE_PROTOCOL_RE.sub(lambda _match: block, text)

    first_task = re.search(r"^## (?:\d+\.|Bonus\b)", text, re.MULTILINE)
    if first_task is None:
        raise ValueError(f"{chapter.slug}/EXERCISES.md is missing its first applied task")
    prefix = text[: first_task.start()].rstrip("\n")
    remainder = text[first_task.start() :].lstrip("\n")
    return f"{prefix}\n\n{block}\n\n{remainder}"


def _ensure_exercise_completion(chapter: Chapter, text: str) -> str:
    block = _render_exercise_completion_block(chapter)
    text = EXERCISE_COMPLETION_RE.sub("", text)
    if "## Self-check" not in text:
        raise ValueError(f"{chapter.slug}/EXERCISES.md is missing its self-check")
    return text.rstrip() + f"\n\n{block}\n"


def _ensure_exercise_hints(text: str) -> str:
    stashed: dict[str, str] = {}

    def _stash_existing(match: re.Match[str]) -> str:
        placeholder = f"EASYCAT_STASHED_EXERCISE_HINT_{len(stashed)}"
        source = _exercise_hint_source(match.group("body"))
        stashed[placeholder] = render_exercise_hints(source)
        return placeholder

    protected = EXERCISE_HINTS_RE.sub(_stash_existing, text)
    updated = BARE_EXERCISE_HINTS_RE.sub(
        lambda match: render_exercise_hints(match.group("body")) + "\n\n",
        protected,
    )
    for placeholder, block in stashed.items():
        updated = updated.replace(placeholder, block)
    return updated


def _ensure_self_check_protocol(chapter: Chapter, text: str) -> str:
    block = _render_self_check_protocol_block()
    if SELF_CHECK_PROTOCOL_RE.search(text):
        return SELF_CHECK_PROTOCOL_RE.sub(lambda _match: block, text)

    heading = re.search(r"^## Self-check$", text, re.MULTILINE)
    if heading is None:
        raise ValueError(f"{chapter.slug}/EXERCISES.md is missing its self-check")
    prefix = text[: heading.end()].rstrip("\n")
    remainder = text[heading.end() :].lstrip("\n")
    return f"{prefix}\n\n{block}\n\n{remainder}"


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
    updated = _ensure_spaced_retrieval(chapter, updated)
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
    updated = _ensure_exercise_protocol(chapter, updated)
    updated = _ensure_exercise_hints(updated)
    updated = _ensure_self_check_protocol(chapter, updated)
    updated = _ensure_exercise_completion(chapter, updated)
    return original, updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chapter", help="Restrict to one chapter slug, e.g. 05-blocking-agent")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if any generated teaching artifact would change. Writes nothing.",
    )
    args = parser.parse_args(argv)

    chapters = discover_chapters()
    if args.chapter:
        chapters = [c for c in chapters if c.slug == args.chapter]
        if not chapters:
            print(f"no chapter matches {args.chapter!r}", file=sys.stderr)
            return 2

    drift = False

    def _update(path: Path, original: str, updated: str) -> None:
        nonlocal drift
        if original == updated:
            return
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
            path.write_text(updated, encoding="utf-8")
            print(f"updated {path.relative_to(ROOT)}")

    for chapter in chapters:
        for filename, regenerator in (
            ("README.md", regen_readme),
            ("EXERCISES.md", regen_exercises),
        ):
            path = chapter.path / filename
            if not path.exists():
                continue
            original, updated = regenerator(chapter)
            _update(path, original, updated)

    if not args.chapter:
        original = (
            PROGRESS_WORKSHEET.read_text(encoding="utf-8") if PROGRESS_WORKSHEET.exists() else ""
        )
        _update(PROGRESS_WORKSHEET, original, render_progress_worksheet())

    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
