# Provider And Protocol Contracts

[`tests/integration/test_provider_contract_matrix.py`](../integration/test_provider_contract_matrix.py)
is the factory/session wiring check. It proves every registered STT and TTS
config can be dispatched, injected with required runtime dependencies, and
driven through a scripted session.

`tests/contracts/` owns provider protocol contracts. These tests stay offline
by default and cover normalized provider behavior, protocol cassette replay,
schema drift fingerprints, and bridge event grammar. A provider surface must
have a row in [`provider_surface_matrix.py`](provider_surface_matrix.py) or an
explicit exclusion with a reason before it can be considered covered.

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
   shape changes; keep the factory/session wiring assertions in the integration
   matrix separate from protocol contract assertions.

From the repository root, run `uv run easycat validate contracts` for the
offline provider, protocol, and bridge contract lane. Use
`uv run easycat validate contracts --json` when a script or coding agent needs
the same contract run inside the standard CLI envelope. Use
`uv run easycat docs --audience provider-maintainers` to narrow the maintained
docs map to provider-facing routes, or
`uv run easycat docs --audience provider-maintainers --json` when automation
needs that smaller route map with command hints. Use
`uv run pytest tests/contracts` for the focused contract suite, and
`uv run pytest tests/integration/test_provider_contract_matrix.py` when you
need to verify the separate factory/session wiring matrix.
