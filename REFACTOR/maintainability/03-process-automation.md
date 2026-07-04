# Phase 3 — Process / Automation

Implementation guide for the "set-and-forget" process work in the
[maintainability plan](../reports/maintainability-plan.md) (Phase 3). In
practice Phase 3 is **one** net-new deliverable — QP2, a tag-triggered PyPI
publish workflow — plus a pointer clarifying that everything else nominally
filed under "process" already landed as Phase 1 quick wins.

This is a planning document. It changes no source; it describes the change so
an implementer (human or agent) can execute it against the current repo state.

> **Note — QP1 is already covered by Phase 1.**
> The plan merges QP1 ("SHA-pinning + Dependabot ecosystems + zizmor +
> pre-commit-in-CI + lockfile enforcement + concurrency") into the quick wins.
> Do **not** re-plan or duplicate these here — they land as quick edits:
>
> | QP1 process item | Lands as | Quick-wins doc |
> | --- | --- | --- |
> | Lockfile enforcement (`uv sync --locked`), uv cache, `concurrency` group | **QW4** | see `01-quick-wins.md` |
> | SHA-pin workflows, expand Dependabot (`github-actions`, best-effort `pip`), add zizmor | **QW6** | see `01-quick-wins.md` |
> | Run pre-commit in CI, align ruff hook to the locked version | **QW7** | see `01-quick-wins.md` |
>
> QP2 below is the only Phase 3 item that is genuinely new work. It also
> *benefits* from QW6 landing first: the SHA-pin convention QW6 establishes is
> exactly what QP2's privileged publish job must follow.

---

## QP2 — Tag-triggered publish via PyPI Trusted Publishing

**Effort:** Small–Medium (one new workflow file + guard tests; the bulk of the
calendar time is out-of-band human setup on PyPI, not code).
**Impact:** High — turns "publish from a dev machine outside every gate" into a
gated, attestable, tokenless release.
**Depends on:** **QW5 (LICENSE) must land first.** The LICENSE decision is
**LOCKED: BSD-2-Clause, Copyright (c) 2026 Yi Ding.** A wheel cannot be
published to PyPI without a license grant in its metadata (PEP 639
`license = "BSD-2-Clause"` + `license-files = ["LICENSE"]`), so QP2 is blocked
until QW5's `/LICENSE` and `pyproject.toml` metadata exist.

### Problem

Today there is **no publish workflow** at all. `ls .github/workflows` shows only
`ci.yml`, `docs.yml`, `nightly-validation.yml`, and `release-validation.yml` —
none of them ships anything.

`release-validation.yml` is the closest thing to a release path, and it
**dead-ends**:

- It is `workflow_dispatch`-only (manual, no tag trigger).
- It runs the real gate: `uv build --sdist --wheel`, `uvx twine check dist/*`,
  a clean out-of-tree wheel install, smoke tests, and strict live/latency
  validation.
- Its final step (`Upload release validation artifacts`) uploads
  `dist/**` as a GitHub artifact with **`retention-days: 30`** — and stops.

So the built, validated, twine-checked wheel expires as a 30-day artifact and
is never published. An actual release today would mean a maintainer running
`uv build` + `twine upload` from a laptop — **outside every gate the repo built**
(the clean-install check, the live validation, the skip auditing), and using a
long-lived PyPI API token.

**Why OIDC / PEP 740 is the 2026 baseline.** PyPI Trusted Publishing lets a
GitHub Actions job mint a short-lived OIDC token scoped to a specific
repo/workflow/environment, exchanged at upload time — no long-lived `PYPI_*`
secret stored in the repo. Combined with default PEP 740 attestations
(publish-time provenance signed via the same OIDC identity), this is the
current baseline for library publishing; long-lived upload tokens are being
deprecated. `git tag -l` returns **zero tags**, so this is a **greenfield first
publish** — there is no prior release to be backward-compatible with, and no
existing token workflow to migrate. (This same "no tags yet" fact is why the
plan rejected `griffe check` API-diffing and towncrier changelog gating: both
diff against a last release tag that does not exist.)

### Implementation steps

Add a new `.github/workflows/release.yml` with a **three-job graph**:
`validate → build → publish`. Only the final job is privileged.

1. **Trigger on version tags.**
   ```yaml
   on:
     push:
       tags:
         - "v*"
   permissions:
     contents: read   # minimal default; publish job overrides narrowly
   ```

2. **Job 1 — `validate` (reuse the existing release gate).**
   The gate already lives in `release-validation.yml`. Prefer making it
   reusable rather than duplicating it: add a `workflow_call:` trigger to
   `release-validation.yml` (it currently exposes only `workflow_dispatch:`),
   then `uses: ./.github/workflows/release-validation.yml` with
   `secrets: inherit` from job 1 here. If wiring `workflow_call` proves
   awkward (the gate reads several `secrets.*` provider keys and an
   `environment: release-validation`), the fallback is to duplicate the
   build+twine-check steps — but reuse is preferred so the gate cannot drift.

3. **Job 2 — `build` (`needs: validate`).**
   `uv build --sdist --wheel`, then `actions/upload-artifact` (SHA-pinned, per
   QW6) the `dist/**` as a short-lived artifact handed to job 3. This keeps
   the build unprivileged: no `id-token` here.

4. **Job 3 — `publish` (`needs: build`) — the only privileged job.**
   - `environment: pypi` with **required reviewers** configured on the GitHub
     environment (a human approval gate before the OIDC token is ever minted).
   - `permissions: id-token: write` **only** (do not also grant
     `contents: write` etc.; the narrower the OIDC token scope, the better).
   - Download the `dist/**` artifact from job 2 (SHA-pinned
     `actions/download-artifact`).
   - Publish with **`pypa/gh-action-pypi-publish` pinned to a full 40-char
     commit SHA** with a trailing `# vX.Y.Z` comment — **mirror the convention
     `docs.yml` already uses** for its privileged OIDC jobs, e.g.:
     ```yaml
     # actions/deploy-pages v4.0.5
     - uses: actions/deploy-pages@d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e
     ```
     Those `docs.yml` `configure-pages`/`deploy-pages` jobs are the existing
     in-repo precedent for a privileged `id-token: write` action pinned by SHA;
     QP2's publish step must look the same. Do **not** use a mutable
     `@release/v1` tag for the publisher.
   - Do not pass a `password:`/token input — Trusted Publishing supplies the
     credential via OIDC. Leave PEP 740 attestations at their default (enabled).

5. **TestPyPI dry-run first.** Before the first real PyPI publish, run the
   publish job against **TestPyPI** (`repository-url:
   https://test.pypi.org/legacy/`) with a Trusted Publisher registered on
   TestPyPI, to prove the OIDC exchange and metadata (including the new
   BSD-2-Clause license) work end-to-end. Then remove/flip the `repository-url`
   for production.

6. **Register the Trusted Publisher on PyPI (out-of-band, human).** In the
   PyPI project settings, add a GitHub Actions Trusted Publisher pinned to this
   repo, `release.yml`, and the `pypi` environment name. This is a human,
   out-of-band step and cannot be done from CI — see the checklist below.

### Lockstep edits

Extend `tests/test_ci_workflow.py` to guard the new workflow's security shape.
Add assertions that:

- `release.yml` is triggered on version **tags** (`on.push.tags` includes
  `v*`).
- the publish job declares **`environment: pypi`**.
- the publish job's `permissions` includes **`id-token: write`** and does not
  broaden beyond what publishing needs.
- the publisher step uses a **SHA-pinned** `pypa/gh-action-pypi-publish`
  (assert the ref is a 40-char hex SHA, not a `@vN` tag — matching how QW6
  guards the other pinned actions).
- **no long-lived `PYPI_*` token secret** is referenced anywhere in
  `release.yml` (assert `PYPI_` / `secrets.PYPI` does not appear in the publish
  job) — the whole point of Trusted Publishing is that no such secret exists.

These mirror the repo's existing "derive expectations from the live workflow"
guard-test discipline already used throughout `tests/test_ci_workflow.py`.

### Validation

```bash
uv run pytest tests/test_ci_workflow.py
```

Plus a live **TestPyPI dry-run** of the publish job (step 5 above) before the
first production tag. Locally, the build half is already exercised by the
existing build-smoke lane:

```bash
uv build && uvx twine check dist/*
```

which must show the BSD-2-Clause license in the wheel metadata once QW5 has
landed.

### Risk & rollback

- **Risk — publishing an unlicensed or wrong-metadata wheel.** Mitigated by the
  QW5 dependency (LICENSE + PEP 639 metadata) landing first and by the TestPyPI
  dry-run confirming metadata before production.
- **Risk — an unintended tag triggers a real publish.** Mitigated by the
  `environment: pypi` **required-reviewer** approval gate: the OIDC token is not
  minted until a human approves the deployment.
- **Risk — the pinned publisher SHA goes stale.** Acceptable and intended: the
  `github-actions` Dependabot ecosystem added in QW6 will propose bumps as
  reviewable PRs, same as every other pinned action.
- **Rollback.** QP2 is a purely additive new file plus test assertions. To roll
  back, delete `release.yml` (and revert the `workflow_call:` addition to
  `release-validation.yml` if that route was taken) and drop the new
  `tests/test_ci_workflow.py` assertions. Because the first publish is
  greenfield (no existing tags/releases), reverting before the first tag leaves
  no published artifact to un-ship. Removing the Trusted Publisher registration
  on PyPI is a separate out-of-band step.

---

## Out-of-band human steps

These cannot be done from CI and must be performed by the repo owner. Flagged
so the implementing agent does not attempt them in code.

- [x] **License sign-off — DONE / LOCKED.** BSD-2-Clause, Copyright (c) 2026
      Yi Ding. This is the decision QW5 encodes; no further owner gate remains
      for the license choice itself.
- [ ] **PyPI account / project ownership.** Ensure the `easycat` project name is
      claimed on PyPI (and on TestPyPI for the dry-run) by the owner account.
- [ ] **Register the Trusted Publisher (TestPyPI first, then PyPI).** Add a
      GitHub Actions Trusted Publisher scoped to this repo + `release.yml` +
      `pypi` environment. Do TestPyPI first for the dry-run, then PyPI.
- [ ] **Create the `pypi` GitHub environment with required reviewers** so the
      publish job's approval gate is real.
- [ ] **Cut the first version tag** (e.g. `v0.1.0`). `git tag -l` is currently
      empty; the first tag is what fires `release.yml`. Do the TestPyPI dry-run
      before the first production tag.
