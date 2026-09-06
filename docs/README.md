# EasyCat Documentation

Use this page as the map for the maintained docs. Planning notes live under
`plan/`; the files below are the current reader-facing documentation.
From this repository, `uv run easycat docs` prints a compact index of route
labels and audience filters; in an installed app environment, use
`easycat docs`. Expand the full map with `--verbose`, or narrow it by audience:

```bash
uv run easycat docs --verbose                # every route and command hint
uv run easycat docs --audience learners      # learning routes
uv run easycat docs --audience app-builders  # scaffold/app-building routes
uv run easycat docs --audience operators     # deployment/observability routes
uv run easycat docs --audience maintainers   # architecture/maintenance routes
uv run easycat docs --json                   # route map with command hints and audience labels
```

Coding agent? Use the root [AGENTS.md](../AGENTS.md) for repository coding
rules; use [llms.txt](../llms.txt) for machine-readable docs route discovery or
run `uv run easycat explain json-schema`. Replace uppercase or angle-bracket
placeholders in command hints, such as `PATH` or `<session_id>`, before running
them. Multi-word audience filters accept hyphens or underscores, so
`uv run easycat docs --audience app-builders` is equivalent to
`uv run easycat docs --audience "app builders"`. The `maintainers` and
`operators` filters also include compound labels such as `provider maintainers`,
`release maintainers`, and `operators and maintainers`.

## Choose Your Path

- New to EasyCat: start with the
  [repository path chooser](../README.md#choose-your-path), then use the
  [quickstart](../README.md#install) to run your first local voice bot. Run
  `uv run easycat doctor` before the example; if provider keys are in a
  project `.env`, run `uv run easycat doctor --env-file .env`, then run the
  example with `uv run --env-file .env python examples/openai_agents_voice.py`.
  Use the [installation and extras guide](install.md) when choosing a smaller
  install, another transport/provider, or an application dependency source.
- Learning voice pipelines from scratch: follow the
  [teaching ladder](teaching/), starting at
  [00-hello-audio](teaching/00-hello-audio/), and copy the generated
  [progress worksheet](teaching/PROGRESS.md) to track evidence-backed completion.
- Learning EasyCat's product surface by building an app: follow the
  [EasyCat feature ladder](using-easycat/), starting at
  [00-first-voice-app](using-easycat/00-first-voice-app/), then continue to
  [01-runtime-modes](using-easycat/01-runtime-modes/) and
  [02-providers-and-voices](using-easycat/02-providers-and-voices/), followed by
  [03-conversation-controls](using-easycat/03-conversation-controls/) and
  [04-tools-actions](using-easycat/04-tools-actions/), and
  [05-agent-bridges](using-easycat/05-agent-bridges/), and
  [06-session-control](using-easycat/06-session-control/),
  [07-observability](using-easycat/07-observability/),
  [08-testing-evals](using-easycat/08-testing-evals/),
  [09-multi-caller](using-easycat/09-multi-caller/),
  [10-telephony](using-easycat/10-telephony/), and
  [11-production-ops](using-easycat/11-production-ops/). It begins with the public
  `VoiceApp` API and adds runtime modes, providers, conversation controls,
  tools, sessions, debugging, evals, servers, telephony, and operations one
  capability at a time.
- Building an application: scaffold with
  `uv run easycat init my-agent`, or run
  `uv run easycat init --list-templates` to compare templates with best-fit
  guidance, base `easycat[...]` package requirements and extras, required
  environment variables, optional environment knobs, generated files, and
  copyable create/preflight/check/fix/docs/json-schema/run commands
  (`uv run easycat init --list-templates --json` emits the same template
  catalog and post-scaffold command previews), then use
  the compact CLI path in the [root README](../README.md#cli) and the
  maintained [CLI reference](cli.md) for every command family.
- Graduating from the quickstart to the production `Session` API: follow
  [from VoiceApp to EasyConfig to Session](from-easyconfig-to-session.md) for
  `create_session`, the `async with session:` lifecycle, event
  subscriptions, `send_text` and session actions, and `debug="full"`
  bundles you can inspect with `uv run easycat replay PATH`.
- Testing agents and running evals: climb the
  [testing and evals ladder](testing-and-evals.md) — bundle fixtures,
  offline text turns through `easycat.debug.testing` (`run_text_turn`,
  `run_text_turns` for a multi-turn scenario, `run_scripted_audio_turn`
  for one scripted pass through the audio pipeline, `assert_latency`,
  `assert_llm_judge`), teaching chapter 12 metrics, then
  live audio with `uv run easycat validate latency --smoke`. Scaffolded
  projects ship an offline `tests/test_agent.py` to start from.
- Automating the CLI: use [llms.txt](../llms.txt) for machine-readable docs
  route discovery, use
  `uv run easycat docs --json` to inspect the docs
  route map with command hints and audience labels, then use
  `uv run easycat explain json-schema` for the standard `--json` envelope,
  including command-specific success and error fields. Use
  `uv run easycat doctor --json` when automation needs first-run
  environment/check rows without Rich formatting; use
  `uv run easycat doctor --env-file .env --json` when those checks should load
  a project `.env`. Use `uv run easycat validate quick --json`,
  `uv run easycat validate contracts --json`,
  `uv run easycat validate release --json`, or
  `uv run easycat validate report .easycat/validation/latest.json --json` when
  automation needs validation run/report payloads. Replace uppercase or
  angle-bracket placeholders in command hints, such as `PATH` or `<session_id>`,
  before running them. Each docs
  route entry includes an `audience` label for choosing the right starting
  point without scraping descriptions. The top-level `available_audience_filters`
  lists copyable filter tokens such as `app-builders` and `coding-agents`;
  the top-level `audience_alias_note` documents shell-friendly hyphen and
  underscore aliases for multi-word audience filters and the broad
  `maintainers` / `operators` role filters, including `provider maintainers`,
  `release maintainers`, and `operators and maintainers`; the top-level `command_note`
  distinguishes installed CLI hints from repo-local `uv run` hints.
- Looking for runnable reference apps: use the
  [examples command matrix](../examples/README.md) for local mic, WebSocket,
  WebRTC, Twilio, provider swaps, tools, and debug-bundle examples.
- New to developing EasyCat itself: follow the
  [developer textbook](development/) for a guided source tour of the system
  map, session ownership, audio and turn-taking, agent streaming and
  interruption, providers and stages, journals and replay, production
  servers, testing, accepted decisions, and common change recipes. It links
  every chapter to the implementation and contract tests.
- Looking up the production API: start with the
  [architecture explanation](architecture.md) for how the pipeline and
  session collaborators fit together, then use the
  [events reference](reference/events.md), the
  [journal record reference](reference/journal-records.md), the
  [EasyConfig field reference](reference/easyconfig.md), and the
  [session lifecycle reference](reference/session-lifecycle.md); for Telnyx,
  also use the
  [Call Control setup guide](reference/telnyx-setup.md). Run
  `uv run easycat explain events`, `uv run easycat explain turn-taking`, or
  `uv run easycat explain journal` for terminal summaries that print the
  matching docs route. Every `easycat docs --json` route entry also carries a
  `diataxis` field (`tutorial`, `how-to`, `reference`, or `explanation`) so
  automation can pick the right kind of page.
- Talking to a bot in the browser: run `uv run easycat serve` and follow the
  [browser playground guide](browser-playground.md) for the one-command
  playground page (live transcript, interruption indicator, per-turn latency)
  and the WebSocket/WebRTC wire protocol behind it.
- Maintaining architecture or package boundaries: use the
  [architecture map](../CLAUDE.md) for the pipeline, key packages, provider
  registries, session lifecycle, test layout, and maintainer command block,
  including docs/onboarding guard recipes; the full architecture explanation
  lives in [docs/architecture.md](architecture.md), and the guided newcomer
  path lives in the [developer textbook](development/). Coding agents should also read the
  [repository agent guide](../AGENTS.md) for repo structure, development
  commands, docs/onboarding guard recipes, validation commands, and PR
  expectations.
- Maintaining public imports: review the
  [public API contract](public-api.md) before changing `easycat.__all__`;
  it points to the docs route map, focused public API test, and docs guard.
- Maintaining provider and protocol contracts: review the
  [provider contract map](../tests/contracts/README.md) before changing
  provider adapters, protocol cassettes, schema fingerprints, or bridge event
  grammar. Run `just guard-contracts` for that focused maintenance surface.
- Building a custom provider or transport: follow the
  [extending guides](extending/) for the duck-typed STT, TTS, VAD, transport,
  and agent-bridge surfaces, complete out-of-tree examples, and conformance
  checks. Scaffold an external package with
  `uv run easycat init my-stt --template provider-stt`,
  `uv run easycat init my-tts --template provider-tts`, or
  `uv run easycat init my-vad --template provider`.
- Contributing code or tests: use the
  [contributor guide](../CONTRIBUTING.md) for the development loop, validation
  slices, docs/onboarding guard recipes (`just guard-docs`,
  `just guard-teaching`, `just guard-examples`,
  `just guard-contributing`, `just guard-validation`, `just guard-contracts`,
  `just guard-ops`), marker taxonomy, cassettes, and
  provider-addition checklist. If `just` is not installed, use its raw command
  table for the equivalent `uv run pytest ...` commands.
- Operating sessions in production: read
  [deployment with Docker](deployment/docker.md), the
  [production multi-client server guide](deployment/production-servers.md), and
  [observability](observability.md) for journal CLI commands, the debugger UI,
  metrics, and traces. Start with `easycat bundles list`; from this repo, add
  `uv sync --extra debugger --group dev` when you need the UI. When a turn
  feels slow, use the [latency guide](latency.md) for the per-turn CLI
  waterfall and the table of latency-adding defaults. Then review the
  [journal durability contract](../src/easycat/runtime/DURABILITY.md) for
  persistence, recovery, and storage layout. Run `just guard-ops` when editing
  these operator-facing pages.
- Hardening a deployment or reporting a vulnerability: read the
  [security policy](../SECURITY.md) for private reporting, supported versions,
  and the index of security-relevant configuration (bearer-token auth and the
  non-loopback bind guard, per-caller isolation, telephony webhook trust, and
  journal redaction). Use `uv run easycat docs --audience operators` (or
  `--audience operators --json`) to list the operator routes it points at.
- Validating a change: run `uv run easycat validate quick`, inspect
  `uv run easycat validate report .easycat/validation/latest.json`, or use the
  matching JSON lanes (`uv run easycat validate quick --json`,
  `uv run easycat validate contracts --json`,
  `uv run easycat validate release --json`, and
  `uv run easycat validate report .easycat/validation/latest.json --json`) when
  automation needs validation output inside the standard CLI
  envelope. Then use the
  [validation workflow](validation.md) and the
  [validation reference](reference/validation-vocabulary.md) for provider and
  report vocabulary. Run `just guard-validation` when editing these
  validation-facing docs or the validate CLI behavior they describe.

## Maintainer Notes

- Keep this index limited to current docs. Historical plans and workstream
  acceptance notes belong in `plan/`.
- Add a link here when a new top-level docs page becomes the maintained source
  for a user workflow.
- Prefer commands that work from the repository root, using `uv run ...` for
  local development.
