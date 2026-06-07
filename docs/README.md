# EasyCat Documentation

Use this page as the map for the maintained docs. Planning notes live under
`plan/`; the files below are the current reader-facing documentation.
From this repository, `uv run easycat docs` prints the same map; in an
installed app environment, use `easycat docs`. Use
`uv run easycat docs --json` when a script or coding agent needs the route map
with command hints and audience labels. Replace uppercase or angle-bracket
placeholders in command hints, such as `PATH` or `<session_id>`, before
running them. Use
`uv run easycat docs --audience learners` to narrow the human map, or
`uv run easycat docs --audience maintainers --json` when automation needs only
maintainer-facing route entries. The human docs menu also prints the available
audience labels so readers can choose a narrower route map without switching
to JSON first. Multi-word audience filters accept hyphens or underscores, so
`uv run easycat docs --audience app-builders` is equivalent to
`uv run easycat docs --audience "app builders"`.

## Choose Your Path

- New to EasyCat: start with the
  [repository path chooser](../README.md#choose-your-path), then use the
  [quickstart](../README.md#install) to run your first local voice bot. Run
  `uv run easycat doctor` before the example; if provider keys are in a
  project `.env`, run `uv run easycat doctor --env-file .env`.
- Learning voice pipelines from scratch: follow the
  [teaching ladder](teaching/), starting at
  [00-hello-audio](teaching/00-hello-audio/).
- Building an application: scaffold with
  `uv run easycat init my-agent`, or run
  `uv run easycat init --list-templates` to compare templates with best-fit
  guidance, base `easycat[...]` package requirements and extras, required
  environment variables, optional environment knobs, generated files, and
  copyable create/preflight/check/fix/docs/run commands. Use
  `uv run easycat init --list-templates --json` when a script or coding agent
  needs the same template catalog and post-scaffold command previews, then use
  the CLI commands documented in the [root README](../README.md#cli).
- Automating the CLI: use `uv run easycat docs --json` to inspect the docs
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
  underscore aliases for multi-word audience filters, and the top-level `command_note`
  distinguishes installed CLI hints from repo-local `uv run` hints.
- Looking for runnable reference apps: use the
  [examples command matrix](../examples/README.md) for local mic, WebSocket,
  WebRTC, Twilio, provider swaps, tools, and debug-bundle examples.
- Maintaining architecture or package boundaries: use the
  [architecture map](../CLAUDE.md) for the pipeline, key packages, provider
  registries, session lifecycle, test layout, and maintainer command block,
  including docs/onboarding guard recipes. Coding agents should also read the
  [repository agent guide](../AGENTS.md) for repo structure, development
  commands, docs/onboarding guard recipes, validation commands, and PR
  expectations.
- Maintaining public imports: review the
  [public API contract](public-api.md) before changing `easycat.__all__`;
  it points to the docs route map, focused public API test, and docs guard.
- Maintaining provider and protocol contracts: review the
  [provider contract map](../tests/contracts/README.md) before changing
  provider adapters, protocol cassettes, schema fingerprints, or bridge event
  grammar.
- Contributing code or tests: use the
  [contributor guide](../CONTRIBUTING.md) for the development loop, validation
  slices, docs/onboarding guard recipes (`just guard-docs`,
  `just guard-teaching`, `just guard-examples`, `just guard-templates`,
  `just guard-contributing`, `just guard-markdown`), marker taxonomy,
  cassettes, and provider-addition checklist. If `just` is not installed, use
  its raw command table for the equivalent `uv run pytest ...` commands.
- Operating sessions in production: read
  [deployment with Docker](deployment/docker.md) and
  [observability](observability.md) for journal CLI commands, the debugger UI,
  metrics, and traces. Start with `easycat bundles list`; from this repo, add
  `uv sync --extra debugger --group dev` when you need the UI. Then review the
  [journal durability contract](../src/easycat/runtime/DURABILITY.md) for
  persistence, recovery, and storage layout.
- Validating a change: run `uv run easycat validate quick`, inspect
  `uv run easycat validate report .easycat/validation/latest.json`, or use the
  matching JSON lanes (`uv run easycat validate quick --json`,
  `uv run easycat validate contracts --json`,
  `uv run easycat validate release --json`, and
  `uv run easycat validate report .easycat/validation/latest.json --json`) when
  a script or coding agent needs validation output inside the standard CLI
  envelope. Then use the validation workflow in the
  [root README](../README.md#validation-workflow) and the
  [validation reference](../plan/validation/reference.md) for provider and
  report vocabulary.

## Maintainer Notes

- Keep this index limited to current docs. Historical plans and workstream
  acceptance notes belong in `plan/`.
- Add a link here when a new top-level docs page becomes the maintained source
  for a user workflow.
- Prefer commands that work from the repository root, using `uv run ...` for
  local development.
