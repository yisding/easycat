# Maintainability — Phase 1 Quick Wins (QW1–QW11)

Hours-each changes with high impact-per-effort, landable against the existing test suite as the
safety net. Most are independent; the CI/tooling batch (QW4/QW6/QW7) must be sequenced because it
touches `tests/test_ci_workflow.py` in lockstep. See [`../SEQUENCING.md`](../SEQUENCING.md) for the
collision table.

| ID | Item | Effort | Impact | Depends on |
|----|------|--------|--------|------------|
| QW1 | Fix `TYPE_CHECKING` re-export drift in `__init__.py` + parity guard | <1h | Med | none |
| QW2 | Repair out-of-sync `all` extra + union guard | 1h | Med | none |
| QW3 | Delete the fake `integration-live` CI job | <1h | Low | none |
| QW4 | Enforce `uv.lock` in CI, cache uv, add concurrency group | 1–2h | High | (test_ci_workflow with QW6) |
| QW5 | Add LICENSE (BSD-2-Clause) + PEP 639 metadata | <1h | High | none · blocks QP2 |
| QW6 | SHA-pin workflows + expand Dependabot + zizmor | half-day | High | before QW7 |
| QW7 | Run pre-commit in CI + align ruff hook version | 1–2h | Med | QW6 |
| QW8 | PEP 702 `@deprecated` on compat aliases + guard | half-day | Med | none |
| QW9 | Enable ruff `ASYNC`, `B`, `RUF006` with ratchet | ~1 day | High | none |
| QW10 | Move loopback/auth helpers into leaf `_net.py` | 2–3h | Med | blocks QS4 |
| QW11 | Config-parity guard tests (justfile/mypy path list) | 1–2h | Low | none |

---

## QW1 — Fix the `TYPE_CHECKING` re-export drift in `__init__.py`

- **Category / Effort / Impact:** typing correctness / <1h / Medium
- **Files:** `src/easycat/__init__.py` (the `LAZY_EXPORTS` registry and the `if TYPE_CHECKING:`
  block), `tests/test_public_api.py`
- **Depends on / Blocks:** none

**Problem.** Three names registered in `LAZY_EXPORTS` (`create_vad`, `SupervisorListenerAttached`,
`SupervisorListenerDetached`) are absent from the static `if TYPE_CHECKING:` import block. Because
the PEP 562 `__getattr__` is unannotated (returns `Any`), type checkers silently resolve those
three to `Any` for consumers of this `py.typed` package — a shipped typing regression.

**Implementation steps.**
1. In `src/easycat/__init__.py`, extend the existing `TYPE_CHECKING` imports (do not add new
   statements): change the `easycat.vad` import to include `create_vad` (alongside `VADConfig`),
   and add `SupervisorListenerAttached` / `SupervisorListenerDetached` to the `easycat.events`
   import line.
2. In `tests/test_public_api.py`, add a test that AST-parses the `if TYPE_CHECKING:` node and
   asserts `(module, name)` parity with the `LAZY_EXPORTS` registry. Reuse the file's existing
   `ast` helpers.

**Lockstep edits (same PR).** The new parity test in `tests/test_public_api.py`.

**Validation.** `just guard-docs` (runs `tests/test_public_api.py`).

**Risk & rollback.** Near-zero — additive imports guarded by `TYPE_CHECKING` (no runtime cost).
Revert is a one-line change.

---

## QW2 — Repair the out-of-sync `all` extra

- **Category / Effort / Impact:** packaging correctness / 1h / Medium
- **Files:** `pyproject.toml` (`[project.optional-dependencies].all` + `[tool.uv].conflicts`),
  `tests/test_dependency_policy.py`, `uv.lock`
- **Depends on / Blocks:** none

**Problem.** `pip install easycat[all]` cannot dispatch the LangChain/LangGraph bridges: the `all`
extra omits `langchain-core` and `langgraph`, yet those bridges runtime-import the packages. A
silent packaging break.

**Implementation steps.**
1. Add `langchain-core>=1.3.3` and `langgraph>=1.2.6` to the `all` list in `pyproject.toml`, with a
   comment documenting the **three** deliberately-excluded extras and why: `ten-vad`
   (non-permissive license), `pydantic-ai` and `pydantic-ai-v2-beta` (mutually exclusive via
   `[tool.uv].conflicts`).
2. Add a test in `tests/test_dependency_policy.py`: assert
   `set(extras["all"]) == union(all extras − exclusion-keys − "all")`, **and** that every exclusion
   key is itself a declared extra (stale-exclusion guard, mirroring `scripts/extras_matrix.py`).
   Compare **raw requirement strings** so the test doubles as a pin-drift guard.
3. Run `uv lock` after the edit and commit the lockfile delta.

**Lockstep edits (same PR).** The union guard test; the regenerated `uv.lock`.

**Validation.** `uv run pytest tests/test_dependency_policy.py`; the nightly extras matrix already
install-tests `all`.

**Risk & rollback.** Low. If the union assertion is too strict for a legitimate future exclusion,
extend the documented exclusion set rather than weakening the test.

---

## QW3 — Delete the fake `integration-live` CI job

- **Category / Effort / Impact:** CI hygiene / <1h / Low
- **Files:** `.github/workflows/ci.yml` (~lines 177–188), `tests/test_ci_workflow.py`,
  `plan/validation/reference.md:579`
- **Depends on / Blocks:** none

**Problem.** ci.yml carries a `workflow_dispatch`-gated `pytest -m integration_live` job with no
secrets wired — it self-skips permanently, a green check that tests nothing. The real secret-gated
live lane already exists in `nightly-validation.yml` (`live-canaries`, `environment:
live-validation`, `ref_protected`).

**Implementation steps.**
1. Remove the `integration-live` job block from `ci.yml`.
2. Add an assertion to `tests/test_ci_workflow.py`: `"integration-live" not in workflow["jobs"]`,
   with a comment pointing to nightly's `live-canaries` as the real path.
3. Update `plan/validation/reference.md:579` ("CI Shape"), which still lists this job.

**Do NOT** instead wire secrets into ci.yml — that job is dispatchable on any branch and would be a
security regression versus nightly's protected environment.

**Lockstep edits (same PR).** The test assertion; the reference doc.

**Validation.** `uv run pytest tests/test_ci_workflow.py`.

**Risk & rollback.** Low.

---

## QW4 — Enforce `uv.lock` in CI, cache uv, add a concurrency group

- **Category / Effort / Impact:** supply-chain + CI cost / 1–2h / High
- **Files:** `.github/workflows/ci.yml`, `nightly-validation.yml`, `release-validation.yml`;
  `tests/test_ci_workflow.py` (asserts at ~lines 78, 557, 597)
- **Depends on / Blocks:** shares `tests/test_ci_workflow.py` with QW6 — **sequence QW4 → QW6**

**Problem.** Bare `uv sync` silently re-resolves on drift, defeating the committed lockfile as a
supply-chain control. No dependency caching across a 4-version matrix; no PR-run cancellation.

**Implementation steps.**
1. Change every `uv sync --group dev` to `uv sync --locked --group dev` across the three workflows.
2. Add `enable-cache: true` + `cache-dependency-glob: "**/uv.lock"` to every `astral-sh/setup-uv`
   step (works on the already-pinned `@v4` — **no version bump**).
3. Add to `ci.yml` top-level:
   ```yaml
   concurrency:
     group: ci-${{ github.ref }}
     cancel-in-progress: ${{ github.event_name == 'pull_request' }}
   ```
4. Skip `required-version` in `[tool.uv]` — recurring version-churn friction for little gain over
   `--locked`.

**Lockstep edits (same PR).** Update the three exact-substring asserts in `tests/test_ci_workflow.py`
(~78, ~557, ~597) that break when `--locked` is inserted; add a blanket assert that every `uv sync`
carries `--locked`, and a concurrency-shape assert.

**Validation.** Run `uv lock --check` locally first (it passes today, so `--locked` won't red CI);
then `uv run pytest tests/test_ci_workflow.py`.

**Risk & rollback.** Low; `uv lock --check` currently clean.

---

## QW5 — Add a LICENSE file and PEP 639 metadata

- **Category / Effort / Impact:** legal/packaging / <1h / High
- **Files:** new `/LICENSE`, `pyproject.toml` `[project]`, `tests/test_dependency_policy.py`
- **Depends on / Blocks:** none · **blocks QP2 (publishing)**

**Problem.** No license grant exists, yet CI build-smoke twine-checks distributable wheels. The
package is legally unusable and unpublishable downstream.

**Decision (LOCKED — not an open question):** **BSD-2-Clause**, `Copyright (c) 2026 Yi Ding`.

**Implementation steps.**
1. Create repo-root `LICENSE` containing the standard 2-Clause BSD ("Simplified") license text with
   the header line `Copyright (c) 2026 Yi Ding`.
2. In `pyproject.toml [project]`, add `license = "BSD-2-Clause"` and
   `license-files = ["LICENSE"]`. Do **not** add a `License ::` trove classifier (PEP 639 forbids
   mixing the SPDX-expression and classifier forms).
3. Add a one-line assert to `tests/test_dependency_policy.py` using the existing `_pyproject()`
   helper: `"license" in _pyproject()["project"]` (optionally also assert `LICENSE` exists at root).

**Lockstep edits (same PR).** The metadata test.

**Validation.** `uv build && uvx twine check dist/*` (the existing build-smoke lane).

**Risk & rollback.** None of consequence — additive.

---

## QW6 — SHA-pin the tag-pinned workflows + expand Dependabot + add zizmor

- **Category / Effort / Impact:** supply-chain security / half-day / High
- **Files:** `ci.yml`, `nightly-validation.yml`, `release-validation.yml`, `.github/dependabot.yml`,
  `.pre-commit-config.yaml`; `tests/test_ci_workflow.py` (~9 sites, exact-equality at ~510),
  `plan/validation/tasks.md`
- **Depends on / Blocks:** shares `tests/test_ci_workflow.py` with QW4 (sequence QW4 → QW6); **→ QW7**

**Problem.** The three secret-bearing workflows reference actions by mutable tag
(`actions/checkout@v4`, `astral-sh/setup-uv@v4`, `extractions/setup-just@v3`,
`actions/upload-artifact@v4`). Post the tj-actions/changed-files incident, SHA-pinning +
Dependabot-managed pins + zizmor are the 2026 baseline — and these are exactly the live/release lanes.

**Implementation steps.**
1. Replace every mutable tag ref with a 40-char commit SHA + trailing `# vX.Y.Z` comment. **Pin at
   the current tags' SHAs — do not fold in the setup-uv v4→v8 major bump**; let the new
   github-actions Dependabot ecosystem propose that as a reviewable PR.
2. Add `persist-credentials: false` to all `actions/checkout` steps (docs.yml already does this).
3. Append `github-actions` (unambiguous) and best-effort `pip` (for `requirements-docs.txt`;
   verify its first PR regenerates hashes sanely, else drop it) ecosystems to `.github/dependabot.yml`.
4. Add the zizmor pre-commit hook to `.pre-commit-config.yaml` and a `uvx zizmor .github/workflows`
   step to ci.yml's lint job.

**Lockstep edits (same PR).** `tests/test_ci_workflow.py` hardcodes `"actions/upload-artifact@v4"`
in ~9 places (incl. an exact-equality at ~510) — switch literals to
`startswith("actions/upload-artifact@")` or the pinned SHA. Update `plan/validation/tasks.md` which
references the token. This is the bulk of the work.

**Validation.** `uv run pytest tests/test_ci_workflow.py`; `uvx zizmor .github/workflows` (it will
flag the very tags being pinned — self-consistent).

**Risk & rollback.** Medium mechanical surface; revert is per-file. Verify each pinned SHA resolves
to the intended tag before committing.

---

## QW7 — Run pre-commit in CI and align the ruff hook version

- **Category / Effort / Impact:** CI/consistency / 1–2h / Medium
- **Files:** `ci.yml` (new job), `.pre-commit-config.yaml`
- **Depends on / Blocks:** **QW6** (needs `persist-credentials: false` for a clean zizmor run); do
  the ruff alignment same-PR-or-before its CI job

**Problem.** No workflow runs pre-commit, so actionlint/codespell/check-yaml enforce only for
contributors with local hooks installed. Worse, `.pre-commit-config.yaml` pins ruff-pre-commit
`v0.15.8` while `uv.lock` locks ruff `0.15.17`, letting local formatting fight CI.

**Implementation steps.**
1. Add a `pre-commit` job to ci.yml: `uv sync --group dev` (locked, per QW4) then
   `just pre-commit` (or `uv run pre-commit run --all-files` — **not** `uvx`, since pre-commit is a
   locked dev dep). Cache `~/.cache/pre-commit`.
2. Fix the ruff skew: convert the ruff hooks in `.pre-commit-config.yaml` to a `repo: local` hook
   shelling to `uv run ruff check` / `uv run ruff format`, so exactly one ruff version exists (and
   it rides existing uv Dependabot coverage).
3. Optionally set `SKIP=ruff-check,ruff-format` in the CI pre-commit job to avoid duplicating the
   dedicated lint job.

**Lockstep edits (same PR).** The ruff-hook conversion must land first-or-together, else the new
job's ruff disagrees with the lint job.

**Validation.** `just check` locally; the new CI job itself.

**Risk & rollback.** Low.

---

## QW8 — PEP 702 `@deprecated` on the compatibility aliases

- **Category / Effort / Impact:** API discipline / half-day / Medium
- **Files:** `pyproject.toml` (dep), `session/_session.py`, `stt/factory.py`, `tts/factory.py`,
  `tests/test_deprecations.py` (new), `docs/public-api.md`; migration of ~15 tests
- **Depends on / Blocks:** none (but touches `session/_session.py` — sequence after #32; see
  SEQUENCING)

**Problem.** The compatibility aliases carry zero machine-visible (type-checker/IDE) deprecation
signal. Establishing that at 0.1.0 is cheap and matches the repo's guard-test investment.

**Implementation steps.**
1. Add `typing-extensions>=4.5` to `[project.dependencies]` (currently only transitively locked).
2. Decorate `Session.shutdown` / `close` / `destroy` with `@deprecated(...)` (from
   `typing_extensions`).
3. Emit `DeprecationWarning` in the `settings`-fold branches of `stt/factory.py` and
   `tts/factory.py`.
4. **Scope:** drop the `_PROVIDERS` piece — PEP 702 can't decorate a module attribute and it's
   underscore-private (see `rejected.md` item A.4 for the maximalist version that was killed).
5. Add `tests/test_deprecations.py` asserting each alias raises via `pytest.warns`.
6. Add a two-sentence removal policy to `docs/public-api.md` (guard its heading in
   `tests/test_public_api.py`).

**Lockstep edits (same PR).** Migrate the ~15 tests calling `await session.shutdown()` to
`stop(force=True)`; update the guard-tested "thin alias" wording in AGENTS.md/CLAUDE.md
(`tests/test_dx_helpers.py:535-543`) and the Session Lifecycle route contract
(`tests/docs/test_route_contracts.py:175-189`); wrap/migrate `settings=`-using factory tests.
`__aexit__` already calls `stop(force=True)`, so the async-with path is unaffected.

**Validation.** `uv run pytest tests/test_deprecations.py`; `just guard-docs`.

**Risk & rollback.** Low; behavior-preserving (warnings only).

---

## QW9 — Enable ruff `ASYNC`, `B`, `RUF006` with the grandfather-list ratchet

- **Category / Effort / Impact:** static-analysis / ~1 day / High
- **Files:** `pyproject.toml [tool.ruff.lint]`; `CLAUDE.md:131`, `justfile:38` (stale rule-list docs)
- **Depends on / Blocks:** none

**Problem.** For an async-first realtime audio framework, `flake8-async` catches exactly the
blocking-call defects that become audible latency — and `ASYNC230`/`ASYNC240` blocking-I/O-in-async
hits already exist in `transports/webrtc.py` and `server/webrtc_routes.py` (these overlap
code-quality findings #17/#40). The repo already runs the proven "fix a file → delete its ignore
line" ratchet for C901.

**Implementation steps.**
1. Extend `select` in `[tool.ruff.lint]` with `ASYNC`, `B`, `RUF006`.
2. Add `[tool.ruff.lint.flake8-bugbear] extend-immutable-calls = ["typer.Option", "typer.Argument"]`
   to kill all 19 B008 Typer false positives cleanly — **do not grandfather them**.
3. Put `ASYNC109` in a **permanent** `ignore` (explicit timeout params are deliberate public API
   here, incl. `timeouts.py`).
4. Run `ruff check --fix` for the ~14 safe autofixes; seed a **new, never-grow** per-file-ignores
   block for the ~57 remainder. `RUF006` and `B023` have zero current hits (free forward guards).
5. **Fix first, not noqa:** the `ASYNC230`/`ASYNC240` hits in `webrtc.py` / `webrtc_routes.py` are
   genuine event-loop-stall bugs — coordinate with code-quality #16/#17/#40.
6. Fix the doc drift: `CLAUDE.md:131` and `justfile:38` still claim "E, F, I, W, UP" (omitting even
   the already-enabled C901/PLR rules).

**Lockstep edits (same PR).** The per-file-ignores block; the CLAUDE.md/justfile rule-list text.

**Validation.** `uv run ruff check .`; `just check`.

**Risk & rollback.** Medium — large mechanical surface. The ratchet makes it incremental; land the
config + autofixes + ignore-seed in one PR, then draw the list down file-by-file in follow-ups.

---

## QW10 — Move loopback/auth helpers into a leaf `_net.py`

- **Category / Effort / Impact:** import-graph hygiene / 2–3h / Medium
- **Files:** new `src/easycat/_net.py`; call sites in `transports/webrtc.py`, `transports/websocket.py`,
  `server/auth.py`, `cli/serve.py`, `debugger/server.py`, `voice_app.py:729-730`,
  `server/webrtc_routes.py:77`; stale prose in `server/auth.py:31`, `server/__init__.py:45`,
  `server/config.py:20`, `voice_app.py:724`
- **Depends on / Blocks:** **blocks QS4** (shrinks the forbidden-contract ignore list)

**Problem.** The canonical `is_loopback_host` / `normalize_auth_token` helpers live in a 1638-line
optional-dependency module, forcing three documented lazy-import cycle-workarounds. That's how
cycles creep in.

**Implementation steps.**
1. Create `src/easycat/_net.py` with **zero intra-package imports**: `is_loopback_host(host: str |
   None) -> bool` (return False for None; keep webrtc's `strip().strip("[]").lower()` normalization)
   and `normalize_auth_token`. The two `_normalize_auth_token` copies are behaviorally identical —
   **no body reconciliation needed**; carry the richer websocket docstring.
2. Update all call sites to import downward from `easycat._net`.
3. Delete the three cycle-workaround lazy imports and debugger's private `_hostname_is_loopback`.
4. Update the now-stale prose narrating the workaround at the four cited locations.
5. Optionally gate `_net.py` in `mypy_gated_paths` (`justfile:58` **and** the
   `[[tool.mypy.overrides]]` list in `pyproject.toml` — keep them in sync).

**Lockstep edits (same PR).** All call sites move together (deleting the old private copies);
the stale prose.

**Validation.** `uv run pytest tests/transports tests/server`; `just typecheck` if gated.

**Risk & rollback.** Low-medium; behavior-preserving. The risk is missing a call site — grep for
both helper names before deleting the originals.

---

## QW11 — Additional config-parity guard tests

- **Category / Effort / Impact:** guard-test coverage / 1–2h / Low
- **Files:** `tests/cli/test_validate_runner.py`, `tests/test_dependency_policy.py`
- **Depends on / Blocks:** none

**Problem.** Two hand-synced config pairs currently have no guard, against the repo's own "derive
expectations from live code" philosophy.

**Implementation steps.**
1. In `tests/cli/test_validate_runner.py`: parse the `test-fast`/`cov` recipes via
   `scripts/_justfile.py` and assert their `-m` expression equals `VALIDATION_SELECTORS["quick"]`
   (`validation/runner.py:62`). Today only ci.yml's copy is guarded.
2. In `tests/test_dependency_policy.py`: assert `justfile:58 mypy_gated_paths` maps 1:1 to the
   `pyproject.toml [[tool.mypy.overrides]]` module globs (coupled only by a comment today).

**Lockstep edits (same PR).** None beyond the new tests.

**Validation.** `uv run pytest tests/cli/test_validate_runner.py tests/test_dependency_policy.py`.

**Risk & rollback.** None — test-only.
