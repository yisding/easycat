# Contributing to EasyCat

Thanks for improving EasyCat. This guide focuses on **testing and validation** —
how to run each test slice, what the markers mean, and how to keep the suite
green. For architecture, see [`CLAUDE.md`](CLAUDE.md) and [`AGENTS.md`](AGENTS.md).

## Quick start

```bash
uv sync --group dev        # install project + dev tools
just                       # list every task (or read the justfile)
just check                 # fmt-check + lint + tests (the pre-PR gauntlet)
```

Don't have [`just`](https://github.com/casey/just)? Every recipe is a one-liner
you can copy out of the `justfile`. Install it with `uv tool install rust-just`,
`brew install just`, `cargo install just`, or your distro's package manager.

## The development loop

| Task | `just` recipe | Raw command |
| --- | --- | --- |
| Install dev deps | `just sync` | `uv sync --group dev` |
| Install an extra | `just sync-extra openai` | `uv sync --group dev --extra openai` |
| Full test suite | `just test` | `uv run pytest` |
| Fast parallel run | `just test-fast` | `uv run pytest -n auto --dist loadscope -m "not integration_socket and not integration_live and not slow and not stress and not flaky"` |
| One file / node | `just test-one tests/test_cancel.py` | `uv run pytest tests/test_cancel.py` |
| Lint | `just lint` | `uv run ruff check .` |
| Format | `just fmt` | `uv run ruff format .` |
| Type gate (mypy, clean core) | `just typecheck` | `uv run mypy --follow-imports=silent src/easycat/debug` |
| Type report (mypy, whole repo) | `just typecheck-all` | `uv run mypy src/easycat` |
| Fast types (ty, advisory) | `just typecheck-fast` | `uvx ty check src/easycat` |
| Coverage | `just cov` | `uv run pytest -n auto --dist loadscope --cov --cov-report=term-missing -m "...safe slice..."` |
| Validate (quick) | `just validate-quick` | `uv run easycat validate quick` |
| Pre-commit hooks | `just pre-commit` | `uv run pre-commit run --all-files` |

> `mypy` ships in the `dev` group, so `just typecheck` / `just typecheck-all`
> work right after `uv sync --group dev`. `just typecheck-fast` runs Astral
> `ty` on demand via `uvx` (no install needed; it's advisory, not a gate).
> `just cov` is plain `pytest --cov` and has no type-checker dependency.

### Parallel runs and xdist safety

`just test-fast` and `just cov` use `pytest -n auto --dist loadscope`.
`loadscope` keeps every test in a module on the **same** worker, which matters
for async event-loop tests and any socket/port-binding tests. If you add tests
that bind a **fixed** port (rather than port `0`), keep them in one module and
prefer marking them `integration_socket`. `just test` (serial) is the source of
truth; parallel runs are an opt-in speedup. Always run coverage as
`pytest --cov` — never `coverage run -m pytest -n auto`, which reports 0% under
xdist.

## Validation slices and the `easycat validate` CLI

CI runs the same slices you can run locally. Each writes a JSON + JUnit report
under `.easycat/validation/`:

| Slice | Command | Marker selection |
| --- | --- | --- |
| `quick` | `easycat validate quick` | not integration_socket / live / slow / stress / flaky |
| `socket` | `easycat validate socket` | integration_socket, not live, not flaky |
| `stress` | `easycat validate stress` | stress, not live, not flaky |
| `latency` | `easycat validate latency --smoke` | latency probes (live) |
| `live` | `easycat validate live --provider openai` | integration_live + provider/surface |

`easycat validate report <path>` renders a saved report.

## Marker taxonomy

Markers are **strict** (`strict_markers = true`): an unknown marker fails
collection. The full list lives in `pyproject.toml` under
`[tool.pytest.ini_options].markers`. What they mean:

- `integration_local` — in-process end-to-end with fake providers.
- `integration_socket` — needs localhost socket bind/connect (auto-skipped
  where the sandbox forbids binding; see `tests/conftest.py`).
- `integration_live` — needs live API keys and optional provider extras.
- `slow` — long end-to-end tests; opt in with `-m slow`.
- `contract` — provider / protocol / bridge contract tests.
- `latency` — latency measurement or SLO tests.
- `stress` — load / soak / high-volume tests.
- `release` — release-gate validation.
- `flaky` — quarantined intermittent test (see policy below).
- `provider_openai` / `provider_deepgram` / `provider_elevenlabs` /
  `provider_cartesia` — provider coverage; `provider("name")` is the generic
  form for custom providers.
- `surface_stt` / `surface_tts` / `surface_agent` / `surface_transport` /
  `surface_vad` — which provider surface is exercised.
- `agent_bridge` — agent bridge contract or live coverage.
- `requires_extra("name")` — needs an optional dependency extra.

### Provider / surface pairing (enforced)

`tests/_marker_lint.py` requires that any test marked `contract`,
`integration_live`, or `latency` declares **both** a provider marker
(`provider_*` or `provider("name")`) **and** a surface marker (`surface_*`).
Declaring one without the other fails collection with a pointer to the
missing side. This keeps the validation matrix honest.

## Flaky-quarantine policy

Quarantine a genuinely intermittent test instead of letting it redden CI, but
quarantine is a **debt with an owner and a deadline**. `@pytest.mark.flaky`
requires three keyword fields (enforced by `tests/_marker_lint.py`):

```python
@pytest.mark.flaky(
    issue="https://github.com/yisding/easycat2/issues/123",
    owner="yi",
    review_by="2026-07-01",  # YYYY-MM-DD; a past date fails collection
)
```

Rules (from `tests/_marker_lint.py`):

- All three of `issue`, `owner`, `review_by` are required and non-empty.
- `review_by` must be a valid ISO date and **must not be in the past** — a
  stale date fails collection, forcing a re-triage.
- A `flaky` test may not also be `release`-scoped.
- Validation slices deselect `flaky`, so quarantined tests never gate a PR.

## Cassettes (`tests/cassettes/`)

Provider protocol tests replay **hand-maintained JSON cassettes** so they run
offline and deterministically. There are three transport flavors:

- `tests/cassettes/http/` — request/response pairs (e.g. `openai-stt.json`).
- `tests/cassettes/ws/` — ordered WebSocket frames
  (e.g. `openai-realtime-stt.json`).
- `tests/cassettes/sse/` — server-sent-event streams
  (e.g. `remote-responses-api.json`).

Replay tests live in `tests/contracts/test_*_cassette_replay.py` and assert the
frame schema and ordering. Cassettes are **redacted** — secrets and volatile
fields are stripped (`tests/contracts/test_http_cassette_redaction.py` guards
this). When a provider's wire protocol changes:

1. Capture the new exchange against the live API in a throwaway script.
2. Redact credentials, account ids, and timestamps.
3. Update the JSON cassette and the expected frame order in the replay test.
4. Run `just test-one tests/contracts/` and confirm the schema-fingerprint
   checks (`tests/contracts/schema_fingerprints.py`) still pass.

Never commit a cassette containing a real key — codespell and the redaction
test are backstops, not a substitute for review.

## RunBundle golden tests (`src/easycat/debug/testing.py`)

A `RunBundle` is a zipped, replayable recording of a full session journal.
`session.export_debug_bundle(path)` writes one; `load_bundle(path)` reads it.
The helpers in `easycat.debug.testing` turn a captured production failure into
a regression test in the same PR that fixes it:

```python
from easycat.debug.testing import load_bundle, assert_turn_completed, assert_no_error

def test_roundtrip_regression():
    bundle = load_bundle("tests/fixtures/roundtrip.zip")
    assert_turn_completed(bundle, turn_id="t1")
    assert_no_error(bundle, turn_id="t1")
```

Available assertions: `assert_exact_match`, `assert_regex`,
`assert_turn_completed`, `assert_no_error`, `assert_tool_called`, plus the
iteration helpers `iter_records`, `turn_records`, `find_record`. To refresh a
golden bundle, re-export it from a session run and re-run the test; review the
bundle diff like any other fixture change.

## Adding an STT or TTS provider

EasyCat uses **registries**, not inheritance. To add a provider:

1. **Implement** one provider per file under `src/easycat/stt/` or
   `src/easycat/tts/`, satisfying the `STTProvider` / `TTSProvider` Protocol
   in `src/easycat/providers.py`. Reuse `STTBase` / `TTSBase` plumbing.
2. **Add a config dataclass** for the provider's options.
3. **Register** the `(provider class, config class)` pair:
   - STT: `_PROVIDER_TO_CONFIG` in `src/easycat/stt/factory.py`.
   - TTS: `_PROVIDER_TO_CONFIG` (aliased `_PROVIDERS`) in
     `src/easycat/tts/factory.py`.
4. **Declare the contract row** in
   `tests/contracts/provider_surface_matrix.py` (a `ProviderSurfaceContract`
   with adapter path, protocol, required extra, credential env var, and
   cassette status) — or add an explicit exclusion with a reason. The matrix
   tests fail if a registered provider has no row.
5. **Add an extra** in `pyproject.toml` `[project.optional-dependencies]` if
   the provider needs an SDK, and wire it into `all` / `quickstart` as
   appropriate.
6. **Tests**: contract tests under `tests/contracts/` plus unit tests under
   `tests/stt/` or `tests/tts/`. Mark provider/surface pairs correctly (see
   the pairing rule above). If the protocol is replayable, add a cassette.

## What's expected on a PR

- `just check` is green (format + lint + tests).
- New code is typed (Python `>=3.11`, typing-first). `mypy` is the
  authoritative type checker: `just typecheck` gates the clean core
  (`easycat.debug`) and must stay green, while `just typecheck-all` is the
  advisory whole-repo report we ratchet down over time. `just typecheck-fast`
  (Astral `ty`) is faster local feedback but advisory only (beta).
- **Patch coverage**: cover the lines your PR changes (`just cov` locally).
  There is no hard global coverage gate; reviewers look at the diff.
- Tests added/updated for every behavior change.
- Commit subjects follow `<scope>: <imperative summary>`
  (e.g. `stt: normalize partial transcript events`).
- Secrets stay in environment variables; never commit keys or un-redacted
  cassettes.
