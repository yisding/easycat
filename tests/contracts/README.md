# Provider And Protocol Contracts

`tests/integration/test_provider_contract_matrix.py` is the
factory/session wiring check. It proves every registered STT and TTS config
can be dispatched, injected with required runtime dependencies, and driven
through a scripted session.

`tests/contracts/` owns provider protocol contracts. These tests stay offline
by default and cover normalized provider behavior, protocol cassette replay,
schema drift fingerprints, and bridge event grammar. A provider surface must
have a row in `provider_surface_matrix.py` or an explicit exclusion with a
reason before it can be considered covered.

From the repository root, run `uv run easycat validate contracts` for the
offline provider, protocol, and bridge contract lane. Use
`uv run pytest tests/contracts` for the focused contract suite, and
`uv run pytest tests/integration/test_provider_contract_matrix.py` when you
need to verify the separate factory/session wiring matrix.
