# EasyCat Documentation

Use this page as the map for the maintained docs. Planning notes live under
`plan/`; the files below are the current reader-facing documentation.
From this repository, `uv run easycat docs` prints the same map; in an
installed app environment, use `easycat docs`. Use
`uv run easycat docs --json` when a script or coding agent needs the route map
with command hints and audience labels. Replace uppercase placeholders in
command hints, such as `PATH`, before running them.

## Choose Your Path

- New to EasyCat: start with the [repository quickstart](../README.md#install),
  then run `uv run easycat doctor`. If provider keys are in a project `.env`,
  run `uv run easycat doctor --env-file .env`.
- Learning voice pipelines from scratch: follow the
  [teaching ladder](teaching/), starting at
  [00-hello-audio](teaching/00-hello-audio/).
- Building an application: scaffold with
  `uv run easycat init my-agent`, or run
  `uv run easycat init --list-templates` to compare templates with copyable
  create/check/run commands, then use the CLI commands documented in the
  [root README](../README.md#cli).
- Automating the CLI: use `uv run easycat docs --json` to inspect the docs
  route map with command hints and audience labels, then use
  `uv run easycat explain json-schema` for the standard `--json` envelope,
  including command-specific success and error fields. Replace uppercase
  placeholders in command hints, such as `PATH`, before running them. Each
  docs route entry includes an `audience` label for choosing the right starting
  point without scraping descriptions.
- Looking for runnable reference apps: use the
  [examples command matrix](../examples/README.md) for local mic, WebSocket,
  WebRTC, Twilio, provider swaps, tools, and debug-bundle examples.
- Maintaining architecture or package boundaries: use the
  [architecture map](../CLAUDE.md) for the pipeline, key packages, provider
  registries, session lifecycle, and test layout.
- Maintaining public imports: review the
  [public API contract](public-api.md) before changing `easycat.__all__`.
- Contributing code or tests: use the
  [contributor guide](../CONTRIBUTING.md) for the development loop, validation
  slices, marker taxonomy, cassettes, and provider-addition checklist.
- Operating sessions in production: read
  [deployment with Docker](deployment/docker.md) and
  [observability](observability.md), then review the
  [journal durability contract](../src/easycat/runtime/DURABILITY.md) for
  persistence, recovery, and storage layout.
- Validating a change: run `uv run easycat validate quick`, inspect
  `uv run easycat validate report .easycat/validation/latest.json`, then use
  the validation workflow in the [root README](../README.md#validation-workflow)
  and the [validation reference](../plan/validation/reference.md) for provider
  and report vocabulary.

## Maintainer Notes

- Keep this index limited to current docs. Historical plans and workstream
  acceptance notes belong in `plan/`.
- Add a link here when a new top-level docs page becomes the maintained source
  for a user workflow.
- Prefer commands that work from the repository root, using `uv run ...` for
  local development.
