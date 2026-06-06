# EasyCat Documentation

Use this page as the map for the maintained docs. Planning notes live under
`plan/`; the files below are the current reader-facing documentation.
From this repository, `uv run easycat docs` prints the same map; in an
installed app environment, use `easycat docs`.

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
  create commands, then use the CLI commands documented in the
  [root README](../README.md#cli).
- Automating the CLI: use `uv run easycat explain json-schema` to inspect the
  standard `--json` envelope, including command-specific success and error
  fields.
- Looking for runnable reference apps: use the
  [examples command matrix](../examples/README.md) for local mic, WebSocket,
  WebRTC, Twilio, provider swaps, tools, and debug-bundle examples.
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
- Validating a change: use the validation workflow in the
  [root README](../README.md#validation-workflow), then consult the
  [validation reference](../plan/validation/reference.md) for provider and
  report vocabulary.

## Maintainer Notes

- Keep this index limited to current docs. Historical plans and workstream
  acceptance notes belong in `plan/`.
- Add a link here when a new top-level docs page becomes the maintained source
  for a user workflow.
- Prefer commands that work from the repository root, using `uv run ...` for
  local development.
