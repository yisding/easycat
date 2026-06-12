# Provider And Protocol Contracts

[`tests/contracts/test_provider_session_matrix.py`](test_provider_session_matrix.py)
is the factory/session wiring check. It proves every registered STT and TTS
config can be dispatched, injected with required runtime dependencies, and
driven through a scripted session.

`tests/contracts/` owns provider protocol contracts. These tests stay offline
by default and cover normalized provider behavior, protocol cassette replay,
schema drift fingerprints, and bridge event grammar. A provider surface must
have a row in [`provider_surface_matrix.py`](provider_surface_matrix.py) or an
explicit exclusion with a reason before it can be considered covered.

The protocol-semantics assertions themselves ship in the installable contract
kit (`src/easycat/testing/`): each per-surface contract file here subclasses
the corresponding `easycat.testing` suite with its offline fake as
`provider_factory`, so the kit external provider authors run and the in-tree
contract tests cannot drift. The kit's own machinery tests live in
`tests/testing/` and run in the same `just guard-contracts` lane.

When adding or changing provider behavior:

1. Update the relevant row in
   [`provider_surface_matrix.py`](provider_surface_matrix.py), including the
   surface, adapter, protocol, required extra, credential env var, cassette
   status, and contract path.
2. Update the focused contract file for the changed surface:
   [`test_stt_provider_contracts.py`](test_stt_provider_contracts.py),
   [`test_tts_provider_contracts.py`](test_tts_provider_contracts.py),
   [`test_vad_provider_contracts.py`](test_vad_provider_contracts.py),
   [`test_transport_contracts.py`](test_transport_contracts.py), or
   [`test_agent_bridge_contracts.py`](test_agent_bridge_contracts.py).
3. Refresh cassettes or schema fingerprints only when the provider protocol
   shape changes; keep the factory/session wiring assertions in the session
   matrix separate from protocol contract assertions.

From the repository root, run `uv run easycat validate contracts` for the
offline provider, protocol, and bridge contract lane (add `--json` for the
same run inside the standard CLI envelope:
`uv run easycat validate contracts --json`). Use
`uv run easycat docs --audience provider-maintainers` to narrow the maintained
docs map to provider-facing routes, or
`uv run easycat docs --audience provider-maintainers --json` when automation
needs that smaller route map. Coding agent? Use the root
[AGENTS.md](../../AGENTS.md) for repository coding rules; use
[llms.txt](../../llms.txt) for machine-readable docs route discovery or run
`uv run easycat explain json-schema`. Use
`uv run pytest tests/contracts` for the focused contract suite, and
`uv run pytest tests/contracts/test_provider_session_matrix.py` when you
need to verify the separate factory/session wiring matrix.
