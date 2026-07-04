# EasyCat Maintainability Plan

Synthesized from confirmed, verified proposals. Ordered by impact-per-effort within each phase. Line numbers and file paths are carried from verification and should be treated as anchors, not gospel (the repo moves fast).

Legend for dependencies: **→** means "must land after". Items with no arrow are independent and can be parallelized.

---

## Phase 1 — Quick wins (hours; land now)

### QW1. Fix the `TYPE_CHECKING` re-export drift in `__init__.py` (+ parity guard)
- **What:** In `src/easycat/__init__.py`, extend the existing imports (no new statements): change the `easycat.vad` import to `from easycat.vad import VADConfig, create_vad`, and add `SupervisorListenerAttached` / `SupervisorListenerDetached` to the `easycat.events` block. Add a test in `tests/test_public_api.py` that AST-parses the `if TYPE_CHECKING:` node and asserts `(module, name)` parity with `LAZY_EXPORTS` (reuse the file's existing `ast` helpers).
- **Why:** This is a real, shipped drift — three names in `LAZY_EXPORTS` resolve to `Any` for consumers of this `py.typed` package; 2026 practice for typed libraries is proving the static surface matches the runtime registry.
- **Validate:** `just guard-docs` (runs `tests/test_public_api.py`). Under an hour.

### QW2. Repair the out-of-sync `all` extra (+ union guard)
- **What:** Add `langchain-core>=1.3.3` and `langgraph>=1.2.6` to the `all` list in `pyproject.toml` with a comment documenting the **three** excluded extras and reasons: `ten-vad` (non-permissive license), `pydantic-ai` and `pydantic-ai-v2-beta` (mutually exclusive via `[tool.uv].conflicts`). Add a test in `tests/test_dependency_policy.py` asserting `set(extras["all"]) == union(all extras − exclusion-keys − "all")` **and** that every exclusion key is a declared extra (stale-exclusion check, mirroring `scripts/extras_matrix.py`). Compare raw requirement strings so the test doubles as a pin-drift guard. Run `uv lock` after the edit.
- **Why:** `pip install easycat[all]` cannot dispatch the LangChain/LangGraph bridges today (they runtime-import those packages) — a real, silent packaging break.
- **Validate:** `uv run pytest tests/test_dependency_policy.py`; the nightly extras matrix already install-tests `all`.

### QW3. Delete the fake `integration-live` CI job
- **What:** Remove `ci.yml` lines ~177-188 (the `workflow_dispatch`-gated `pytest -m integration_live` job with no secrets — a permanent no-op that self-skips). Add a small assertion to `tests/test_ci_workflow.py` (`"integration-live" not in workflow["jobs"]`) pointing at nightly's `live-canaries` as the real path. Update `plan/validation/reference.md:579` ("CI Shape") which still lists this job.
- **Why:** A permanently-green check that tests nothing is an anti-pattern; the real, secret-gated live lane already exists in `nightly-validation.yml`. Do **not** wire secrets into ci.yml — that job is dispatchable on any branch and would be a security regression versus nightly's `environment: live-validation` + `ref_protected` gating.
- **Validate:** `uv run pytest tests/test_ci_workflow.py`.

### QW4. Enforce `uv.lock` in CI, cache uv, add a concurrency group
- **What:** Change every `uv sync --group dev` to `uv sync --locked --group dev` across `ci.yml`, `nightly-validation.yml`, `release-validation.yml`. Add `enable-cache: true` + `cache-dependency-glob: "**/uv.lock"` to every `astral-sh/setup-uv` step (works on the already-pinned `@v4` — **no version bump needed**). Add to `ci.yml` top-level:
  ```yaml
  concurrency:
    group: ci-${{ github.ref }}
    cancel-in-progress: ${{ github.event_name == 'pull_request' }}
  ```
  **Must update in lockstep** three exact-substring asserts in `tests/test_ci_workflow.py` (lines ~78, ~557, ~597) that break when `--locked` is inserted, then add a blanket assert that every `uv sync` carries `--locked` and a concurrency-shape assert. Skip `required-version` in `[tool.uv]` (recurring version-churn friction for little gain over `--locked`).
- **Why:** Bare `uv sync` silently re-resolves on drift, defeating the committed lockfile as a supply-chain control; caching + PR-run cancellation cut cost/latency across a 4-version matrix in an agent-heavy repo. `uv lock --check` passes today, so `--locked` will not red CI.
- **Validate:** `uv lock --check` locally first; `uv run pytest tests/test_ci_workflow.py`.

### QW5. Add a LICENSE file and PEP 639 metadata
- **What:** Add `/LICENSE`, and `license = "<SPDX>"` + `license-files = ["LICENSE"]` to `[project]` in `pyproject.toml`. Do **not** add a `License ::` trove classifier (PEP 639 deprecates mixing forms). Add a one-line assert to `tests/test_dependency_policy.py` using the existing `_pyproject()` helper: `"license" in _pyproject()["project"]` (and optionally `LICENSE` exists at repo root).
- **Why:** No license grant exists, yet CI build-smoke twine-checks distributable wheels — the package is legally unusable/unpublishable downstream.
- **⚠️ Owner gate:** License choice (MIT vs Apache-2.0 — the latter's patent grant is commonly preferred for AI/voice infra) is a one-way product decision requiring the repo owner's explicit sign-off. Update `authors`/copyright holder in the LICENSE text if a real name should appear. **Blocks QP2 (publishing).**
- **Validate:** `uv build && uvx twine check dist/*` (existing build-smoke lane).

### QW6. SHA-pin the three tag-pinned workflows + expand Dependabot + add zizmor
- **What:** Replace every mutable tag ref (`actions/checkout@v4`, `astral-sh/setup-uv@v4`, `extractions/setup-just@v3`, `actions/upload-artifact@v4`) in `ci.yml`/`nightly-validation.yml`/`release-validation.yml` with 40-char SHAs + trailing `# vX.Y.Z` comment. **Pin at the current tags' SHAs — do not fold in the setup-uv v4→v8 major bump**; let the new github-actions Dependabot ecosystem propose that as a reviewable PR. Add `persist-credentials: false` to all checkouts (docs.yml already does). Append `github-actions` (unambiguous) and best-effort `pip` (for `requirements-docs.txt`; verify its first PR regenerates hashes sanely, or drop it) ecosystems to `.github/dependabot.yml`. Add the zizmor hook to `.pre-commit-config.yaml` and a `uvx zizmor .github/workflows` step to ci.yml's lint job.
- **Coordination:** `tests/test_ci_workflow.py` hardcodes `"actions/upload-artifact@v4"` in ~9 places (incl. an exact-equality at line ~510) and `plan/validation/tasks.md` references the token — update all three in the same PR (switch literals to `startswith("actions/upload-artifact@")` or the pinned SHA). This is the bulk of the work.
- **Why:** Post tj-actions/changed-files, SHA-pinning + Dependabot-managed pins + zizmor are baseline — and the secret-bearing live/release lanes are exactly the currently tag-pinned workflows.
- **Validate:** `uv run pytest tests/test_ci_workflow.py`; `uvx zizmor .github/workflows` (expect it to flag the very tags being pinned — self-consistent).

### QW7. Run pre-commit in CI and align the ruff hook to the locked version
- **What:** Add a `pre-commit` job to ci.yml running `uv sync --group dev` then `just pre-commit` (or `uv run pre-commit run --all-files` — **not** `uvx`, since pre-commit is a locked dev dep). Cache `~/.cache/pre-commit`. Fix the ruff skew: `.pre-commit-config.yaml` pins ruff-pre-commit `v0.15.8` while `uv.lock` locks `0.15.17` — prefer converting to a `repo: local` hook shelling to `uv run ruff check`/`format` so exactly one ruff version exists (rides existing uv Dependabot coverage). Optionally `SKIP=ruff-check,ruff-format` in the CI pre-commit job to avoid duplicating the dedicated lint job.
- **Why:** No workflow runs pre-commit today, so actionlint/codespell/check-yaml enforce only for contributors with local hooks; the ruff version skew lets local formatting fight CI.
- **Ordering:** Do the ruff alignment **first or same-PR**, or the new job's ruff 0.15.8 disagrees with the lint job's 0.15.17. Depends on **QW6** (`persist-credentials: false`) so zizmor's first run is clean.
- **Validate:** `just check` locally; the new CI job itself.

### QW8. Adopt PEP 702 `@deprecated` on the compatibility aliases (+ guard)
- **What:** Add `typing-extensions>=4.5` to `[project.dependencies]` (currently only transitively locked). Decorate `Session.shutdown`/`close`/`destroy` (`session/_session.py`) with `@deprecated(...)`; emit `DeprecationWarning` in the `settings`-fold branches of `stt/factory.py` and `tts/factory.py`. **Drop the `_PROVIDERS` piece** — PEP 702 can't decorate a module attribute and it's underscore-private. Add `tests/test_deprecations.py` asserting each alias raises via `pytest.warns`. Add a two-sentence removal policy to `docs/public-api.md` (guard its heading in `tests/test_public_api.py`).
- **Coordination:** Migrate the ~15 tests calling `await session.shutdown()` to `stop(force=True)`; update the guard-tested "thin alias" wording in AGENTS.md/CLAUDE.md (`tests/test_dx_helpers.py:535-543`) and the Session Lifecycle section (`tests/docs/test_route_contracts.py:175-189`); wrap/migrate `settings=`-using factory tests. `__aexit__` already calls `stop(force=True)`, so the async-with path is unaffected.
- **Why:** Establishing machine-visible (type-checker + IDE) deprecation signals at 0.1.0 is cheap and matches the repo's guard-test investment; the aliases carry zero runtime/static signal today.
- **Validate:** `uv run pytest tests/test_deprecations.py`; `just guard-docs`.

### QW9. Enable ruff `ASYNC`, `B`, `RUF006` with the grandfather-list ratchet
- **What:** Extend `select` in `pyproject.toml [tool.ruff.lint]`. Two policy decisions from verification: (a) add `[tool.ruff.lint.flake8-bugbear] extend-immutable-calls = ["typer.Option", "typer.Argument"]` to kill all 19 B008 Typer false positives cleanly — **do not grandfather them**; (b) put `ASYNC109` in a **permanent** `ignore` (explicit timeout params are deliberate public API here, incl. `timeouts.py` itself). Then `ruff check --fix` the ~14 safe fixes, and seed a **new never-grow** per-file-ignores block for the ~57 remainder. `RUF006` and `B023` have zero current hits (free forward guards). Fix the doc drift: `CLAUDE.md:131` and `justfile:38` still claim "E, F, I, W, UP" (omitting even the enabled C901/PLR rules).
- **Priority within the ratchet:** The `ASYNC230`/`ASYNC240` blocking-I/O-in-async hits in `transports/webrtc.py` and `server/webrtc_routes.py` are genuine event-loop-stall (audible-latency) bugs — flag them to fix first, not just noqa.
- **Why:** For an async-first realtime audio framework, flake8-async catches exactly the blocking-call defects that become audible latency; the proven "fix a file → delete its line" ratchet is ready to absorb them. Effort is closer to a day than "days" given the two policy decisions.
- **Validate:** `uv run ruff check .`; `just check`.

### QW10. Move `is_loopback_host` / `normalize_auth_token` into a leaf `_net.py`
- **What:** Create `src/easycat/_net.py` (zero intra-package imports) with `is_loopback_host(host: str | None) -> bool` (returns False for None; keep webrtc's `strip().strip("[]").lower()` normalization) and `normalize_auth_token`. **No body reconciliation needed** — the two `_normalize_auth_token` copies are behaviorally identical (carry the richer websocket docstring). Update call sites downward: `transports/webrtc.py`, `transports/websocket.py`, `server/auth.py`, `cli/serve.py`, `debugger/server.py`, **`voice_app.py:729-730`** (lazy-imports both), **`server/webrtc_routes.py:77`** (module-level). Delete the three cycle-workaround lazy imports + debugger's private `_hostname_is_loopback`. Update the now-stale prose narrating the workaround (`server/auth.py:31`, `server/__init__.py:45`, `server/config.py:20`, `voice_app.py:724`). Optionally gate `_net.py` in `mypy_gated_paths` (`justfile:58` **+** the `[[tool.mypy.overrides]]` list in `pyproject.toml` — kept in sync).
- **Why:** Canonical helpers living in a 1638-line optional-dependency module is how cycles creep in; this deletes three documented cycle-workarounds and gives one canonical implementation. **Shrinks the ignore list for QS4 (Import Linter forbidden contract).**
- **Validate:** `uv run pytest tests/transports tests/server`; `just typecheck` if gated.

### QW11. Additional config-parity guard tests (justfile quick-slice, mypy path list)
- **What:** Two pure-guard tests beyond QW1/QW2's guards: (a) in `tests/cli/test_validate_runner.py`, parse the `test-fast`/`cov` recipes via `scripts/_justfile.py` and assert their `-m` expression equals `VALIDATION_SELECTORS["quick"]` (`runner.py:62`) — today only ci.yml's copy is guarded; (b) in `tests/test_dependency_policy.py`, assert `justfile:58 mypy_gated_paths` maps 1:1 to the `pyproject.toml [[tool.mypy.overrides]]` module globs (coupled only by a comment today).
- **Why:** Extends the repo's own "derive expectations from live code" guard-test philosophy to two hand-synced pairs that currently have no guard.
- **Validate:** `uv run pytest tests/cli/test_validate_runner.py tests/test_dependency_policy.py`.

---

## Phase 2 — Structural refactors (sequenced, incremental)

General rule for all splits: **one concern per PR**, keep the original file as a **facade that re-exports** moved names (several tests and `cli/_app.py` import privates by name), and move constants **with** their functions (re-export aliases break `monkeypatch`).

### QS1. Module line-budget ratchet guard *(do first — freezes growth)*
- **What:** Add `tests/test_module_size_ratchet.py`: walk `src/easycat/**/*.py`, fail any file >1000 lines unless in a checked-in `{path: line_count}` allowlist that may only shrink. Seed with **all 13** current offenders (not 7): `debugger/server.py` 3006, `cli/debug/bundles.py` 2445, `validation/runner.py` 1734, `transports/webtransport.py` 1660, `transports/webrtc.py` 1638, `integrations/agents/langgraph.py` 1579, `session/_session.py` 1504, `integrations/agents/llama_agents.py` 1372, `transports/twilio_media.py` 1368, `server/voice_server.py` 1059, `config/easy.py` 1059, `integrations/agents/pydantic_ai.py` 1035, `runtime/journal_sql.py` 1029. Exclude `cli/scaffold/templates`. **Critical:** store as a `{path: count}` dict, and never write `path:NNN` colon-form in the test's own comments — `tests/test_source_hygiene.py` fails on brittle `file.py:NNN` refs. Soften the shrink rule to a tolerance (fail on growth, or when an entry drops ≤1000 → delete it) to avoid churn on every refactor commit. Header comment: "shrink this dict, never grow it," pointing at `docs/architecture.md` for split seams.
- **Why:** 2026 practice operationalizes "keep files small for agents" as a CI ratchet — agent edit accuracy degrades sharply on 2-3k-line files, and `server.py` (+1574 lines/month) and `bundles.py` (+2135/month) are actively doubling. Mirrors the proven C901 grandfather-list discipline.
- **Validate:** `uv run pytest tests/test_module_size_ratchet.py`; `just check`. **Each split below then shrinks its allowlist entry.**

### QS2. Split `cli/debug/bundles.py` (2445 lines) *(lowest-risk split; 2-3 PRs)*
- **What:** Extract `_summary.py`/`_common.py` (shared `_load_bundle_or_journal`, `_print_wide`, `_summarise_bundle`) first, then per-command modules (`export.py`, `replay.py`, `latency.py`, `diff.py`, `promote.py`, `grep.py`, `follow.py`), one-to-two commands per PR ordered by churn (latency, diff first). **Keep the explicit `bundles_app.command(...)(func)` registration block in `bundles.py`** (preserves `--help` ordering exactly, eliminating the stated Typer-order risk) and keep `bundles.py` as a **facade** — `cli/_app.py:948-976` and four test files import moved privates by name; re-export via `__all__`.
- **Why:** Second-hottest file (42 commits/month), pure aggregation of ~11 independent Typer commands — mechanical to split, high agent-maintainability payoff.
- **Validate:** `just guard-ops` (`tests/cli/test_bundles.py`) + `just guard-docs` (JSON envelope contract).

### QS3. Split `debugger/server.py` (3006 lines) *(5 PRs, ordered)*
- **What:** One PR per module: (1) `_records.py` (filter/search/pagination + regex-backtracking analyzer), (2) `_audio.py` (PCM/WAV/frame coercion), (3) `_sources.py` (`DebuggerSource` + `_bundle_source`/`_session_source`), (4) `_aec_routes.py` (AEC diagnostics + VAD what-if), (5) convert `_make_app`'s 29 nested handlers to module-level functions taking `source`/`registry` explicitly (follow the existing `debugger/_aec.py` sibling pattern; thread the dev-mode proxy source explicitly). **Move constants with functions** (tests monkeypatch `_AEC_MAX_TRACK_BYTES` as a module global) and update the ~30 `from easycat.debugger.server import _helper` sites in `tests/debugger/`. Keep `aiohttp` imports **lazy** in extracted route modules (optional extra). Gate each pure-Python leaf via `mypy_gated_paths` + `[[tool.mypy.overrides]]` (both, in sync).
- **Why:** Largest and hottest module (55 commits/month) with an 886-line `_make_app` closure whose handlers can't be imported or unit-tested — the exact partial-read failure mode for agents.
- **Validate:** `uv run pytest tests/debugger/` per PR (`just guard-ops` additionally only for bundles/journal-adjacent steps).

### QS4. Import Linter contracts *(Phase 1 lands green; Phase 2 after splits)*
- **What — Phase 1:** Add `import-linter>=2` to dev deps; add `[tool.importlinter]` (`root_package = "easycat"`, `exclude_type_checking_imports = true`) with two low-ambiguity contracts: (1) **independence** among `easycat.stt`/`tts`/`vad` **only** (verified clean; **exclude transports/telephony** — they're genuinely bidirectionally coupled via `twilio_media.py`↔`telephony.dtmf`); (2) **forbidden** `server`/`cli`/`debugger` → `transports.webrtc`, seeded with `ignore_imports` for the 4 current edges (`server/auth.py`, `cli/serve.py`, `server/webrtc_routes.py`, `server/voice_server.py`). Wire `uv run lint-imports` into the justfile lint recipe, ci.yml lint job, and a `repo: local` pre-commit hook. **Phase 2 (separate PR):** the full `layers` contract — first resolve/grandfather the known real violations (`transports/websocket.py:32` module-level `session_manager`; function-local `session_manager` at `webrtc.py:462`, `webtransport.py:1482`), ratcheting `ignore_imports` down per the C901 convention.
- **Why:** The layering exists only in prose comments guarding a documented history of hand-fixed cycles; Import Linter turns it into a machine-enforced contract in seconds of CI time — especially valuable while the splits above churn import edges.
- **Ordering:** **QW10 (`_net.py`)** lets you shrink the forbidden-contract ignore list. Phase 2 **→ after** QS2/QS3/QS7 (import edges settle).
- **Validate:** `uv run lint-imports`; `just check`.

### QS5. Extract a `LaneHarness` from `validation/runner.py`'s four lanes *(4 PRs)*
- **What:** Add `src/easycat/validation/_lane_harness.py` with `_start_lane_run` (owns run-id/run-dir creation, report-path resolution, base artifacts dict) and `_finish_lane_run` (git/env metadata stamping, the triple atomic report write, `ValidationRunResult` assembly). Convert one lane per PR. **Scope to prologue+epilogue only** — do not unify the stdout/stderr/junit redaction (slice uses `redact_text`, latency uses `redact_runtime_secrets`; live/release accumulate multi-command logs). `_finish_lane_run` takes lane-specific fields (`latency=`, `reliability=`, `skips=`, a caller-supplied `command`) as passthrough. Release strictness lives in `run_live_validation`'s params, **not** the scaffolding — lower risk than proposed. Keep `VALIDATION_SELECTORS` untouched, but **update source-text pin tokens** in `tests/cli/test_validate_runner.py` and (for the latency lane) `tests/cli/test_latency_selectors_artifacts.py`/`test_latency_reliability_failures.py` — re-anchor to `_lane_harness.py`. Gate the new module in the mypy paths.
- **Why:** Four copy-pasted lane skeletons mean any report-format change is a four-site edit, in a 1734-line hot module the framework's own docs call the observability backbone.
- **Validate:** `just guard-validation` after each lane conversion.

### QS6. Finish the M7 WebRTC convergence *(3 PRs)*
- **What — PR1 (safety net):** `tests/transports/test_webrtc_route_parity.py` asserting the two `_cors_headers` and `_stats_*` implementations agree across an origin/token/quota matrix (include a **non-ASCII credential** case pinned to the corrected 401 behavior). **PR2:** replace `webrtc.py`'s `_request_authorized` with `server.auth.BearerTokenAuth` (build the policy only when a token is configured — map no-token → no-policy/open; pass `allow_query_token`). This **fixes a latent DoS**: the current unguarded `compare_digest` raises `TypeError`→HTTP 500 on non-ASCII credentials. **PR3:** extract the *stateless* surface (`_cors_headers`, `_unauthorized_response`, `_stats_*`, config/stats/health/root/cors-preflight handlers) from `WebRTCRoutes` into a shared unit (e.g. `server/_webrtc_handlers.py`) parameterized by `(config, AuthPolicy|None, stats, ...)`; `WebRTCTransport` imports it lazily inside `connect()`. **Do not delete `_handle_offer`** (genuinely per-peer negotiation, semantically different between singleton and multi-session modes) and **do not mount `WebRTCRoutes` wholesale** (its constructor needs `config_factory`/`gate`/`manager` the singleton transport shouldn't own). Expected shrink: ~250-400 lines.
- **Why:** The highest-severity duplication finding — ~400 lines of signaling HTTP surface exist twice, kept "byte-identical" by docstring promise only, with no test enforcing it.
- **Validate:** `just guard-contracts` + `tests/transports/test_transport_conformance.py` per PR, plus `tests/server/test_webrtc_routes.py` and the `tests/transports/test_webrtc_{config,stats_artifacts,auth_browser_playground}.py` regression net.

### QS7. *(Covered above)* — `webtransport.py` (1660), `langgraph.py` (1579), `session/_session.py` (1504) remain on the ratchet allowlist as future split candidates; no confirmed proposal targets them yet, so leave them to be driven down by QS1's shrink-only discipline.

---

## Phase 3 — Process / automation (set-and-forget)

### QP1. *(Merged)* — SHA-pinning + Dependabot ecosystems + zizmor + pre-commit-in-CI + lockfile enforcement + concurrency are all in Phase 1 (QW4, QW6, QW7). They are "process" in spirit but land as quick edits.

### QP2. Tag-triggered publish via PyPI Trusted Publishing
- **What:** Add `.github/workflows/release.yml` triggered on version tags: job 1 reuses the release gate (add a `workflow_call:` trigger to `release-validation.yml` — no reusable workflow exists today — or duplicate the gate), job 2 builds sdist/wheel via `uv build` and uploads as artifact, job 3 downloads and publishes with `pypa/gh-action-pypi-publish` **pinned to a full commit SHA** (matching docs.yml's convention for privileged OIDC jobs), under `environment: pypi` with required reviewers and `permissions: id-token: write` **only**. Register the Trusted Publisher on PyPI; do a TestPyPI dry-run first. Extend `tests/test_ci_workflow.py` to guard the tag trigger, `environment: pypi`, `id-token: write`, the SHA-pinned publisher, and **absence** of any long-lived `PYPI_*` token secret.
- **Why:** Trusted Publishing (OIDC) + default PEP 740 attestations is the 2026 baseline (long-lived tokens deprecating); today `release-validation.yml` builds and twine-checks wheels then dead-ends as a 30-day artifact — an actual publish would run from a dev machine outside every gate the repo built.
- **Ordering:** **→ QW5 (LICENSE)** must land first (can't publish an unlicensed wheel). PyPI registration + a real tag are out-of-band human steps.
- **Validate:** `uv run pytest tests/test_ci_workflow.py`; TestPyPI dry-run.

---

## Cross-cutting ordering summary

- **QW5 (LICENSE) → QP2 (publish).**
- **QW6 (SHA-pin, `persist-credentials: false`) → QW7 (pre-commit/zizmor in CI)** so zizmor's first run is clean; do QW7's ruff alignment same-PR-or-before its CI job.
- **QW4 & QW6 both touch `tests/test_ci_workflow.py`** — sequence them or reconcile the shared file; QW6 also touches `plan/validation/tasks.md`.
- **QW10 (`_net.py`) → QS4 forbidden-contract ignore-list shrink.**
- **QS1 (ratchet) first**, then every split (QS2, QS3, QS5, QS6) shrinks its allowlist entry.
- **QS4 Phase 2 (layers) → after QS2/QS3/QS6** (import edges settle).
- Independent / parallelizable: QW1, QW2, QW3, QW8, QW9, QW11.

---

## Appendix — Explicitly rejected (do not re-litigate)

1. **Shared `tests/_fakes/` package with Protocol-conformance assertions + migrate 21 `FakeSession`s.** The conformance mechanism already ships (`src/easycat/testing/contracts.py` with per-protocol tests, guarded by `just guard-contracts`). `Session` is a concrete class, not a `runtime_checkable` Protocol, so the proposed canonical-fake `isinstance` test cannot exist. The 21 `_FakeSession` classes fake **disjoint** role slices (start/stop, private attrs, turn methods) — one canonical fake would be a god-object or an empty base with no drift protection, and drift already surfaces as `AttributeError` in consumer tests. Only real duplication: five ~4-line `_pcm16_bytes` helpers — a 30-minute fold, not a package.

2. **Extract shared LangChain message accessors into `_lc_messages.py`.** Real part is trivial (~12 identical lines of `_content_of`/`_set_content`), and a shared home already exists (`_helpers.py`). The headline "shared `rewrite_last_ai_message`" is infeasible: the two rewrite functions operate on fundamentally different persistence models (in-memory shadow list + `RunnableWithMessageHistory` vs. LangGraph checkpoint `get_state`/`update_state`) and share only an innermost loop — unifying needs a leaky predicate+persistence abstraction the proposal itself concedes.

3. **Extract the 532-line `_DOCS_LINKS` map out of `cli/_app.py`.** `_app.py` (996 lines) isn't among the large modules; the block is a single grep-able list literal; its location **is** documented (`scripts/regen_llms_txt.py`). Real importer footprint is 11 files pulling a coupled helper cluster, so a pure code-move delivers no correctness benefit and invites merge friction against constant concurrent `codex/*` merges.

4. **Full deprecation machinery / written removal policy across *all* aliases (the maximalist version).** PEP 702 can't decorate the module-level `_PROVIDERS` dict or the `settings` dataclass field, so the "every alias carries the decorator" guard is unsatisfiable for those targets; an invented "removal 0.3" policy contradicts CLAUDE.md's deliberate "kept alias" wording. *(Note: the scoped, feasible subset — decorate the three `Session` methods + settings-fold warnings — is accepted as **QW8**.)*

5. **`griffe check` signature-level API-break detection in CI.** `git tag -l` returns zero tags and `griffe check` diffs against the last release tag — permanent no-op until a first release, and it fights the deliberate pre-1.0 API churn the test suite already documents (`test_culled_symbols_remain_available`, snapshot cap `<= 95`).

6. **towncrier changelog with a PR-fragment gate.** No tags, no PyPI publish step, no adopters, and no existing CHANGELOG to conflict — the headline "avoid CHANGELOG merge conflicts" benefit is moot. High per-PR friction on a stream dominated by tiny agent-authored hardening PRs, for near-zero reader value at 0.1.0.

7. **Convert the frozen advisory gates into enforced ratchets (mypy error-count baseline, diff-cover `fail-under`).** The mypy count is currently **non-deterministic** — the CI command dies on a numpy stub (`python_version` 3.11 config vs 3.12 stubs); forcing 3.12 yields 135 errors, not the "~72" in hand comments. A count baseline can't be pinned without first fixing the interpreter/stub mismatch, and dep bumps would keep re-destabilizing it. The repo already has a **superior** enforced ratchet (gated clean-core `mypy_gated_paths`). diff-cover `--fail-under` would false-block provider/transport/telephony PRs whose tests are integration-marked and excluded from the coverage slice. Only the trivial comment cleanup (remove the phantom `--fail-under` reference) is justified.

8. **Import Linter *layers* + *independence(all)* + *forbidden* as originally specified.** Two of three contracts contradict repo reality: `forbidden server/cli→webrtc` fails on the legitimate module-level import at `server/webrtc_routes.py:72`; `independence` including transports/telephony fails on real bidirectional coupling (`twilio_media.py`↔`telephony.dtmf`). *(Note: the feasible subset — independence of `stt`/`tts`/`vad` + forbidden-with-ignores + deferred layers — is accepted as **QS4**.)*