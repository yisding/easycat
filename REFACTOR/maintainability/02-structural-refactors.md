# Phase 2 — Structural Refactors (QS1–QS7): Implementation Guide

This guide sequences the seven structural refactors from the maintainability plan's "Phase 2 — Structural refactors" section. These are multi-PR, incremental changes to the largest and hottest modules in `src/easycat/`. The **ground rule for every split** is non-negotiable: **one concern per PR**; keep the original file as a **facade that re-exports** every moved name (several tests and `cli/_app.py` import privates by name); and **move constants together with the functions that use them** — a re-export alias for a constant breaks `monkeypatch` targets, so the constant must physically live next to its consumers in the new module.

Line counts below were re-verified with `wc -l` against the working tree. **All 13 QS1 offenders matched the plan's stated counts exactly — no drift.** Paths use the `src/easycat/` prefix (the plan quotes package-relative paths; both refer to the same files).

| ID | Refactor | Est. PRs | Depends on |
|----|----------|----------|------------|
| QS1 | Module line-budget ratchet guard | 1 | — (do first, freezes growth) |
| QS2 | Split `cli/debug/bundles.py` (2445 lines) | 2–3 | QS1 |
| QS3 | Split `debugger/server.py` (3006 lines) | 5 | QS1 |
| QS4 | Import Linter contracts (Phase 1 + Phase 2) | 2 | QW10 (shrinks ignores); Phase 2 → QS2/QS3/QS6 |
| QS5 | Extract `LaneHarness` from `validation/runner.py` (1734 lines) | 4 | QS1 |
| QS6 | Finish M7 WebRTC convergence | 3 | QS1 |
| QS7 | Ratchet-driven residue (webtransport/langgraph/_session) | 0 | QS1 only |

Sequencing constraints carried from the plan's cross-cutting summary:
- **QS1 lands first**, then every split (QS2, QS3, QS5, QS6) shrinks its own allowlist entry as its final lockstep edit.
- **QW10 (`_net.py`)** must precede QS4's forbidden-contract so the ignore list is already smaller.
- **QS4 Phase 2 (the `layers` contract)** lands only **after** QS2/QS3/QS6, once import edges settle.

---

## QS1 — Module line-budget ratchet guard *(do first — freezes growth)*

**Effort / # of PRs:** 1 PR (a few hours).

**Files:**
- New: `tests/test_module_size_ratchet.py`.
- Reads (no edits): every `src/easycat/**/*.py`.

**Depends on / Blocks:** Depends on nothing. **Blocks nothing hard**, but is the anchor for QS2/QS3/QS5/QS6 — each of those shrinks the allowlist entry this test seeds, so land QS1 first to freeze growth.

**Problem.** 2026 practice operationalizes "keep files small for agents" as a CI ratchet: agent edit accuracy degrades sharply on 2–3k-line files, and the two hottest files are actively doubling — `debugger/server.py` grows ~+1574 lines/month and `cli/debug/bundles.py` ~+2135 lines/month. Without a guard, the splits below get re-inflated by concurrent `codex/*` PRs. This mirrors the proven C901 grandfather-list discipline already used in the repo.

**Split/refactor plan.** Add `tests/test_module_size_ratchet.py` that walks `src/easycat/**/*.py`, counts lines per file, and **fails any file >1000 lines unless it is in a checked-in `{path: line_count}` allowlist**. Seed the allowlist with **all 13** current offenders (not just the 7 biggest). Re-verified counts (working tree, `wc -l`):

| Path | Count |
|------|-------|
| `debugger/server.py` | 3006 |
| `cli/debug/bundles.py` | 2445 |
| `validation/runner.py` | 1734 |
| `transports/webtransport.py` | 1660 |
| `transports/webrtc.py` | 1638 |
| `integrations/agents/langgraph.py` | 1579 |
| `session/_session.py` | 1504 |
| `integrations/agents/llama_agents.py` | 1372 |
| `transports/twilio_media.py` | 1368 |
| `server/voice_server.py` | 1059 |
| `config/easy.py` | 1059 |
| `integrations/agents/pydantic_ai.py` | 1035 |
| `runtime/journal_sql.py` | 1029 |

Every entry matched the plan exactly; **no count required correction**.

Structural requirements:
- **Store the allowlist as a `{path: count}` dict.** Do not store a flat list of paths — the count is what lets the shrink rule fire.
- **Exclude `cli/scaffold/templates`** from the walk (generated scaffold sources are not hand-maintained modules).
- **Soften the shrink rule to a tolerance** to avoid churn on every refactor commit: fail on *growth* past the allowlisted count; when a file legitimately drops to ≤1000 lines, the rule is to **delete its entry** rather than track every intermediate shrink. (i.e., the dict may only shrink or lose entries; it may never grow a count or add a path.)
- **Header comment:** `"shrink this dict, never grow it"`, pointing readers at `docs/architecture.md` for the documented split seams.

**Facade & compatibility.** No production code changes; this is a pure guard test. The one hard compatibility constraint is with `tests/test_source_hygiene.py`, which fails on brittle `file.py:NNN` references anywhere in the tree.

**Lockstep edits (per PR).**
- **Critical:** never write `path:NNN` colon-form (e.g. `debugger/server.py:3006`) anywhere in this test — not in the dict, not in comments — because `tests/test_source_hygiene.py` flags `file.py:NNN` tokens. Keep path and count as separate dict key/value, and phrase comments to avoid the colon-number form.
- Confirm `tests/test_source_hygiene.py` still passes against the new file.

**Validation.**
```bash
uv run pytest tests/test_module_size_ratchet.py
uv run pytest tests/test_source_hygiene.py
just check
```

**Risk & rollback.** Very low risk — a test-only addition. Rollback is deleting the one file. The only failure mode is a false red if a colon-number token slips in (caught by `test_source_hygiene.py` in the same run) or if the walk picks up `cli/scaffold/templates` (caught immediately). Each split below then edits exactly one allowlist entry, so the ratchet and the refactors stay coupled.

---

## QS2 — Split `cli/debug/bundles.py` (2445 lines) *(lowest-risk split; 2–3 PRs)*

**Effort / # of PRs:** 2–3 PRs (mechanical). One-to-two commands per PR, ordered by churn.

**Files:**
- `src/easycat/cli/debug/bundles.py` — **2445 lines** (verified). Becomes a facade.
- New siblings under `src/easycat/cli/debug/`: `_common.py` (or `_summary.py`), then `export.py`, `replay.py`, `latency.py`, `diff.py`, `promote.py`, `grep.py`, `follow.py`.
- Importers to keep green: `src/easycat/cli/_app.py` (imports moved privates; the plan cites `cli/_app.py:948-976`) and four test files under `tests/cli/`.

**Depends on / Blocks:** Depends on QS1. Blocks nothing; independent of QS3/QS5/QS6. Feeds QS4 Phase 2 (import edges settle).

**Problem.** `bundles.py` is the second-hottest file (~42 commits/month) and is a pure aggregation of ~11 independent Typer commands. Verified structure confirms the seam: shared helpers `_load_bundle_or_journal` (line 776), `_print_wide` (line 745), and `_summarise_bundle` (line 188) are used across the per-command functions `list_bundles` (668), `show_bundle`/`inspect_bundle` (1096/1116), `export_bundle` (1139), `replay_bundle` (1294), `latency_command` (1524), `diff_command` (1658), plus promote/grep/follow further down. Each command is largely independent — a textbook mechanical split with high agent-maintainability payoff.

**Split/refactor plan.**
- **PR1 — extract the shared core first.** Move `_load_bundle_or_journal`, `_print_wide`, `_summarise_bundle` (and their tightly-coupled helpers such as `_annotations_tally`, `_add_annotations_row`, `_crash_dump_artifact_root`) into `cli/debug/_common.py` (the plan also names `_summary.py`; keep to one shared module). Nothing else moves yet. This proves the facade re-export path before any command moves.
- **PR2 — highest-churn commands.** Move `latency_command` (+ `_latency_percentiles`, `_latency_turn_table`, `_latency_percentile_table`) into `latency.py`, and `diff_command` (+ `_redact_diff_result`, `_diff_turn_filter`, `_diff_table`) into `diff.py`. The plan explicitly orders **latency and diff first**.
- **PR3 (and a possible PR4) — remaining commands.** `export.py` (`export_bundle`, `_default_export_path`, `_write_context_pack`, `_artifact_manifest`, `_prepare_output_dir`, `_readme_text`, `_timeline_markdown`, the context-record helpers), `replay.py` (`replay_bundle`, `_render_replay_summary`, `_replay_signature`), `promote.py` (`_promote_stub_test_name`, `_promote_test_stub`), `grep.py`, `follow.py` — one-to-two commands per PR ordered by churn.

**Facade & compatibility.**
- **Keep the explicit `bundles_app.command(...)(func)` registration block in `bundles.py`.** Do not move registration into the per-command modules. This preserves `--help` ordering exactly and eliminates the Typer-order risk the plan calls out. The command *bodies* live in the new modules; `bundles.py` imports them and registers them in the same order as today.
- **`bundles.py` stays a facade.** `cli/_app.py:948-976` and four `tests/cli/` files import moved privates by name (`_load_bundle_or_journal`, `_summarise_bundle`, `_print_wide`, etc.). Re-export every moved name from `bundles.py` via `__all__` so `from easycat.cli.debug.bundles import _foo` keeps resolving.
- Move any module-level constants **with** their functions (no aliasing back into `bundles.py`).

**Lockstep edits (per PR).**
- Update `__all__` in `bundles.py` to include the newly-moved names each PR.
- Verify `cli/_app.py` import block still resolves (it imports from `bundles`, which re-exports).
- The JSON-envelope contract test in `just guard-docs` must stay green — command output shape is unchanged, only file location moves.

**Validation.** Per PR:
```bash
just guard-ops     # runs tests/cli/test_bundles.py
just guard-docs    # JSON envelope contract for the CLI surface
```
Final PR also shrinks the QS1 allowlist entry for `cli/debug/bundles.py` (or deletes it if the facade drops ≤1000 lines).

**Risk & rollback.** Low. The Typer-registration-in-facade decision removes the one real risk (`--help` reordering). Each PR is self-contained and revertable. If a re-export is missed, the failure is an immediate `ImportError` in `cli/_app.py` or `tests/cli/`, caught by `just guard-ops`.

---

## QS3 — Split `debugger/server.py` (3006 lines) *(5 PRs, ordered)*

**Effort / # of PRs:** 5 PRs, one module per PR, in the order below.

**Files:**
- `src/easycat/debugger/server.py` — **3006 lines** (verified). Becomes a facade.
- New siblings under `src/easycat/debugger/`: `_records.py`, `_audio.py`, `_sources.py`, `_aec_routes.py`; plus a refactor of `_make_app`'s nested handlers into module-level functions (follow the existing `debugger/_aec.py` sibling pattern).
- Importers to keep green: ~30 `from easycat.debugger.server import _helper` sites across `tests/debugger/`.
- mypy gating: `justfile:58` `mypy_gated_paths` **and** the `[[tool.mypy.overrides]]` list in `pyproject.toml` (kept in sync — see QW11's parity guard).

**Depends on / Blocks:** Depends on QS1. Independent of QS2/QS5. Feeds QS4 Phase 2.

**Problem.** Largest and hottest module (~55 commits/month) with an ~886-line `_make_app` closure whose 29 nested handlers **cannot be imported or unit-tested** — the exact partial-read failure mode for agents. Verified structure confirms clean seams: record filtering/search/pagination (`_filter_records` 657, `_filter_and_paginate` 720, `_regex_tree_has_unsafe_backtracking` 760, `_compile_search_regex` 821, `_search_records` 900); audio coercion (`_np_pcm_dtype` 99, `_np_tomono` 108, `_coerce_frames_to_format` 1011, `_wav_header` 1521); the `DebuggerSource` class (242) plus `_run_bundle_source` (411), `_bundle_source` (503), `_session_source` (518); and AEC diagnostics (`_aec_diagnostics_for_turn` 1311, `_vad_whatif_frames` 1456) which reference the module global `_AEC_MAX_TRACK_BYTES` (defined at line 1213).

**Split/refactor plan.** One PR per module, in this order:
1. **`_records.py`** — record filtering/search/pagination plus the regex-backtracking analyzer (`_filter_records`, `_filter_and_paginate`, `_regex_tree_has_unsafe_backtracking`, `_compile_search_regex`, `_record_searchable_text`, `_record_match_fields`, `_search_records`, `_build_transcript`, `_record_to_dict`, `_error_to_dict`).
2. **`_audio.py`** — PCM/WAV/frame coercion (`_np_pcm_dtype`, `_np_tomono`, `_np_ratecv`, `_project_converted_pcm_bytes`, `_serialize_frame`, `_coerce_frames_to_format`, `_safe_audio_format_from_metadata`, `_wav_header`, `_concatenated_wav_for_turn`).
3. **`_sources.py`** — `DebuggerSource` + `_run_bundle_source`/`_bundle_source`/`_session_source` (+ `_safe_ref`, `_safe_turn_id`, `_validated_replay_kwargs`, `_safe_session_config_snapshot`).
4. **`_aec_routes.py`** — AEC diagnostics + VAD what-if (`_aec_track_format`, `_aec_interruption_frames`, `_limit_aec_track`, `_aec_diagnostics_for_turn`, `_vad_baseline_start_count`, `_vad_whatif_frames`, and the constant `_AEC_MAX_TRACK_BYTES`).
5. **`_make_app` conversion** — convert the 29 nested handlers into module-level functions that take `source`/`registry` explicitly (follow `debugger/_aec.py`); **thread the dev-mode proxy source explicitly** rather than capturing it via closure.

**Facade & compatibility.**
- **Move constants with their functions.** `_AEC_MAX_TRACK_BYTES` (line 1213) is monkeypatched by tests as a module global — it must physically move into `_aec_routes.py` alongside `_aec_diagnostics_for_turn`/`_limit_aec_track`, **not** be re-exported as an alias (an alias would not receive the monkeypatch). Update tests that patch it to target the new module path.
- **`server.py` stays a facade** re-exporting every moved private via `__all__`, so the ~30 `from easycat.debugger.server import _helper` sites in `tests/debugger/` keep resolving *until* their PR migrates them (see lockstep).
- **Keep `aiohttp` imports lazy** in the extracted route modules — aiohttp is an optional extra, so top-level import must not break base installs.

**Lockstep edits (per PR).**
- Migrate the `tests/debugger/` import sites for the names moved in *that* PR (either update the import to the new module, or rely on the facade re-export — prefer migrating monkeypatch targets like `_AEC_MAX_TRACK_BYTES` to the real new home so patches land).
- **Gate each pure-Python leaf** (`_records.py`, `_audio.py`, `_sources.py`, `_aec_routes.py`) by adding it to **both** `justfile:58 mypy_gated_paths` and the `[[tool.mypy.overrides]]` module globs in `pyproject.toml`, in sync (QW11 adds a parity guard for exactly this pair).

**Validation.** Per PR:
```bash
uv run pytest tests/debugger/
just typecheck          # gated clean-core, since each leaf is newly gated
```
Add `just guard-ops` only for the bundles/journal-adjacent steps. The final PR shrinks (or deletes) the QS1 allowlist entry for `debugger/server.py`.

**Risk & rollback.** Medium — the `_make_app` handler conversion (PR5) is the only non-mechanical step, because it changes closure capture to explicit parameters. Mitigate by landing PRs 1–4 (pure moves) first so PR5's diff is small. Each PR reverts independently. Missed re-exports fail loudly in `tests/debugger/`; the lazy-aiohttp requirement is verified by importing the module in a base (no-extra) environment.

---

## QS4 — Import Linter contracts *(Phase 1 lands green; Phase 2 after splits)*

**Effort / # of PRs:** 2 PRs (Phase 1 now; Phase 2 later, after the splits settle import edges).

**Files:**
- `pyproject.toml` — add `import-linter>=2` to dev deps; add `[tool.importlinter]` config.
- `justfile` — wire `uv run lint-imports` into the lint recipe.
- `.github/workflows/ci.yml` — add to the lint job.
- `.pre-commit-config.yaml` — add a `repo: local` hook shelling to `uv run lint-imports`.
- Phase 2 touches the `ignore_imports` list only.

**Depends on / Blocks:** **QW10 (`_net.py`)** should land first — it removes cycle-workaround edges and lets the forbidden-contract `ignore_imports` list start smaller. **Phase 2 → after QS2/QS3/QS6** (import edges must settle before the full `layers` contract is stable).

**Problem.** The package layering exists only in prose comments that guard a documented history of hand-fixed cycles. Import Linter turns that prose into a machine-enforced contract for seconds of CI time — especially valuable *while the QS2/QS3/QS6 splits churn import edges*.

**Split/refactor plan.**
- **Phase 1 (PR1) — two low-ambiguity contracts.** Add `[tool.importlinter]` with `root_package = "easycat"` and `exclude_type_checking_imports = true`:
  1. **independence** among `easycat.stt` / `easycat.tts` / `easycat.vad` **only** (verified clean). **Exclude `transports`/`telephony`** — they are genuinely bidirectionally coupled via `twilio_media.py` ↔ `telephony.dtmf`, so an independence contract over them would fail on real, intended coupling.
  2. **forbidden** `server` / `cli` / `debugger` → `transports.webrtc`, seeded with `ignore_imports` for the current edges (`server/auth.py`, `cli/serve.py`, `server/webrtc_routes.py`, `server/voice_server.py`). QW10 shrinks this list; QS6 removes more.
- **Phase 2 (PR2, separate) — the full `layers` contract.** First **resolve or grandfather** the known real violations: `transports/websocket.py:32` (module-level `session_manager` import) and the function-local `session_manager` imports at `webrtc.py:462` and `webtransport.py:1482`. Ratchet `ignore_imports` down per the C901 grandfather convention.

**Facade & compatibility.** No production code moves in Phase 1 — purely additive config + tooling wiring. The contract must pass green on first land, which is why the `ignore_imports` seeds are explicit. Phase 2's `layers` contract must not red CI on legitimate edges — hence grandfathering the three known violations rather than forcing an immediate fix.

**Lockstep edits (per PR).**
- Phase 1: add the dev dep (`import-linter>=2`), run `uv lock`, and wire the command into justfile + ci.yml + pre-commit **in the same PR** so the contract is enforced everywhere at once.
- Phase 2: as each split (QS2/QS3/QS6) removes an import edge, delete the corresponding `ignore_imports` entry so the contract tightens monotonically.

**Validation.**
```bash
uv run lint-imports
just check
```

**Risk & rollback.** Low for Phase 1 (config-only, seeded to pass). Phase 2 carries the risk of a contract that reds on a legitimate edge — mitigated by grandfathering the three documented violations and only ratcheting after QS2/QS3/QS6 land. Rollback is removing the `[tool.importlinter]` block and the wiring.

---

## QS5 — Extract a `LaneHarness` from `validation/runner.py`'s four lanes *(4 PRs)*

**Effort / # of PRs:** 4 PRs — one lane converted per PR.

**Files:**
- `src/easycat/validation/runner.py` — **1734 lines** (verified). Retains `VALIDATION_SELECTORS` (line 62) and the four `run_*` lane entry points: `run_validation_slice` (140), `run_latency_validation` (302), `run_live_validation` (554), `run_release_validation` (806).
- New: `src/easycat/validation/_lane_harness.py`.
- Pin-token tests: `tests/cli/test_validate_runner.py`, and for the latency lane `tests/cli/test_latency_selectors_artifacts.py` + `tests/cli/test_latency_reliability_failures.py`.
- mypy gating: add `_lane_harness.py` to `mypy_gated_paths` + `[[tool.mypy.overrides]]`.

**Depends on / Blocks:** Depends on QS1. Independent of the other splits. Feeds QS4 Phase 2 marginally.

**Problem.** Four copy-pasted lane skeletons mean any report-format change is a four-site edit — verified by the four `ValidationRunResult` construction sites (lines 294, 546, 798, 1167), each preceded by near-identical run-id/run-dir/report-path prologue and git/env-stamp/atomic-write epilogue. This lives in a 1734-line hot module the framework's own docs call the observability backbone.

**Split/refactor plan.** Add `src/easycat/validation/_lane_harness.py` with two functions:
- **`_start_lane_run`** — owns run-id creation, run-dir creation, report-path resolution, and the base artifacts dict.
- **`_finish_lane_run`** — owns git/env metadata stamping, the **triple atomic report write**, and `ValidationRunResult` assembly. It takes lane-specific fields (`latency=`, `reliability=`, `skips=`, and a caller-supplied `command`) as **passthrough** parameters.

Convert **one lane per PR** (suggested order: slice → latency → live → release, cheapest first).

**Scope discipline (do not over-unify):**
- **Prologue + epilogue only.** Do **not** unify the stdout/stderr/junit redaction — the slice lane uses `redact_text` while latency uses `redact_runtime_secrets`, and the live/release lanes accumulate multi-command logs. These differ intentionally; leave them in the lane bodies.
- **Release strictness stays in `run_live_validation`'s params, not the scaffolding.** Do not push strictness flags into `_start_lane_run`/`_finish_lane_run` — that raises risk for no benefit.
- **`VALIDATION_SELECTORS` stays untouched** at `runner.py:62`.

**Facade & compatibility.** `runner.py` keeps all four public `run_*` entry points and `VALIDATION_SELECTORS` at their current names — callers and the CLI are unaffected. Only the *internal* prologue/epilogue bodies move into `_lane_harness.py`; the lanes call the two new helpers.

**Lockstep edits (per PR).**
- **Update source-text pin tokens** in `tests/cli/test_validate_runner.py` — these assert on source text that now points at `_lane_harness.py` instead of inline runner code. Re-anchor them.
- For the **latency lane PR specifically**, also re-anchor pin tokens in `tests/cli/test_latency_selectors_artifacts.py` and `tests/cli/test_latency_reliability_failures.py`.
- Gate `_lane_harness.py` in `mypy_gated_paths` + `[[tool.mypy.overrides]]` (first PR that creates the module).

**Validation.** After each lane conversion:
```bash
just guard-validation
```

**Risk & rollback.** Medium-low. The main risk is the triple atomic report write behaving differently once centralized — mitigated by converting one lane at a time and keeping `just guard-validation` green at each step (it exercises report models, runners, and latency artifacts). The scope discipline (no redaction unification, no strictness move) keeps the blast radius small. Each lane PR reverts independently.

---

## QS6 — Finish the M7 WebRTC convergence *(3 PRs)*

**Effort / # of PRs:** 3 PRs, strictly ordered (safety net → auth swap → extraction).

**Files:**
- `src/easycat/transports/webrtc.py` — **1638 lines** (verified). Holds the duplicated stateless surface: `_cors_headers` (901), `_request_authorized` (931), `_unauthorized_response` (953), `_stats_write_permitted` (962), `_stats_forbidden_response` (979), `_stats_quota_response` (995), `_stats_quota_error` (1004), `_record_stats_write` (1031); `connect` (1036) mounts `_handle_offer`.
- `src/easycat/server/webrtc_routes.py` — **667 lines** (verified; under the 1000 ratchet threshold, so not a QS1 offender). Holds the sibling `WebRTCRoutes` class (96) with its own `_cors_headers` (299), `_unauthorized_response` (326), `_authorized` (250), and the `_handle_offer` delegation (378).
- `src/easycat/server/auth.py` — provides `BearerTokenAuth` (class at line 155, `allow_query_token` field at 166, `authorize` at 169) and request adapters `from_aiohttp_request` (84) / `from_websocket` (98).
- New (PR3): `src/easycat/server/_webrtc_handlers.py`.
- New (PR1): `tests/transports/test_webrtc_route_parity.py`.

**Depends on / Blocks:** Depends on QS1 (allowlist). **Blocks QS4 Phase 2** (the `layers` contract waits until webrtc import edges settle). Coordinates with QW10 (`_net.py`) which removes the `is_loopback`/`normalize_auth_token` edges nearby.

**Problem.** The highest-severity duplication finding: ~400 lines of signaling HTTP surface exist twice — once in `WebRTCTransport` (`transports/webrtc.py`) and once in `WebRTCRoutes` (`server/webrtc_routes.py`) — kept "byte-identical" by docstring promise only, with **no test enforcing it**. Verified: both files carry their own `_cors_headers`, `_unauthorized_response`, `_stats_*` deque logic, and the transport's `_request_authorized` uses a raw `hmac.compare_digest` (import at `webrtc.py:33`, calls at 947/950) with no non-ASCII guard.

**Split/refactor plan (strict order):**
- **PR1 — safety net (no behavior change).** Add `tests/transports/test_webrtc_route_parity.py` asserting the two `_cors_headers` and `_stats_*` implementations agree across an **origin/token/quota matrix**. Include a **non-ASCII credential** case, pinned to the *corrected* 401 behavior (this documents the bug PR2 fixes).
- **PR2 — fix the latent DoS via auth swap.** Replace `webrtc.py`'s `_request_authorized` (931) with `server.auth.BearerTokenAuth`: build the policy **only when a token is configured** (map no-token → no-policy / open), and pass `allow_query_token` to preserve the query-token path. This fixes the latent DoS: today's unguarded `compare_digest` raises `TypeError` → HTTP 500 on non-ASCII credentials; `BearerTokenAuth.authorize` returns a clean 401.
- **PR3 — extract the stateless surface.** Pull the *stateless* handlers (`_cors_headers`, `_unauthorized_response`, `_stats_*`, and the config/stats/health/root/cors-preflight handlers) out of `WebRTCRoutes` into a shared unit, e.g. `server/_webrtc_handlers.py`, **parameterized by `(config, AuthPolicy|None, stats, ...)`**. `WebRTCTransport` imports it **lazily inside `connect()`** (line 1036) so the optional server deps stay optional.

**Explicit non-goals (carried verbatim from the plan):**
- **Do NOT delete `_handle_offer`** — it is genuinely per-peer negotiation and is *semantically different* between singleton (transport) and multi-session (`WebRTCRoutes`) modes.
- **Do NOT mount `WebRTCRoutes` wholesale** into the transport — its constructor needs `config_factory` / `gate` / `manager` that the singleton transport should not own.

**Facade & compatibility.** The transport's public surface (`connect()`, the mounted routes and their paths) is unchanged. Auth behavior changes only in the corrected direction (non-ASCII credential → 401 instead of 500), which PR1's parity test pins first. The lazy import inside `connect()` preserves base-install importability of `transports/webrtc.py`.

**Lockstep edits (per PR).**
- PR1: the parity test is the lockstep artifact — it must pass against *current* behavior for everything except the non-ASCII case (which asserts the target 401).
- PR2: update any test that asserted the old 500/`TypeError` behavior to the new 401.
- PR3: update the docstrings in both files that promise byte-identical duplication (they now point at the shared `_webrtc_handlers.py`); shrink the QS1 allowlist entry for `transports/webrtc.py` (expected shrink ~250–400 lines) and remove the corresponding QS4 forbidden-contract `ignore_imports` edge if the extraction eliminates it.

**Validation.** Per PR:
```bash
just guard-contracts
uv run pytest tests/transports/test_transport_conformance.py
uv run pytest tests/server/test_webrtc_routes.py \
  tests/transports/test_webrtc_config.py \
  tests/transports/test_webrtc_stats_artifacts.py \
  tests/transports/test_webrtc_auth_browser_playground.py \
  tests/transports/test_webrtc_route_parity.py
```

**Risk & rollback.** Medium — this touches live auth on a network-facing transport. The PR ordering *is* the mitigation: PR1's parity net must be green before PR2 changes auth, and PR2's DoS fix ships before PR3's mechanical extraction. Each PR reverts independently. The two non-goals (`_handle_offer`, wholesale mount) prevent the highest-risk over-reach.

---

## QS7 — Ratchet-driven residue (no active split)

**Effort / # of PRs:** 0 dedicated PRs.

**Files (on the QS1 allowlist as future split candidates):**
- `src/easycat/transports/webtransport.py` — **1660 lines** (verified).
- `src/easycat/integrations/agents/langgraph.py` — **1579 lines** (verified).
- `src/easycat/session/_session.py` — **1504 lines** (verified).

**Depends on / Blocks:** Depends only on QS1 (they sit on its allowlist). Nothing else.

**Problem / status.** These three are large but **no confirmed proposal targets them**. The Appendix explicitly rejected speculative splits here (e.g. the LangChain message-accessor extraction — rejected item 2 — because the two rewrite functions operate on fundamentally different persistence models). Forcing a split now would be inventing an approach the plan declined.

**Plan.** **Leave them to be driven down by QS1's shrink-only discipline.** They remain on the ratchet allowlist; as incidental refactors trim them, their allowlist counts shrink (never grow). Do not open a dedicated split PR for any of them without a fresh, confirmed proposal. `session/_session.py` in particular is coupled to the Session-lifecycle guard tests and the `@deprecated` alias work in QW8 — churn it only through those channels.

**Validation.** Covered by QS1's `tests/test_module_size_ratchet.py` (any future shrink deletes/decrements the entry) and the existing `just check`.

**Risk & rollback.** None — this is a deliberate no-op with a standing guard.
