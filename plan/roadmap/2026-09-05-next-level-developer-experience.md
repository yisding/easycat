# EasyCat's next level: a shorter path from app to reliable change

Status: active backlog.
Date: 2026-09-05.
Evidence baseline: `2c59760107d963807d009f4c9aa2ad8d13e45efb`.

Make EasyCat easier to adopt, debug, and extend by connecting its existing
product surfaces and reducing the number of places that own the same decision.
The desired outcome is concrete: an application author can configure an app,
understand why it cannot start, test their own behavior offline, and turn a
failure into a useful regression. A maintainer can change one runtime policy
without rediscovering its lifecycle obligations across the package.

This is the execution detail for [open backlog §8](open-backlog.md#8-next-level-developer-experience).
It proposes future behavior; it does not document shipped APIs. Existing
bug-resistant work keeps its [original ordering and acceptance gates](2026-08-02-bug-resistant-refactor-plan.md).
Items referenced below retain their original owner; they are not new copies
of that work. Implement one concern per PR and refresh its evidence first.

## Current state and the opportunity

The source inspection found strong foundations worth preserving:

| Shipped foundation | Remaining friction observed in source | Implication |
| --- | --- | --- |
| [`VoiceApp`](../../src/easycat/voice_app.py), `EasyConfig`, and `Session.from_providers` already provide a progressive API. The [README](../../README.md) also offers an offline audio demo. | Application modes, config normalization, factory construction, and static planning have separate decision paths. | Improve agreement and explainability across these paths; preserve the entry-point ladder. |
| [`build_provider_plan`](../../src/easycat/planning/provider_plan.py) resolves seven roles without constructing providers. | It independently resolves defaults and shortcuts; [`test_parity.py`](../../tests/planning/test_parity.py) documents the need to keep it aligned with construction, and its success case can skip when extras are absent. | Share pure resolution decisions and add always-running parity cases. |
| [`doctor`](../../src/easycat/cli/diagnose/doctor.py) understands scaffold requirements, and [`errors.py`](../../src/easycat/errors.py) owns stable codes and fixes. | Doctor's requirements come from scaffold metadata and environment checks, while `plan` consumes manifest profiles. | Make diagnosis of a selected app use the same requirements as its plan. |
| Session already has collaborators, epochs, scopes, and a typed wiring object. | [`SessionWiringContext`](../../src/easycat/session/_wiring.py) exposes turn state, flags, providers, telephony, and lifecycle verbs to every collaborator. [`_session.py`](../../src/easycat/session/_session.py) is 2,511 lines at this revision. | Narrow collaborator capabilities and finish the existing ownership migrations. File size is a navigation signal, not a success target. |
| [`run_text_turn`](../../src/easycat/debug/testing.py), portable provider contracts, and generated offline tests already exist. | The default scaffold's [`test_agent.py`](../../src/easycat/cli/scaffold/templates/openai-agents/tests/test_agent.py) tests a separate `StubAgent`; its [`agent.py`](../../src/easycat/cli/scaffold/templates/openai-agents/agent.py) starts local mode at module scope. | Make generated tests exercise an importable application factory with injected boundaries. |
| Bundle replay and `journal promote` already exist. | [`_iter_artifacts`](../../src/easycat/debug/export.py) depends on private `_store`/`_dir` shapes; [`promote`](../../src/easycat/cli/debug/promote.py) validates saved-record replay and prints a test stub. | Make storage portable and distinguish an artifact consistency test from a test of changed application code. |
| [Import Linter contracts and mypy](../../pyproject.toml), [ratchets](../../tests/ratchets), and [named validation lanes](../../justfile) are established. | The wiring still includes `Any` and broad callables; stricter mypy settings are incremental. | Strengthen the interfaces changed by this program using the existing tools. |

These are scoped observations, not a fresh audit of every backlog claim. The
[July snapshot](current-code-status.md) and August backlog contain historical
premises; for example, the current README already leads with `VoiceApp`.
Use current code and tests before reviving an old API cleanup.

Open PRs were checked on 2026-09-05. Configuration-default work in
[#1059](https://github.com/yisding/easycat/pull/1059), turn ownership fixes in
[#1060](https://github.com/yisding/easycat/pull/1060) and
[#1061](https://github.com/yisding/easycat/pull/1061), and journal adapter work in
[#1055](https://github.com/yisding/easycat/pull/1055) overlap these areas. Recheck
their status and preserve their regression cases before implementation.

Review validation: `uv run --no-sync pytest tests/planning tests/cli/test_plan.py
tests/cli/test_errors.py -q` passed 56 tests with 8 skips;
`uv run --no-sync pytest tests/test_markdown_links.py tests/test_contributing.py -q`
passed 25 tests. This PR changes planning documents only. The implementation
commands below are future acceptance checks; no full runtime suite or live
provider tests were run for this proposal.

## Delivery order

| Priority / slice | Deliverable and dependency | Review size |
| --- | --- | --- |
| P0 / DX1 | One pure configuration resolution path; foundation for DX2 | 3 PRs |
| P0 / DX2 | Selected-app diagnostics using DX1 | 2 PRs |
| P0 / DX3 | Importable scaffold plus tests of the actual app; can begin independently | 2–3 PRs |
| P1 / DX4 | Narrow session interfaces, coordinated with existing WS1/WS2 work | 1 pilot PR, then one collaborator per PR if justified |
| P1 / DX5 | Portable export and useful failure-to-test workflow; storage/privacy first, application rerun after DX3 | 3–4 PRs |
| P1 / DX6 | Complete one third-party extension journey using DX1 and existing contracts | 2 PRs |
| P1 / DX7 | Focused contributor feedback and installed-package acceptance; begins with measurement | 2 PRs |

Ship DX1–DX3 as the first milestone. Start with DX1's characterization and
DX3's importable default scaffold; both deliver useful evidence before the
larger runtime work. Review that milestone before expanding DX4 across peers.
DX5's already-owned privacy work can proceed immediately under its existing
backlog entries. Do not postpone a correctness fix behind a structural slice.

Sizes are review boundaries, not elapsed-time promises. DX1–DX3 are a planning
allowance of roughly 10–15 engineering days, subject to the first resolution
spike; release dates depend on review capacity. Existing WS migrations are
additional work, not included in that estimate. Re-estimate the second
milestone from the pilot results instead of committing to a broad rewrite.

## DX1: resolve configuration once, then construct resources

**Problem.** A static plan can diverge from what startup selects, especially
after a mutable config changes or a default depends on another provider.

**Change.** Extract a private, typed resolution result from the existing
planner/factory decisions. Normalize shortcuts, selected roles, defaults,
requirements, and compatibility decisions through shared pure helpers. Keep
the public `ProviderPlan` as the safe diagnostic projection. `create_session`
uses the resolved inputs to construct resources inside its existing rollback
boundary. `VoiceApp` and manifest profiles adapt into that same path.

Resolution receives an explicit environment/probe snapshot. Diagnostic output
contains credential names and presence, never values. Caller-supplied provider
objects retain identity and ownership; do not deep-copy them. Represent
unknown custom capabilities as unknown. Keep provider construction, SDK imports,
network checks, and resource allocation out of the pure built-in path; document
the separate trust boundary for third-party entry-point discovery.

Split delivery into characterization tests, shared selection/normalization,
then consumer migration with removal of duplicated decisions. Retain mutable
`EasyConfig` compatibility by resolving a fresh snapshot at construction;
an earlier preview does not silently freeze later edits. Preserve explicit
overrides and current exception timing unless a separate behavior-change PR
documents and tests the change.

**Targets:** [`config/easy.py`](../../src/easycat/config/easy.py),
[`config/_factory.py`](../../src/easycat/config/_factory.py),
[`planning/`](../../src/easycat/planning), [`project/`](../../src/easycat/project),
and [`voice_app.py`](../../src/easycat/voice_app.py).

**Acceptance:** table-driven cases compare the resolved values actually passed
to injected factories with the preview, covering explicit configs, shortcuts,
late mutations, absent credentials/extras, fallback selection, and custom
instances. These cases run with only the dev group; real-extras smoke remains
an additional lane. Repeated previews neither mutate input nor allocate runtime
resources. No heavy-import regression and no new top-level export.

**Verify:** `uv run pytest tests/planning tests/config tests/test_voice_app.py`.

## DX2: make a setup failure explain the selected app

**Change.** Extend the existing `doctor` command to accept the same manifest and
profile selection as `plan`; derive selected-role requirements from DX1.
Preserve generic environment diagnosis and scaffold compatibility. Keep static
readiness distinct from explicit runtime/reachability probes: resolving a
profile must not import and execute its application or make provider calls.
Make the probe boundary visible in help and JSON.

Reuse the error registry to report a field/role, stable code, reason, and
actionable fix across Python startup, CLI, and readiness output. A browser
profile must not fail because a local microphone dependency is absent. A fix
must respect the project's dependency source, including Git pins while EasyCat
is unpublished. Existing codes, exception subclasses, and JSON keys remain
compatible; add optional fields or version a breaking schema deliberately.

**Targets:** [`cli/diagnose/`](../../src/easycat/cli/diagnose),
[`cli/plan.py`](../../src/easycat/cli/plan.py),
[`errors.py`](../../src/easycat/errors.py), and [`server/`](../../src/easycat/server).

**Acceptance:** missing credential, missing selected extra, invalid provider,
and incompatible selection each identify the same underlying cause in the
plan, doctor, and startup. Tests cover human/JSON output, secrets in exception
causes, unused extras, and unavailable hardware. Network probes remain bounded.
Record a short fresh-environment walkthrough from failure to successful retry.

**Verify:** `uv run pytest tests/cli/test_doctor.py tests/cli/test_plan.py tests/cli/test_errors.py tests/server/test_plan_endpoint.py tests/server/test_plan_route_auth.py`.

## DX3: generated tests should protect the generated application

**Change.** Give the default scaffold an importable app/agent factory and a
small guarded executable entry point. Tests call that factory with a
deterministic model or bridge boundary and safe tool dependencies. They should
preserve the application's configuration and tool wiring wherever the selected
framework supports it. Make the boundary explicit when a stub replaces agent
reasoning. Never imply that offline pipeline tests assess live model quality.

Reuse `run_text_turn` and journal assertions. Add only the smallest helper
needed to drive a multi-turn scenario on one session; caller-owned sessions
already support repeated turns. Prove the default template first, then migrate
other templates by framework. Keep the short beginner example and avoid a
required new application base class, YAML scenario language, or eval service.

**Targets:** [`cli/scaffold/templates/`](../../src/easycat/cli/scaffold/templates),
[`debug/testing.py`](../../src/easycat/debug/testing.py), and
[`tests/cli/e2e/`](../../tests/cli/e2e).

**Acceptance:** import never starts audio or a server. A generated project runs
offline tests outside this checkout with ambient credentials present and
provider traffic blocked. Intentionally breaking a tested app/tool behavior
makes its generated test fail. Cover two turns on one session, tool failure,
and forced stop with no owned tasks left behind. Keep at least one real-audio
stub scenario so a text-only success cannot stand in for audio coverage.

**Verify:** `just guard-examples` and `uv run pytest tests/debug tests/cli/test_console.py`;
record a generated-project run using the built wheel, not a source-tree import.

## DX4: narrow session dependencies without changing lifecycle policy

**Change.** Introduce small private protocols for the capabilities each
collaborator actually uses. Start with one collaborator whose dependency set
is easy to characterize, such as STT commitment. Keep the builder as the
composition point and live getters where runtime mutation requires them.
Do not replace the wiring object with an untyped service lookup mechanism.

This complements WS1/WS2; it does not replace their scope tree or add another
cancellation abstraction. Any extraction that changes scope ownership, turn
identity, or `stop()` ordering must satisfy those slices' prerequisites and
mapping tables. Preserve graceful/forced stop, exception precedence, stale-turn
fences, survivor accounting, audible cutoff, and post-stop journal access.

**Targets:** [`session/_wiring.py`](../../src/easycat/session/_wiring.py),
[`session/_builder.py`](../../src/easycat/session/_builder.py), the selected
collaborator, and its existing lifecycle tests.

**Acceptance:** the pilot collaborator can be constructed with a small typed
fake without building a `Session`; it cannot access unrelated lifecycle or
telephony mutations through its interface. Remove obsolete wiring in the same
PR. Tighten mypy on the changed interface without introducing new `Any`
escape hatches. Run existing cancellation and stale-turn regressions before
expanding. Continue only if the pilot reduces dependencies and test setup;
moving methods into more files alone does not justify migration.

**Verify:** `just typecheck`, `just lint`, and
`uv run pytest tests/session tests/integration/test_session_lifecycle_e2e.py tests/ratchets`.
Run the existing contracts/stress lanes when an adoption changes their domain.

## DX5: make failure capture portable and regression tests meaningful

**Change.** First deliver the existing [export/promotion privacy work](open-backlog.md#1-security-and-privacy-now)
and [artifact-store gap](open-backlog.md#4-critique-residue-api-dx-and-packaging).
Read artifacts through the public store contract using references from a
consistent journal snapshot, or a narrowly defined optional snapshot capability
if enumeration is necessary. Avoid requiring access to private backing storage.
Report missing/truncated artifacts and the resulting replay fidelity explicitly.

Then resolve [promotion's existing surface decision](open-backlog.md#32-decide-the-promotion-surface-q13)
by extending `journal promote`. Offer clearly described saved-record assertions
and an application-rerun test template built on DX3. A saved bundle can prove
record consistency; it cannot by itself prove that current application code
has been fixed. Let authors supply the expected behavior when it cannot be
recovered safely from captured evidence. No automatic execution of recorded
tools, and no claim that redaction preserves full replay fidelity.

**Targets:** [`runtime/artifacts.py`](../../src/easycat/runtime/artifacts.py),
[`debug/export.py`](../../src/easycat/debug/export.py),
[`cli/debug/promote.py`](../../src/easycat/cli/debug/promote.py), and existing
bundle/replay contracts. Reuse the shared redaction policy.

**Acceptance:** a third-party store implementing the documented interface
exports referenced artifacts without private attributes. Export works after
`stop()` and identifies incomplete captures. Default shareable promotion omits
audio and applies export redaction; generated source contains no captured
secrets. The application-rerun example fails on a seeded application bug and
passes after its fix, offline. Unsupported capture data produces an explicit
limitation rather than a false passing regression.

**Verify:** `uv run pytest tests/runtime tests/debug tests/cli/test_bundles.py`;
also run the existing promotion tests in `tests/cli` and a generated test in a
temporary consumer project. Add focused compatibility cases before changing
any artifact or journal format.

## DX6: prove the third-party extension path end to end

**Change.** Use one tiny external provider package to exercise the complete
journey: scaffold, registration, planning, doctor, construction, contract tests,
and a stub session. Source capability and installation metadata from the
existing catalogs. Close gaps found by this journey before adding more registry
abstractions. Publish supported/unsupported/unknown capability distinctions
with links to the contract cases that establish them.

**Targets:** [`_provider_catalog.py`](../../src/easycat/_provider_catalog.py),
[`testing/`](../../src/easycat/testing), provider templates, and
[`docs/extending/`](../../docs/extending).

**Acceptance:** the external fixture is installed alongside an EasyCat wheel,
with no imports from this repository's tests. One provider registration plus
its typed config supplies discovery metadata to every consumer; new extension
behavior does not require edits to CLI switch statements. Cover registration
collision, unknown capabilities, and clear contract failure messages. Keep
provider SDK dependencies optional and the default contract path offline.

**Verify:** `just guard-contracts`, `just guard-examples`, and the installed
fixture smoke. This work owns the journey; [backlog 4.7 and 6.1](open-backlog.md)
continue to own the broader bridge matrix and provider cassette coverage.

## DX7: make the contributor loop reflect the change

**Change.** Measure existing local lanes and document the smallest reliable
command for a config, provider, lifecycle, scaffold, or docs change. Derive any
new routing from maintained guard/lane metadata; avoid another handwritten
test-selection table. Keep the full required checks as the pre-merge authority.
Profile a slow guard before adding caching or changing its scope.

Strengthen the existing mypy/import contracts at newly narrowed boundaries.
Use consumer typing fixtures for APIs touched by DX1–DX3. Run the flagship
configure/diagnose/test/export journey against an installed wheel outside the
checkout through the existing release validation infrastructure.

**Targets:** [`justfile`](../../justfile), [`CONTRIBUTING.md`](../../CONTRIBUTING.md),
[`pyproject.toml`](../../pyproject.toml), [`tests/typecheck/`](../../tests/typecheck),
and existing CI/release validation jobs.

**Acceptance:** publish cold/warm timings and the machine/environment for the
selected commands; do not claim improvement without a baseline. The focused
path discovers the relevant regression, and required CI retains behavioral
coverage. No new guard scans the whole tree to enforce a file move or an
arbitrary line ceiling. Installed-consumer checks catch accidental imports of
checkout-only modules. Reuse the current checker rather than adding a second
authoritative type checker.

**Verify:** relevant guard recipes plus `just check` for implementation PRs;
`just validate-release` for the installed-package milestone.

## Milestone review and compatibility

The first milestone is complete when the same selected app has consistent
preview/startup decisions, useful failure recovery, and a generated test that
detects a change to its own behavior. Use five repeatable journeys: offline
demo, local app with a missing dependency, browser app without audio hardware,
custom provider, and captured failure reproduced as a test. Record commands,
manual edits, completion/failure, and elapsed time on a stated environment.
These are new UX measurements, separate from the frozen refactor metrics.

At the second milestone, compare the DX4 pilot's dependency surface and test
setup with its baseline; verify third-party export and extension smoke; and
repeat the journeys against the wheel. Stop expanding an abstraction if it
does not improve these outcomes. Carry evidence and completed PR links back
to the owning slice, then retire finished detail under the operating model.

Keep public entry points, `Session.stop()` semantics, protocol compatibility,
optional installs, and the retained peer set. Deprecations need a supported
replacement and migration example in their own PR. Old bundles remain readable;
format changes require compatibility fixtures. Each structural PR must be
independently revertible and remove the duplicate path it supersedes.

Defer new providers/transports, a plugin marketplace, a package split, an eval
platform, public namespace culling, and a new lifecycle engine. They do not
advance the first milestone. Preserve the [existing do-not-revive decisions](open-backlog.md#7-decided-do-not-revive).
Publishing a package or changing release policy is a separate decision; this
plan prepares installed-package evidence without scheduling a publication.
