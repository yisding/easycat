# CLI reference and discovery

This page is the maintained map of EasyCat's command families. Commands use the
installed CLI form; from this repository, prefix them with `uv run` (for
example, `uv run easycat doctor`). Run any command with `--help` for its full
option reference.

## First run and scaffolding

```bash
easycat console                    # offline text console
easycat console --voice-demo       # scripted, keyless audio-pipeline proof
easycat console --live             # explicit provider-backed console
easycat init my-agent              # scaffold a project
easycat init my-agent --easycat-git URL --easycat-git-rev REV # portable dependency
easycat init --list-templates # compare templates, base package requirements, env vars, files, preflight/check/fix/docs/json-schema/run commands
easycat init --list-templates --json # emit the machine-readable template catalog
easycat doctor           # check API keys, optional extras, provider reachability
easycat doctor --json    # emit machine-readable environment checks
easycat doctor --env-file .env
easycat doctor --env-file .env --json # emit checks with project .env loaded
easycat doctor --fix               # explicitly create repairable local state
```

`init --list-templates` reports each scaffold's base `easycat[...]` package
requirement and extras, required environment variables, optional environment
knobs, generated files, and copyable
create/preflight/check/fix/docs/json-schema/run commands. Use its JSON form for
automation.

By default, a CLI running from an editable EasyCat checkout records that local
path in the generated `[tool.uv.sources]`. Use `--easycat-git URL` with optional
`--easycat-git-rev REV` when the project must install in CI or on another
developer's machine. It is mutually exclusive with `--easycat-source PATH`.
JSON config uses `easycat_git`, `easycat_git_rev`, and `easycat_source` with the
same rules; Git credentials belong in a credential helper or SSH agent.

`doctor` distinguishes required, optional, unused, and not-applicable
requirements. Network liveness is not credential validation. The default run
does not create journal directories; `--fix` owns repair mutations.

## Running an application or playground

```bash
easycat serve
easycat serve --mode browser
easycat serve --manifest easycat.toml
easycat serve --manifest easycat.toml --profile production
easycat plan --manifest easycat.toml
easycat plan --manifest easycat.toml --profile production --json
```

Without `--manifest`, `serve` starts EasyCat's bundled playground agent. It does
not import a `VoiceApp` from the current directory. With a manifest, it builds
the selected `VoiceServer` profile. `plan` resolves the same provider and
capability inputs without starting the server. Roles the session does not build
are reported as `off` — a `vad` role, for example, when the STT declares
`native_endpointing` and owns turn boundaries — so their install extras are not
counted as blocking gaps.

## Documentation and error lookup

```bash
easycat docs             # list route labels and available audience filters
easycat docs --verbose   # expand every route with descriptions and command hints
easycat docs --audience learners # expand routes for one reader audience or broad role
easycat docs --audience learners --json # emit a filtered docs route map for learners
easycat docs --audience app-builders # filter docs to scaffold and app-building routes
easycat docs --audience app-builders --json # emit a filtered docs route map for app builders
easycat docs --audience operators # filter docs to deployment and observability routes
easycat docs --audience operators --json # emit a filtered docs route map for operators
easycat docs --audience maintainers # filter docs to architecture and maintenance routes
easycat docs --audience maintainers --json # emit a filtered docs route map for maintainers
easycat docs --audience coding-agents # filter docs to repository coding-agent routes
easycat docs --audience coding-agents --json # emit a filtered docs route map for coding agents
easycat docs --json      # emit docs routes, audiences, and command hints for automation
easycat explain E102     # look up errors and CLI schema topics
easycat explain json-schema # document the --json envelope and command metadata
easycat explain --list
```

`docs --json` returns route paths, audience labels, Diátaxis categories, command
hints, `available_audiences`, `available_audience_filters`, and the
`audience_alias_note`. `explain json-schema` defines the shared JSON envelope
and command-specific fields.

## Bundles, journals, replay, and debugging

```bash
easycat bundles list      # list captured debug bundles and crash dumps
easycat bundles list --json # emit machine-readable bundle list
easycat bundles show PATH # summarise a debug bundle or SQLite journal
easycat bundles show PATH --json # emit machine-readable bundle/journal summary
easycat bundles export PATH # write a redacted coding-agent context pack
easycat bundles export PATH --output DIR --json # emit context-pack metadata
easycat inspect PATH      # summarise a debug bundle or SQLite journal
easycat inspect PATH --json # emit machine-readable bundle/journal summary
easycat replay PATH       # replay a debug bundle or SQLite journal
easycat replay PATH --json # emit machine-readable replay summary
easycat latency PATH
easycat latency PATH --json
easycat diff PATH_A PATH_B
easycat diff PATH_A PATH_B --json
easycat journal grep PATH --query TEXT
easycat journal follow PATH
easycat journal promote PATH TURN_ID --out FILE
easycat tail PATH
easycat debugger serve PATH --no-open-browser
```

Use `bundles list` to discover files instead of guessing paths. `show` and
`inspect` accept a debug bundle or SQLite journal. `export` writes a redacted
coding-agent context pack. Journal search/follow output is redacted; promoted
turns become deterministic replay fixtures. See [observability](observability.md)
for lifecycle, retention, privacy, and storage-budget guidance.

## Validation

```bash
easycat validate quick
easycat validate quick --json
easycat validate socket
easycat validate socket --json
easycat validate stress
easycat validate stress --json
easycat validate contracts
easycat validate contracts --json
easycat validate latency --smoke
easycat validate latency --smoke --json
easycat validate live
easycat validate live --json
easycat validate release
easycat validate release --json
easycat validate report .easycat/validation/latest.json
easycat validate report .easycat/validation/latest.json --json
```

`quick` is deterministic and credential-free. `socket`, `stress`, and
`contracts` are explicit local lanes; `latency` and `live` can use provider
credentials and may incur charges. See the [validation workflow](validation.md)
for lane selection and release requirements.

## JSON contract

Commands that support `--json` return the standard envelope described by:

```bash
easycat explain json-schema
```

The command families include docs routes, template catalogs, scaffold output,
doctor environment/check rows, plans, validation runs/reports, bundle
list/show/export, inspection, and replay. Command-specific success fields
include `entries`, `commands`, `catalog`, `audience`, `audience_filter`,
`available_audiences`, `available_audience_filters`, `audience_alias_note`,
`command_note`, `base_requirement`, `create_command`, `repo_create_command`,
`next_step_commands`, `pyproject_name`, `run_command`, `check_command`,
`fix_command`, `easycat_source`, `easycat_git`, `easycat_git_rev`, `environment`,
`checks`, `validation`, `source_path`, and
`fidelity_effective`. Errors add fields such as `report_path`, `path`, and
`output_path` where relevant.

Replace uppercase or angle-bracket placeholders such as `PATH`, `DIR`,
`TURN_ID`, and `<session_id>` before executing copied commands.
