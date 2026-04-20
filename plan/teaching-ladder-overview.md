# Teaching Ladder: Voice Pipelines from Scratch

A 12-chapter progressive ladder for learning voice-AI pipelines through
EasyCat. Modeled after *Crafting Interpreters*, *Ray Tracer in One
Weekend*, and the Karpathy `micrograd`/`nanoGPT` tradition.

> **Status**: planning. No teaching code has been written yet. Each
> file in this ladder is a per-chapter plan; the chapters themselves
> will live in `examples/teaching/` once written.

## Pedagogical principles

These are the five patterns that recur across the best teaching
repositories we surveyed. Every chapter must honor all five.

1. **Small enough to hold in your head.** Each chapter introduces
   ≤ ~200 lines of new reader-facing code. If a chapter needs more,
   split it.
2. **Runnable at every checkpoint.** Every chapter ends with a
   program the reader can invoke and hear. No "it'll work once we
   add three more files."
3. **Wrong version first.** Chapters 3, 5, and 8 deliberately ship
   broken or naive implementations to motivate the fix. Do not
   collapse them with the next chapter.
4. **Observable internal state.** Starting at chapter 2, every
   chapter dumps a `RunBundle` the reader can scrub. The journal
   is the single source of truth for "what just happened."
5. **One axis of complexity per step.** No chapter may introduce
   two new concepts. If a chapter is about VAD, it is not also
   about noise reduction.

## The ladder

| # | Title | New concept | Wrong-version-first? |
|---|---|---|---|
| 0 | Hello, Audio | PCM, sample rates, chunks | — |
| 1 | Echo | Transport protocol, async streams | — |
| 2 | Transcribe | STT; batch vs streaming | contrast |
| 3 | Parrot, the naive way | Turn-taking by silence timeout | ✓ |
| 4 | VAD + pre-roll | Real speech detection | — |
| 5 | The blocking agent | LLM latency pain | ✓ |
| 6 | Streaming agent + sentence TTS | Pipeline overlap | — |
| 7 | Smart-turn | Endpoint classification | — |
| 8 | Interruption / barge-in | Cancel + heard-estimation | ✓ (three versions) |
| 9 | Noise reduction | Why NR lives before VAD | contrast |
| 10 | The journal as mental model | Observability mastery | — |
| 11 | Swap providers | Protocol design payoff | — |

## Repo conventions

- The entire ladder lives on `main` under `docs/teaching/`. No
  git tags, no long-lived branches. Readers clone the repo once
  and every chapter is already there.
- Each chapter is a **self-contained folder**:
  `docs/teaching/NN-name/` containing at minimum a `README.md`
  (the narrative) and a `main.py` (the runnable example).
  Chapters that ship multiple scripts (2, 8, 11) follow the same
  convention — every file they need lives inside their folder.
- Chapter N+1 **copies** chapter N's code as its starting point
  rather than modifying it in place. This is the whole point of
  dropping git tags: each chapter folder is a frozen, runnable
  artifact that the reader can visit independently. A little
  copy-paste between folders is the intended cost.
- Per-chapter README contains exactly **one** architecture diagram,
  **one** "what you'll hear" description, and **one** "try breaking
  it" exercise. Longer than one page = too long.
- No chapter may introduce a concept already covered by a prior
  chapter. Strict ladder discipline, Nand2Tetris-style.
- Chapters 2+ always emit a `RunBundle` to
  `docs/teaching/NN-name/runs/` (gitignored except for the planted
  bundles chapter 10 ships intentionally).
- A top-level `docs/teaching/README.md` is the landing page: the
  table from this plan, plus a "start here" pointer to chapter 0.

## Audience

Intermediate Python programmers comfortable with `async`/`await` and
dataclasses. **Not** assumed to know: audio formats, speech ML,
any specific vendor SDK, or voice-UX conventions.

## Library support for the ladder

The chapters lean on three additions made to the library
specifically to keep teaching code small:

- **`easycat.quick`** — `transcribe_file(path)` and
  `speak(transport, text)` helpers so chapter 2 and chapter 3 can
  stay under ~20 lines each. They are intentionally not
  comprehensive; chapters reach for `create_stt_provider` /
  `create_tts_provider` directly when they need control.
- **Top-level re-exports** — `Agent`, `TurnManager`,
  `TurnManagerConfig`, `TurnManagerState`, `create_stt_provider`,
  `create_tts_provider`, `parse_stt_string`, `parse_tts_string`,
  `export_debug_bundle` are all reachable as `easycat.*` so no
  chapter needs a submodule import for a core concept.
- **`JournalView.filter_by_stage` / `filter_by_turn` /
  `lookup_by_sequence`** — mirror the existing `RunBundle`
  methods so chapter 10 can teach one query surface.

Two session helpers were promoted from private to public modules
to support chapter 6 and chapter 8 walk-throughs:

- `easycat.session.interruption` (was `_interruption`)
- `easycat.session.text_utils` (was `_text_utils`) — exports
  `split_at_sentence_boundaries` and
  `has_unclosed_markdown_delimiters` without leading underscores
- `easycat.session.tts_helpers` (was `_tts_helpers`)

Chapters 4 and 6 deliberately **do not** use `TurnManager` or
`consume_agent_stream` directly. Each chapter builds its own small
version (~40-60 lines) to teach the concept, then reads the
production code as reference material. This is the Nand2Tetris /
Crafting-Interpreters pattern: understand by building, then study
the battle-tested version.

## Budget

- Narrative prose: ~40-60 pages total (3-5 per chapter).
- New example code: ~2000 lines total (~150-200 per chapter).
- Reader time per chapter: 10-30 minutes.

## Per-chapter plan structure

Each `teaching-NN-title.md` follows this template:

1. **Title + one-line hook**
2. **Prerequisites** (prior chapters, setup)
3. **Learning objectives** (what the reader walks away knowing)
4. **What you build** (concrete deliverables)
5. **Narrative arc** (the walk-through)
6. **(If applicable) The naive version** — the wrong-version-first
   payload. Chapters 3, 5, 8 center on this.
7. **Key concepts** (with pointers to existing EasyCat source files)
8. **Exercises** (1-3 "try breaking it" prompts)
9. **Journal highlights** (what records should appear in the bundle)
10. **Files created/modified**
11. **Success criteria**
12. **Links forward** (what the next chapter builds on)

## Why this exists

EasyCat is a production-oriented framework; the source is written
in a terse style with assumed context. That's correct for production
but wrong for teaching. This ladder is an orthogonal teaching
artifact: the framework's code does not change, but a parallel
`docs/teaching/` tree of narrative prose and runnable examples lets
a beginner build up to the framework rather than be dropped into it.

The chapter plans in this folder are the blueprint. Writing the
chapters themselves is a separate workstream.
