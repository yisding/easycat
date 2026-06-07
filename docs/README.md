# EasyCat Documentation

Use this page as the map for the maintained docs. Planning notes live under
`plan/`; the files below are the current reader-facing documentation.
From this repository, `uv run easycat docs` prints the same map; in an
installed app environment, use `easycat docs`. Use
`uv run easycat docs --json` when a script or coding agent needs the route map
with command hints and audience labels. Replace uppercase placeholders in
command hints, such as `PATH`, before running them.

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
  copyable create/check/run commands. Use
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
  a project `.env`. Replace uppercase placeholders in command hints, such as
  `PATH`, before running them. Each docs route entry includes an `audience`
  label for choosing the right starting point without scraping descriptions,
  and the top-level `command_note` distinguishes installed CLI hints from
  repo-local `uv run` hints.
- Looking for runnable reference apps: use the
  [examples command matrix](../examples/README.md) for local mic, WebSocket,
  WebRTC, Twilio, provider swaps, tools, and debug-bundle examples.
- Maintaining architecture or package boundaries: use the
  [architecture map](../CLAUDE.md) for the pipeline, key packages, provider
  registries, session lifecycle, and test layout. Coding agents should also
  read the [repository agent guide](../AGENTS.md) for repo structure,
  development commands, validation commands, and PR expectations.
- Maintaining public imports: review the
  [public API contract](public-api.md) before changing `easycat.__all__`.
- Contributing code or tests: use the
  [contributor guide](../CONTRIBUTING.md) for the development loop, validation
  slices, docs/onboarding guard recipes (`just guard-docs`,
  `just guard-examples`, `just guard-templates`, `just guard-contributing`,
  `just guard-markdown`), marker taxonomy, cassettes, and provider-addition
  checklist.
- Operating sessions in production: read
  [deployment with Docker](deployment/docker.md) and
  [observability](observability.md), then review the
  [journal durability contract](../src/easycat/runtime/DURABILITY.md) for
  persistence, recovery, and storage layout.
- Validating a change: run `uv run easycat validate quick`, inspect
  `uv run easycat validate report .easycat/validation/latest.json`, or use
  `uv run easycat validate report .easycat/validation/latest.json --json` when
  a script or coding agent needs the saved report inside the standard CLI
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
