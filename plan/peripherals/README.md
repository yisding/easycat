# Peripheral Backlog

Status: active backlog.

Snapshot: every item below was verified against the tree on 2026-08-03.

These are separable follow-ups. None of them is on the critical path of the
bug-resistant refactor program in
[../roadmap/2026-08-02-bug-resistant-refactor-plan.md](../roadmap/2026-08-02-bug-resistant-refactor-plan.md),
and none of them blocks a shipped user workflow. Work outside this file — the
security, contract, evals, DX, structural, and validation queues — lives in
[../roadmap/open-backlog.md](../roadmap/open-backlog.md).

This file replaces the six peripheral design documents it absorbed. Each item
names the source path that proves it is still open, so an executor can
re-verify in one command before starting. Re-verify anyway: the previous
version of this file carried five "remains planned" claims that had already
shipped.

## Telephony and audio output

**1. Twilio μ-law pass-through.** `src/easycat/transports/twilio_media.py:1245`
and `:1752` both call `pcm16_to_mulaw(chunk.data, chunk.format.sample_rate)`
unconditionally. When a provider already returns μ-law 8 kHz, that call runs
the PCM→μ-law table over μ-law bytes and mangles the audio, so the branch is a
correctness gate, not only a 6x wire-bytes optimisation. Add the pass-through
branch on both send paths.

**2. Encoding-aware normalization in `TTSBase`.**
`src/easycat/tts/base.py:216` `_normalize_audio` handles mono downmix and
resample only; `grep mulaw src/easycat/tts/base.py` is empty. Decode μ-law to
PCM16 before downmix/resample and encode back afterwards, so item 1 can rely
on a correct source format instead of each transport guessing.

**3. Per-provider best native match.** Give each of
`src/easycat/tts/deepgram_tts.py`, `elevenlabs_tts.py`, `cartesia_tts.py`, and
`openai_tts.py` a private `_request_params_for(target)` that returns the
provider request plus the actual source format it will produce. Deepgram and
Cartesia can emit μ-law 8 kHz natively; ElevenLabs needs `ulaw_8000` added to
`_ELEVENLABS_FORMAT_MAP` (`elevenlabs_tts.py:56`) and its non-PCM rejection at
`:144` lifted; OpenAI is fixed at 24 kHz PCM16 and stays on the normalize
path. Depends on item 2.

**4. STT input-format negotiation.** The same alignment on the inbound side:
let a transport's native capture format reach the STT provider instead of
forcing a normalize hop. Sequenced after items 1-3.

## Privacy and redaction

**5. Export-time second redaction pass.**
`src/easycat/session/_session.py:989-993` — `export_debug_bundle` takes only
`(path, inline_artifacts, overwrite)`. Add an optional `redaction=` argument
that applies a second, potentially stricter pass on top of the runtime
default, running at export time so the original journal is untouched and
re-redaction is idempotent. This is the item that unblocks item 16. The
related `journal promote` privacy defect is tracked in
[../roadmap/open-backlog.md](../roadmap/open-backlog.md), not here.

**6. Hash strategy for redacted fields.** `RedactionPolicy` ships at
`src/easycat/validation/redaction.py:26` as the public `journal_redaction`
field with `secrets` and `pii` values. The `hash` strategy — stable SHA-256
truncated to 12 hex characters, salted per session so bundles cannot be
cross-referenced — does not exist. It is what makes "did this field change
across turns?" answerable without exposing content.

**7. Redaction presets.** Three composable defaults on top of items 5 and 6:
`development` (retain transcripts and tool args, drop keys and auth headers),
`production` (hash transcripts, drop audio and tool arguments, redact provider
payloads), and `regulated` (structural bundles only — stages, turns, and
errors, no payload content). Users compose their own or override one field of
a preset.

## Provider ecosystem

**8. Backchannel filter.** `grep -rni backchannel src/ tests/` returns
nothing. Suppress "mm-hmm"/"yeah" style listener noise before it reaches turn
taking, so a cooperative listener does not trigger an interruption.

**9. Smart Turn default-on promotion.** `src/easycat/smart_turn.py:397` still
reads `enabled: bool = False`. The bundled v3.2 ONNX support exists; the open
work is making it the default endpointing baseline, with auto-disable when a
conversational STT that owns endpointing is selected.

**10. Deepgram Flux auto-selection.** `src/easycat/stt/factory.py:40` already
derives the `native_endpointing` capability from a `flux`-prefixed model, so
the pipeline knows what Flux implies. What is missing is selecting it: pick
Flux automatically when `DEEPGRAM_API_KEY` is present and no explicit model is
configured, and have `easycat doctor` probe Flux reachability specifically —
its WebSocket handshake differs from the non-Flux Deepgram endpoints.

## Developer experience

**11. `EasyConfig.offline()`.** `grep -n "def offline" src/easycat/config/*.py`
returns nothing. A zero-key preset needs a local TTS to exist first, so this
is gated on the ecosystem, not on us. `EasyConfig` currently has 44 fields
(`len(dataclasses.fields(EasyConfig))`), which is the real reason a preset is
worth more than another flag.

**12. structlog adoption.** `grep -rn structlog src/ pyproject.toml` returns
nothing. The stdlib logger already supports the JSON and human renderers, so
this is a swap for processor composition, not for output formats — take it
only if a concrete need appears.

## Evals and dev loop

**13. Auto-reload.** No `reload` parameter exists on `VoiceApp.run`
(`src/easycat/voice_app.py:468`) and `CodeReloaded` returns no hits in `src/`
or `tests/`. A library helper — `run(..., reload=True)` or
`async with session.autoreload():` — watches for file changes and swaps the
agent module in-process at the bridge boundary, writing a `CodeReloaded`
checkpoint that the debugger timeline renders as a divider. `EASYCAT_DEV`
dev-debugger mode already ships (`src/easycat/cli/serve.py:161-166`,
`src/easycat/debugger/dev.py`); this is the missing half.

**14. Persona simulator.** No persona simulator exists in `src/`. Ship two
cooperating roles, not one: a Simulator that plays a configurable caller
persona and drives a multi-turn conversation toward a goal, and a Judge that
scores the transcript independently. One model cannot play both without
persona bleed. Persona, goal, and success criteria are plain text in a fixture
file so non-engineers can contribute cases; scripted turns remain supported as
the degenerate deterministic case. `assert_llm_judge` already ships at
`src/easycat/debug/testing.py:441` and is taught in the scaffold templates, so
the judge half has a foundation.

**15. Forked replay.** `src/easycat/runtime/replay.py:57` defines exactly
three fidelity classes — `ARTIFACT`, `SIMULATED`, `LIVE`. The fourth,
`forked_replay` (branch at a checkpoint, run forward under changed code),
returns no hits in `src/` or `tests/`. Depends on item 13's checkpoint
vocabulary.

## CLI surfaces gated on the above

**16. `bundles export --raw`.** `src/easycat/cli/debug/bundles.py:27-29`
documents that the context pack "omits raw transcripts, prompts, generated
text, tool payloads, and provider responses until the full redaction-policy
layer lands," and the module exposes no `--raw` option. Ship it only after
item 5, so `--raw` is an explicit opt-out of a real policy rather than the
absence of one.

**17. `replay --fail-on-regression`.** Exists only as a comment at
`src/easycat/debug/testing.py:237`. Turn the bundle assertion helpers into a
CLI exit code so replay can gate CI.

## Commands NOT in This Plan

Explicit non-goals, with reasoning. Carried from the retired CLI peripheral
plan; only its three cross-references to sibling plan files were repointed to
their post-cleanup homes.

- **`easycat run`** — generated projects already call
  `uv run --env-file .env python agent.py` or the template-specific server
  command. Adding a wrapper doesn't save meaningful keystrokes, and `run`
  without the broader set (logs, signal handling, flag pass-through) is
  strictly worse than a plain Python invocation. Defer; revisit if user
  research says it's missed.
- **`easycat dev`** — the dev loop (file watcher, bridge swap, debugger UI
  auto-launch) is genuinely valuable, but it's deep enough to deserve its own
  plan. The library surface for it (`run(..., debug="full")` plus an
  auto-reload helper) is item 13 above. Defer to a future extended CLI plan.
- **`easycat test`** — `pytest` with the `easycat.testing` plugin pre-loaded
  is a one-line convenience, but `pyproject.toml` can already register the
  plugin in `[tool.pytest.ini_options]`. Scaffolded templates do exactly that.
  No wrapper needed.
- **`easycat cost`** — runtime cost rollups were removed as undercooked and
  duplicative with the journal; the rationale is preserved in
  `plan/archive/peripheral-observability-and-cost.md`, and reviving cost
  budgets is an explicit do-not-revive entry in
  [../roadmap/open-backlog.md](../roadmap/open-backlog.md). Defer any
  dedicated command until cost tracking is revisited.
- **`easycat login` / credential store** — API keys go in `.env`. Managing
  credentials is the OS's job.
- **`easycat deploy`** — deployment is documented in
  [peripheral-deployment.md](peripheral-deployment.md); the CLI is not a
  deploy tool.
- **Plugin system** — `uv tool install easycat-plugin-foo` style extension is
  out of scope. Small, vendored command surface stays small.
- **`easycat update`** — `uv tool upgrade easycat` already works.

If a deferred command's case becomes obvious from user research, it lives in a
future extended CLI plan — separate plan, separate review.

## Permanent guardrail

EasyCat is a chained pipeline. Speech-to-speech (voice-to-voice) models are a
standing non-goal, not an unstarted item, and the reasoning is recorded in
[../../docs/architecture.md](../../docs/architecture.md). Do not file it here.

## Not tracked here

Deployment work is the one peripheral large enough to keep its own file:
[peripheral-deployment.md](peripheral-deployment.md). Everything else that was
once a peripheral document either shipped, moved to
[../roadmap/open-backlog.md](../roadmap/open-backlog.md), or was retired to
`plan/archive/` with a banner naming its current source of truth.
